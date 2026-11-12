"""LongCat-Video-Avatar-1.5 引擎适配器（43 号方案批 B）。

与 MultiTalk 同一 ComfyUI/WanVideoWrapper 生态：LongCat 13.6B（GGUF 量化）+
Whisper-Large-v3 音频编码 + longcat_distill_euler 蒸馏少步采样 + 原生双音轨。
复用既有「逐镜头缓存 → 近景裁切 → 拼接 → 严格校验 → 候选登记」管线语义，
不改动已经 GPU 验收的 MultiTalk 路径（comfyui_video.py 保持原样）。

节点与参数依据（2026-11 核对）：
- kijai/ComfyUI-WanVideoWrapper `LongCat/nodes.py`（LongCatAvatarWhisperEmbeds /
  WanVideoLongCatAvatarExtendEmbeds 的准确 schema）
- `HuMo/nodes.py` WhisperModelLoader（WHISPERMODEL，audio_encoders 目录）
- `nodes_sampler.py` WanVideoSamplerv2 / WanVideoSchedulerv2
- 官方示例 example_workflows/LongCatAvatar_audio_image_to_video_example_01.json
  （bf16 强制、longcat_distill_euler 12 步 / shift 12、rank128 蒸馏 LoRA）

边界（v1，显式拒绝而非静默回落）：
- 仅标准生成配置（accepted）+ 双人同框（twoShot）；倾听/专家/动作参考配置、
  过肩机位、局部重做（reuse/redo）仍属 MultiTalk 引擎。
- 生成画布 480p(832x480)/720p(1280x720)；1080p 导出走既有裁切放大链路。
- GPU 效果与显存实测须在 RTX 5080 验收（HANDOFF 边界）；本机 CPU 仅验契约与管线逻辑。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import marshal
import math
import shutil
import time
import uuid
import wave
from copy import deepcopy
from pathlib import Path
from types import CodeType

import httpx

from ..services.generation_profiles import validate_video_stream
from ..services.video_plans import camera_plan, clipped_camera_plan, closeup_box
from .comfyui_video import ComfyUIVideoProvider

ENGINE = "longcat-avatar-1.5"
TEMPLATE_PATH = Path(__file__).with_name("longcat_workflow.json")


def load_template() -> dict:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


def validate_longcat_request(req: dict, visual: dict) -> None:
    """v1 能力边界：不支持的请求显式拒绝并指明替代路径，绝不静默回落。"""
    if visual.get("mode", "twoShot") != "twoShot":
        raise RuntimeError("LongCat 引擎当前仅支持双人同框场景；过肩正反打请切换 MultiTalk 引擎")
    if req.get("generationProfile", "accepted") != "accepted":
        raise RuntimeError("LongCat 引擎当前仅支持标准生成配置；倾听/专家/动作参考请切换 MultiTalk 引擎")
    if req.get("reuseMotionArtifactId") or req.get("redoFromSec") is not None:
        raise RuntimeError("LongCat 引擎 v1 暂不支持局部重做；请整段生成或切换 MultiTalk 引擎")


def generation_canvas(resolution: str) -> tuple[int, int]:
    """LongCat 原生 480p/720p；更高分辨率由导出裁切链路放大。"""
    width, height = map(int, resolution.split("x"))
    return (832, 480) if height <= 480 else (1280, 720)


def build_shot_workflow(template: dict, *, image_name: str, audio_a: str, audio_b: str,
                        audio_mix: str, mask_a: str, mask_b: str, positive_prompt: str,
                        seed: int, frame_count: int, width: int, height: int,
                        filename_prefix: str, model: str | None = None,
                        lora: str | None = None) -> dict:
    """把模板参数化为一个镜头的可提交工作流（纯函数，CPU 可测）。"""
    wf = deepcopy(template)
    wf["104"]["inputs"]["image"] = image_name
    wf["105"]["inputs"]["width"] = width
    wf["105"]["inputs"]["height"] = height
    wf["108"]["inputs"]["audio"] = audio_a
    wf["109"]["inputs"]["audio"] = audio_b
    wf["121"]["inputs"]["audio"] = audio_mix
    wf["111"]["inputs"]["image"] = mask_a
    wf["112"]["inputs"]["image"] = mask_b
    wf["110"]["inputs"]["num_frames"] = frame_count
    wf["116"]["inputs"]["num_frames"] = frame_count
    wf["117"]["inputs"]["positive_prompt"] = positive_prompt
    wf["119"]["inputs"]["seed"] = int(seed)
    wf["122"]["inputs"]["filename_prefix"] = filename_prefix
    if model:
        wf["100"]["inputs"]["model"] = model
    if lora:
        wf["102"]["inputs"]["lora"] = lora
    return wf


def _implementation_hash() -> str:
    def normalized(code):
        constants = tuple(normalized(c) if isinstance(c, CodeType) else c for c in code.co_consts)
        return code.replace(co_firstlineno=0, co_linetable=b"", co_consts=constants)
    return hashlib.sha256(marshal.dumps((
        normalized(build_shot_workflow.__code__),
        normalized(validate_longcat_request.__code__),
        normalized(LongCatVideoProvider._shot.__code__),
    ))).hexdigest()


class LongCatVideoProvider:
    capabilities = {"simulated": False, "engine": ENGINE, "dualTrack": True,
                    "overShoulder": False,
                    "resolutions": ["854x480", "1280x720", "1920x1080"],
                    "fps": [25, 30], "progress": True, "cancel": False,
                    "generationProfiles": ["accepted"]}

    def __init__(self, *, base_url: str, input_dir: Path, artifact_store,
                 model: str | None = None, lora: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.input_dir = input_dir
        self.artifacts = artifact_store
        self.model = model
        self.lora = lora
        self._lock = asyncio.Lock()
        self.implementation_hash = _implementation_hash()

    # 与 MultiTalk 共用的引擎无关工具（静态方法，直接复用不重写）。
    _command = staticmethod(ComfyUIVideoProvider._command)
    _health = staticmethod(ComfyUIVideoProvider._health)
    _mask_png = staticmethod(ComfyUIVideoProvider._mask_png)
    performance_plan = staticmethod(ComfyUIVideoProvider.performance_plan)

    def _asset(self, asset_id):
        entry = self.artifacts.by_id(asset_id)
        if not entry or not Path(entry["path"]).is_file():
            raise RuntimeError(f"缺少真实资产文件：{asset_id}")
        return Path(entry["path"])

    async def render(self, req, progress=None):
        if req.get("cacheOnly"):
            return await self._render(req, progress)
        async with self._lock:
            return await self._render(req, progress)

    async def _render(self, req, progress=None):
        project = req["projectSnapshot"]
        timeline = project.get("audioTimeline") or {}
        if timeline.get("revisionId") != req["revisionId"]:
            raise RuntimeError("脚本与配音版本不一致，请重新生成配音")
        master = self._asset(timeline.get("masterAudioAssetId"))
        variants = [v for v in project.get("visualVariants", [])
                    if v.get("aspect") == req.get("aspect", project.get("aspect"))]
        if not variants:
            raise RuntimeError("请先建立对应画幅的双人场景资产")
        selected_id = req.get("visualVariantId")
        visual = next((v for v in variants if v["id"] == selected_id), None) if selected_id else variants[-1]
        if visual is None:
            raise RuntimeError("所选画面资产不存在或画幅不匹配")
        validate_longcat_request(req, visual)
        image = self._asset(visual["masterImage"].get("artifactId"))
        width, height = map(int, req.get("resolution", "854x480").split("x"))
        fps = int(req.get("fps", 25))
        if (width, height) not in {(854, 480), (1280, 720), (1920, 1080)} or fps not in {25, 30}:
            raise RuntimeError("不支持的导出规格")
        gen_w, gen_h = generation_canvas(req.get("resolution", "854x480"))
        rate = int(timeline["sampleRate"])
        total = timeline["sampleCount"] / rate
        start = float(req.get("rangeStartSec", 0))
        end = float(req.get("rangeEndSec", total))
        if not 0 <= start < end <= total + 0.001:
            raise RuntimeError("非法生成区间")
        revision = next(r for r in project["scriptRevisions"] if r["id"] == req["revisionId"])
        speakers = {t["id"]: t["speaker"] for t in revision["turns"]}
        performance = self.performance_plan(project, req["revisionId"])
        template = load_template()
        digest = hashlib.sha256(json.dumps(req, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        folder = self.artifacts.root / "video" / digest / uuid.uuid4().hex
        folder.mkdir(parents=True, exist_ok=True)
        motion_digest = hashlib.sha256(json.dumps({
            "engine": ENGINE,
            "audio": self.artifacts._hash_file(str(master)), "image": self.artifacts._hash_file(str(image)),
            "timeline": timeline, "speakers": speakers, "regions": visual["personRegions"],
            "closeupRegions": visual.get("closeupRegions") or {},
            "workflow": template, "model": self.model, "lora": self.lora,
            "performance": performance, "implementation": self.implementation_hash,
            "seed": req.get("seed", 1), "generationCanvas": [gen_w, gen_h],
        }, sort_keys=True).encode()).hexdigest()
        motion_folder = self.artifacts.root / "video_motion" / motion_digest
        motion_folder.mkdir(parents=True, exist_ok=True)
        self.input_dir.mkdir(parents=True, exist_ok=True)
        # 与 MultiTalk 同口径：母轨归一化后按权威时间轨精确拆分 A/B，间隙双轨静音。
        pcm = folder / "master.wav"
        await self._command("ffmpeg", "-v", "error", "-y", "-i", master,
                            "-ar", rate, "-ac", 1, "-c:a", "pcm_s16le", pcm)
        with wave.open(str(pcm)) as reader:
            data = reader.readframes(reader.getnframes())
        if len(data) // 2 < timeline["sampleCount"]:
            raise RuntimeError("母轨文件短于权威时间轨")
        tracks = {speaker: bytearray(len(data)) for speaker in ("A", "B")}
        for unit in timeline["units"]:
            offset = timeline["unitOffsets"][unit["id"]]
            stop = offset + unit["sampleCount"]
            tracks[speakers[unit["turnId"]]][offset * 2:stop * 2] = data[offset * 2:stop * 2]
        for speaker in ("A", "B"):
            self._mask_png(self.input_dir / f"{digest}_{speaker}.png", visual["personRegions"][speaker])
        shutil.copyfile(image, self.input_dir / f"{digest}.png")
        (folder / "performance_plan.json").write_text(json.dumps(performance, ensure_ascii=False, indent=2), encoding="utf-8")
        episode_camera_plan = camera_plan(project, req.get("cameraMode", "speaker"))
        segments = []
        stats = {"generated": 0, "cached": 0, "engineUsed": False}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30, trust_env=False) as client:
            cursor = 0.0
            while cursor < end - 0.001:
                stop = min(total, cursor + 5)
                shot_key = round(cursor * rate) * 100000000 + round(stop * rate)
                clip = motion_folder / f"shot_{shot_key}.mp4"
                reference_hash = self.artifacts._hash_file(str(segments[-1])) if segments else self.artifacts._hash_file(str(image))
                marker = clip.with_suffix(".complete.json")
                valid = False
                if clip.exists() and marker.exists():
                    try:
                        saved = json.loads(marker.read_text(encoding="utf-8"))
                    except (ValueError, OSError):
                        saved = {}
                    valid = saved.get("referenceHash") == reference_hash and saved.get("fileHash") == self.artifacts._hash_file(str(clip))
                if progress:
                    progress(f"longcat motion {cursor:.1f}–{stop:.1f}s: " + ("cached" if valid else "generating"))
                if not valid:
                    if req.get("cacheOnly"):
                        raise RuntimeError("已验证动画缺失或完整性改变，请重新生成对应片段")
                    if not stats["engineUsed"]:
                        await self._prepare_engine(client)
                        stats["engineUsed"] = True
                    await self._shot(client, digest, shot_key, cursor, stop, rate, data, tracks,
                                     motion_folder, clip, segments[-1] if segments else None,
                                     performance, req.get("seed", 1), visual["personRegions"],
                                     gen_w, gen_h)
                    stats["generated"] += 1
                    marker.write_text(json.dumps({"referenceHash": reference_hash,
                                                  "fileHash": self.artifacts._hash_file(str(clip))}), encoding="utf-8")
                else:
                    stats["cached"] += 1
                segments.append(clip)
                cursor = stop
        if progress:
            progress("compose and validate")
        concat = folder / "clips.txt"
        concat.write_text("\n".join("file '" + p.as_posix().replace("'", "'\\''") + "'" for p in segments), encoding="utf-8")
        raw_base = folder / "all_motion.mp4"
        base = folder / "animated_duo.mp4"
        await self._command("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", 0,
                            "-i", concat, "-c", "copy", raw_base)
        if start == 0 and abs(cursor - end) < 0.001:
            shutil.copyfile(raw_base, base)
        else:
            await self._command("ffmpeg", "-v", "error", "-y", "-ss", start,
                                "-i", raw_base, "-t", end - start, "-an", "-c:v", "libx264", "-crf", 19, base)
        # 近景裁切完全按显式虚线框（closeup_box），与标准/快速模式同一口径。
        edited = []
        for n, shot in enumerate(clipped_camera_plan(episode_camera_plan, start, end)):
            a, b, active = shot["start"], shot["end"], shot["camera"]
            crop = ""
            if active in ("A", "B"):
                box = closeup_box(visual, active)
                crop = f"crop=iw*{box['width']}:ih*{box['height']}:iw*{box['x']}:ih*{box['y']},"
            part = folder / f"edit_{n:04d}.mp4"
            first_frame = round((a - start) * fps)
            last_frame = round((b - start) * fps)
            if last_frame <= first_frame:
                continue
            await self._command("ffmpeg", "-v", "error", "-y", "-reinit_filter", 0, "-i", base,
                "-frames:v", last_frame - first_frame, "-an", "-vf", f"fps={fps},trim=start_frame={first_frame}:end_frame={last_frame},setpts=PTS-STARTPTS," + crop + f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1",
                "-r", fps, "-c:v", "libx264", "-preset", "fast", "-crf", 19, "-pix_fmt", "yuv420p", part)
            edited.append(part)
        concat.write_text("\n".join("file '" + p.as_posix().replace("'", "'\\''") + "'" for p in edited), encoding="utf-8")
        final = folder / "final.mp4"
        await self._command("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", 0,
                            "-i", concat, "-ss", start, "-i", pcm, "-t", end - start,
                            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", final)
        probe = json.loads(await self._command("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", final))
        if not any(s["codec_type"] == "audio" for s in probe["streams"]) or abs(float(probe["format"]["duration"]) - (end - start)) > 0.2:
            raise RuntimeError("成片音轨或时长校验失败，未登记输出")
        stream = next((v for v in probe["streams"] if v["codec_type"] == "video"), None)
        if not stream or (stream.get("width"), stream.get("height")) != (width, height):
            raise RuntimeError("成片分辨率校验失败，未登记输出")
        try:
            validate_video_stream(stream, end - start, fps)
        except ValueError as exc:
            raise RuntimeError(str(exc) + "，未登记输出") from exc
        artifact_id = "VID-" + uuid.uuid4().hex
        motion_manifest = {"motionDirectory": str(motion_folder), "stats": stats,
                           "engine": ENGINE, "cameraPlan": episode_camera_plan}
        (folder / "motion_manifest.json").write_text(json.dumps(motion_manifest, indent=2), encoding="utf-8")
        self.artifacts.register(artifact_id, "video", str(final), digest, req, "longcat-avatar-v1")
        return {"artifactIds": [artifact_id], "meta": {"durationSec": end - start,
            "resolution": f"{width}x{height}", "fps": fps, "path": str(final),
            "motionDirectory": str(motion_folder), "engine": ENGINE,
            "performancePlan": performance, "generationProfile": "accepted",
            "cameraPlan": episode_camera_plan, "generationStats": stats,
            "cameraMode": req.get("cameraMode", "speaker"), "qualityStatus": "needs_review"}}

    async def _prepare_engine(self, client):
        """与 MultiTalk 同口径：先释放配音模型再占显存；有任务在跑则绝不释放。"""
        await self._health(client)
        queue = (await client.get("/queue")).json()
        if queue.get("queue_running") or queue.get("queue_pending"):
            return
        release = await client.post("/prompt", json={"prompt": {"release": {
            "class_type": "BSAI_IndexTTS2.5UnloadModel", "inputs": {"any_input_任意输入": uuid.uuid4().hex}}}})
        release.raise_for_status()
        for _ in range(30):
            history = (await client.get(f"/history/{release.json()['prompt_id']}")).json().get(release.json()["prompt_id"])
            if history:
                if history.get("status", {}).get("status_str") != "success":
                    raise RuntimeError("无法释放配音模型，未开始视频生成")
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("释放配音模型超时")
        free = await client.post("/free", json={"free_memory": True, "unload_models": True})
        free.raise_for_status()
        await asyncio.sleep(2)

    async def _shot(self, client, digest, index, start, end, rate, data, tracks, folder, clip,
                    previous_clip, performance, seed, regions, gen_w, gen_h):
        tag = f"{digest}_{index:04d}"
        for kind, track in (("a", tracks["A"]), ("b", tracks["B"]), ("mix", data)):
            with wave.open(str(self.input_dir / f"{tag}_{kind}.wav"), "wb") as writer:
                writer.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                # 与 MultiTalk 相同的静音预热，避免模型初始不稳定帧进入剪辑。
                writer.writeframes(bytes(round(0.32 * rate) * 2) + bytes(track[round(start * rate) * 2:round(end * rate) * 2]))
        image_name = f"{digest}.png"
        if previous_clip:
            reference = self.input_dir / f"{tag}_reference.png"
            await self._command("ffmpeg", "-v", "error", "-y", "-sseof", -0.04, "-i", previous_clip,
                                "-frames:v", 1, reference)
            if not reference.is_file():
                raise RuntimeError("无法读取前一镜头的末帧，未开始下一镜头")
            image_name = reference.name
        roles = " ".join(
            f"From {max(0, part['start'] - start):.1f} to {min(end, part['end']) - start:.1f} seconds, "
            f"{('the man on the left' if part['speaker'] == 'A' else 'the woman on the right')} speaks; "
            f"{('the woman on the right' if part['speaker'] == 'A' else 'the man on the left')} listens with closed lips, "
            "turns toward the speaker, gently nods once and adjusts shoulders naturally."
            for part in (performance or []) if part['end'] > start and part['start'] < end)
        positive = (
            "A continuous live-action podcast conversation between two hosts in a warm modern studio. "
            "The silent listener actively reacts: natural head turn toward the speaker, gentle nod, "
            "blinking, breathing and subtle shoulder movement, with lips closed. "
            "The speaking host makes conversational hand gestures. Neither person holds a frozen portrait pose. "
            "Preserve identities, clothing, microphones, hands and stable studio. " + roles)
        frame_count = 4 * math.ceil(((end - start) + 0.32) * 25 / 4) + 1
        wf = build_shot_workflow(load_template(), image_name=image_name,
            audio_a=f"{tag}_a.wav", audio_b=f"{tag}_b.wav", audio_mix=f"{tag}_mix.wav",
            mask_a=f"{digest}_A.png", mask_b=f"{digest}_B.png", positive_prompt=positive,
            seed=seed, frame_count=frame_count, width=gen_w, height=gen_h,
            filename_prefix=f"completion/{tag}", model=self.model, lora=self.lora)
        record = folder / f"job_{index:04d}.json"
        prompt = None
        if record.is_file():
            try:
                saved = json.loads(record.read_text(encoding="utf-8"))
                if saved.get("workflow") == wf:
                    previous = saved["promptId"]
                    history = (await client.get(f"/history/{previous}")).json().get(previous)
                    queue = (await client.get("/queue")).json()
                    queued = any(len(item) > 1 and item[1] == previous for item in queue.get("queue_running", []) + queue.get("queue_pending", []))
                    if history or queued:
                        prompt = previous
            except (ValueError, KeyError, OSError):
                pass
        if prompt is None:
            response = await client.post("/prompt", json={"prompt": wf, "client_id": tag})
            response.raise_for_status()
            result = response.json()
            if result.get("node_errors"):
                raise RuntimeError(str(result["node_errors"]))
            prompt = result["prompt_id"]
            temporary = record.with_suffix(".partial.json")
            temporary.write_text(json.dumps({"promptId": prompt, "start": start, "end": end,
                "workflow": wf}, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(record)
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            await self._health(client)
            history = (await client.get(f"/history/{prompt}")).json().get(prompt)
            if not history:
                continue
            if history.get("status", {}).get("status_str") != "success":
                raise RuntimeError("LongCat 采样失败：" + json.dumps(history.get("status"), ensure_ascii=False)[-2500:])
            videos = history.get("outputs", {}).get("122", {}).get("gifs", [])
            video = next((v for v in videos if v.get("filename", "").endswith(".mp4")), None)
            if not video or Path(video["filename"]).name != video["filename"]:
                raise RuntimeError("LongCat 没有真实视频输出；完成标记不能作为视频")
            response = await client.get("/view", params={k: video[k] for k in ("filename", "subfolder", "type")})
            response.raise_for_status()
            raw = folder / f"raw_{index:04d}.mp4"
            raw.with_suffix(".partial.mp4").write_bytes(response.content)
            raw.with_suffix(".partial.mp4").replace(raw)
            probe = json.loads(await self._command("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", raw))
            stream = next((s for s in probe["streams"] if s["codec_type"] == "video"), None)
            expected = (end - start) + 0.32
            if not stream or float(probe["format"].get("duration", 0)) < expected - 0.08:
                raise RuntimeError("LongCat 输出短于计划时长，未采用该镜头")
            await self._command("ffmpeg", "-v", "error", "-y", "-i", raw, "-ss", 0.32, "-t", end - start,
                                "-an", "-c:v", "libx264", "-crf", 19, "-pix_fmt", "yuv420p",
                                clip.with_suffix(".partial.mp4"))
            clip.with_suffix(".partial.mp4").replace(clip)
            return
        raise RuntimeError("镜头生成超时；已完成镜头保留，可重试")