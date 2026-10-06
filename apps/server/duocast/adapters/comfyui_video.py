"""Real dual-track MultiTalk rendering with immutable, resumable shot files."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import marshal
from types import CodeType
from pathlib import Path
import shutil
import time
import uuid
import wave
import struct
import zlib

import httpx
from ..services.listening_plan import listening_plan, listening_prompt, reference_workflow
from ..services.video_plans import camera_plan, clipped_camera_plan, crop_region, adaptive_prompt
from ..services.generation_profiles import configure_generation_profile, listener_layer_filter, validate_video_stream, PROFILES

# Persisted fingerprint of the accepted v4 generator. Only its unchanged legacy
# prompt path may reuse that cache; adaptive work has a distinct fingerprint.
LEGACY_IMPLEMENTATION = "f05d6b4e6a7a98808a8bdbb2a7fdf51bc93f1c7149b61dd499c9b7413e8e4d75"


class ComfyUIVideoProvider:
    capabilities = {"simulated": False, "dualTrack": True,
                    "overShoulder": True,
                    "resolutions": ["854x480", "1280x720", "1920x1080"],
                    "fps": [25, 30], "progress": True, "cancel": False}

    def __init__(self, *, base_url: str, input_dir: Path, artifact_store):
        self.base_url = base_url.rstrip("/")
        self.input_dir = input_dir
        self.artifacts = artifact_store
        self._lock = asyncio.Lock()
        def normalized(code):
            constants = tuple(normalized(c) if isinstance(c, CodeType) else c for c in code.co_consts)
            return code.replace(co_firstlineno=0, co_linetable=b"", co_consts=constants)
        self.motion_implementation_hash = hashlib.sha256(marshal.dumps((normalized(type(self)._automatic_reference.__code__), normalized(listening_prompt.__code__), normalized(reference_workflow.__code__), normalized(type(self)._shot.__code__), normalized(type(self)._prepare_motion_reference.__code__), normalized(type(self)._compose_listener.__code__), normalized(adaptive_prompt.__code__), normalized(configure_generation_profile.__code__), normalized(listener_layer_filter.__code__)))).hexdigest()

    def _asset(self, asset_id):
        entry = self.artifacts.by_id(asset_id)
        if not entry or not Path(entry["path"]).is_file():
            raise RuntimeError(f"缺少真实资产文件：{asset_id}")
        return Path(entry["path"])

    @staticmethod
    async def _command(*args):
        process = await asyncio.create_subprocess_exec(
            *map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate()
        if process.returncode:
            raise RuntimeError(err.decode("utf-8", "replace")[-3000:])
        return out

    async def render(self, req, progress=None):
        if req.get("cacheOnly") or req.get("generationProfile") == "listener":
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
        if visual.get("mode") == "overShoulder":
            from .over_shoulder_video import render_over_shoulder
            return await render_over_shoulder(self, req, visual, progress)
        performance = self.performance_plan(project, req["revisionId"])
        performance_mode = req.get("performanceMode", "legacy")
        generation_profile = req.get("generationProfile", "accepted")
        if generation_profile not in PROFILES:
            raise RuntimeError("未知生成配置")
        reference = self.artifacts.by_id(req.get("motionReferenceArtifactId")) if req.get("motionReferenceArtifactId") else None
        if generation_profile in {"reference", "listener"} and (not reference or reference.get("fileHash") != self.artifacts._hash_file(reference["path"])):
            raise RuntimeError("动作参考缺失或完整性已改变")
        dialogue_plan = listening_plan(project, visual["id"], req.get("listeningSeed", 1)) if generation_profile == "dialogue" else None
        if dialogue_plan and req.get("listeningPlan") != dialogue_plan:
            raise RuntimeError("倾听动作计划与冻结输入不一致")
        automatic_references = []
        episode_camera_plan = camera_plan(project, req.get("cameraMode", "speaker"))
        workflow_template = json.loads(Path(__file__).with_name("multitalk_workflow.json").read_text(encoding="utf-8"))
        if visual.get("mode", "twoShot") != "twoShot":
            raise RuntimeError("当前真实渲染器需要双人同框场景，请选择同框机位")
        image = self._asset(visual["masterImage"].get("artifactId"))
        width, height = map(int, req.get("resolution", "854x480").split("x"))
        fps = int(req.get("fps", 25))
        if (width, height) not in {(854, 480), (1280, 720), (1920, 1080)} or fps not in {25, 30}:
            raise RuntimeError("不支持的导出规格")
        rate = int(timeline["sampleRate"])
        total = timeline["sampleCount"] / rate
        start = float(req.get("rangeStartSec", 0))
        end = float(req.get("rangeEndSec", total))
        if not 0 <= start < end <= total + 0.001:
            raise RuntimeError("非法生成区间")
        revision = next(r for r in project["scriptRevisions"] if r["id"] == req["revisionId"])
        speakers = {t["id"]: t["speaker"] for t in revision["turns"]}
        digest = hashlib.sha256(json.dumps(req, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        folder = self.artifacts.root / "video" / digest / uuid.uuid4().hex
        folder.mkdir(parents=True, exist_ok=True)
        motion_digest = hashlib.sha256(json.dumps({
            "audio": self.artifacts._hash_file(str(master)), "image": self.artifacts._hash_file(str(image)),
            "timeline": timeline, "speakers": speakers, "regions": visual["personRegions"],
            "workflow": workflow_template, "performance": performance,
            "strategy": "listening-v4-cfg2" if performance_mode == "legacy" else "listening-adaptive-v1",
            "implementation": LEGACY_IMPLEMENTATION if performance_mode == "legacy" else self.motion_implementation_hash,
            "seed": req.get("seed", 1),
            **({"listeningPlan": dialogue_plan, "dialogueSource": req.get("reuseMotionArtifactId")} if dialogue_plan else {}),
            **({"generationProfile": generation_profile, "profileImplementation": self.motion_implementation_hash} if generation_profile != "accepted" else {}),
            **({"motionReference": {"fileHash":reference["fileHash"], "params":reference["paramsSnapshot"]}} if reference else {}),
            **({"source": req["reuseMotionArtifactId"], "redoFromSec": req.get("redoFromSec")} if req.get("redoFromSec") is not None else {})}, sort_keys=True).encode()).hexdigest()
        motion_folder = self.artifacts.root / "video_motion" / motion_digest
        motion_folder.mkdir(parents=True, exist_ok=True)
        self.input_dir.mkdir(parents=True, exist_ok=True)
        # Normalize once, then split by exact sample offsets; gaps stay silent in both tracks.
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
        # Real masks follow the saved person regions, rather than rebinding both voices to a full image.
        for speaker in ("A", "B"):
            region = visual["personRegions"][speaker]
            self._mask_png(self.input_dir / f"{digest}_{speaker}.png", region)
        shutil.copyfile(image, self.input_dir / f"{digest}.png")
        (folder / "performance_plan.json").write_text(json.dumps(performance, ensure_ascii=False, indent=2), encoding="utf-8")
        segments = []
        stats = {"generated": 0, "composited": 0, "cached": 0, "retained": 0, "engineUsed": False}
        source_folder = None
        if req.get("reuseMotionArtifactId"):
            source = self.artifacts.by_id(req["reuseMotionArtifactId"])
            # The router validates the frozen voice/image input before this point.
            if not source or not Path(source["path"]).is_file():
                raise RuntimeError("原成片文件缺失，无法重做")
            manifest = Path(source["path"]).parent / "motion_manifest.json"
            if manifest.is_file():
                source_folder = Path(json.loads(manifest.read_text(encoding="utf-8"))["motionDirectory"])
            else:
                # Existing real renders stored their clip paths in clips.txt before
                # editing replaced it. Read the project/job manifest instead.
                source_project = source["paramsSnapshot"]["projectSnapshot"]
                adopted = source_project.get("outputManifest") or {}
                if req["reuseMotionArtifactId"] in adopted.get("artifactIds", []):
                    source_folder = Path(adopted["meta"]["motionDirectory"])
                if source_folder is None:
                    current_manifest = project.get("outputManifest") or {}
                    if req["reuseMotionArtifactId"] in current_manifest.get("artifactIds", []):
                        source_folder = Path(current_manifest["meta"]["motionDirectory"])
            if source_folder and not source_folder.resolve().is_relative_to((self.artifacts.root / "video_motion").resolve()):
                raise RuntimeError("动画目录超出工程产物目录")
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30, trust_env=False) as client:
            # Completed clips survive engine failure and are reused on a retry.
            # Preserve the reference chain even when a preview begins mid-episode.
            motion_start = 0
            cursor = motion_start
            while cursor < end - 0.001:
                stop = min(total, cursor + 5)
                shot_key = round(cursor*rate)*100000000 + round(stop*rate)
                clip = motion_folder / f"shot_{shot_key}.mp4"
                reference_hash = self.artifacts._hash_file(str(segments[-1])) if segments else self.artifacts._hash_file(str(image))
                marker = clip.with_suffix(".complete.json")
                valid = False
                retained = False
                automatic_id = None
                planned = next((p for p in dialogue_plan["segments"] if p["start"] == cursor), None) if dialogue_plan else None
                optimize = bool(planned and planned["eligible"] and (req.get("redoFromSec") is None or cursor >= req["redoFromSec"]))
                if source_folder and not optimize and (generation_profile == "dialogue" or req.get("redoFromSec") is None or cursor < req["redoFromSec"]):
                    original = source_folder / clip.name
                    original_marker = original.with_suffix(".complete.json")
                    if original.is_file() and original_marker.is_file():
                        saved = json.loads(original_marker.read_text(encoding="utf-8"))
                        if saved.get("referenceHash") == (self.artifacts._hash_file(str(source_folder / segments[-1].name)) if generation_profile == "dialogue" and segments else reference_hash) and saved.get("fileHash") == self.artifacts._hash_file(str(original)):
                            if original != clip:
                                shutil.copyfile(original, clip)
                                marker.write_text(json.dumps({"referenceHash":reference_hash,"fileHash":self.artifacts._hash_file(str(clip))}), encoding="utf-8")
                            retained = True
                if clip.exists() and marker.exists():
                    try:
                        saved = json.loads(marker.read_text(encoding="utf-8"))
                    except (ValueError, OSError):
                        saved = {}
                    valid = saved.get("referenceHash") == reference_hash and saved.get("fileHash") == self.artifacts._hash_file(str(clip))
                if progress:
                    progress(f"motion {cursor:.1f}–{stop:.1f}s: " + ("cached" if valid else "generating"))
                if not valid:
                    if req.get("cacheOnly"):
                        raise RuntimeError("已验证动画缺失或完整性改变，请重新生成对应片段")
                    if optimize:
                        original = source_folder / clip.name if source_folder else clip.with_name(clip.stem+"_speaking.mp4")
                        if source_folder:
                            saved = json.loads(original.with_suffix(".complete.json").read_text(encoding="utf-8"))
                            source_reference = self.artifacts._hash_file(str(source_folder / segments[-1].name)) if segments else self.artifacts._hash_file(str(image))
                            if saved.get("referenceHash") != source_reference or saved.get("fileHash") != self.artifacts._hash_file(str(original)):
                                raise RuntimeError("原说话者动画完整性已改变")
                        else:
                            await self._prepare_engine(client)
                            await self._shot(client, digest, shot_key, cursor, stop, rate, data, tracks, motion_folder, original,
                                segments[-1] if segments else None, performance, req.get("seed", 1), performance_mode, visual["personRegions"])
                            stats["generated"] += 1
                        await self._prepare_engine(client)
                        stats["engineUsed"] = True
                        if progress: progress(f"倾听动作 {cursor:.1f}–{stop:.1f}s: {planned['listener']} · {planned['label']}")
                        automatic = await self._automatic_reference(client, project, visual, planned, segments[-1] if segments else None, image)
                        automatic_id = automatic["artifactId"]
                        automatic_references.append(automatic_id)
                        await self._compose_listener(automatic, original, clip, visual["personRegions"], cursor, stop)
                        stats["composited"] += 1
                    elif generation_profile == "listener":
                        original = source_folder / clip.name if source_folder else None
                        if not original or not original.is_file() or not original.with_suffix(".complete.json").is_file():
                            raise RuntimeError("原说话者动画缺失")
                        saved = json.loads(original.with_suffix(".complete.json").read_text(encoding="utf-8"))
                        if saved.get("referenceHash") != reference_hash or saved.get("fileHash") != self.artifacts._hash_file(str(original)):
                            raise RuntimeError("原说话者动画完整性已改变")
                        await self._compose_listener(reference, original, clip, visual["personRegions"], cursor, stop)
                        stats["composited"] += 1
                    else:
                        if not stats["engineUsed"]:
                            await self._prepare_engine(client)
                            stats["engineUsed"] = True
                        await self._shot(client, digest, shot_key, cursor, stop, rate, data, tracks, motion_folder, clip,
                                         segments[-1] if segments else None, performance, req.get("seed", 1),
                                         performance_mode, visual["personRegions"], "accepted" if generation_profile == "dialogue" else generation_profile, reference)
                        stats["generated"] += 1
                    marker.write_text(json.dumps({"referenceHash": reference_hash, "fileHash": self.artifacts._hash_file(str(clip)), "automaticReferenceArtifactId": automatic_id}), encoding="utf-8")
                else:
                    stats["retained" if retained else "cached"] += 1
                    if optimize and saved.get("automaticReferenceArtifactId"):
                        automatic_references.append(saved["automaticReferenceArtifactId"])
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
        if start == motion_start and abs(cursor-end) < 0.001:
            shutil.copyfile(raw_base, base)
        else:
            await self._command("ffmpeg", "-v", "error", "-y", "-ss", start-motion_start,
                "-i", raw_base, "-t", end-start, "-an", "-c:v", "libx264", "-crf", 19, base)
        # Cut among the genuine moving two-shot and speaker close-ups.
        edited = []
        for n, shot in enumerate(clipped_camera_plan(episode_camera_plan, start, end)):
            a, b, active = shot["start"], shot["end"], shot["camera"]
            crop = ""
            if active in ("A", "B"):
                box = crop_region(visual["personRegions"][active])
                crop = f"crop=iw*{box['width']}:ih*{box['height']}:iw*{box['x']}:ih*{box['y']},"
            part = folder / f"edit_{n:04d}.mp4"
            first_frame = round((a-start)*fps)
            last_frame = round((b-start)*fps)
            if last_frame <= first_frame:
                continue
            await self._command("ffmpeg", "-v", "error", "-y", "-reinit_filter", 0, "-i", base,
                "-frames:v", last_frame-first_frame, "-an", "-vf", f"fps={fps},trim=start_frame={first_frame}:end_frame={last_frame},setpts=PTS-STARTPTS," + crop + f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1",
                "-r", fps, "-c:v", "libx264", "-preset", "fast", "-crf", 19, "-pix_fmt", "yuv420p", part)
            edited.append(part)
        concat.write_text("\n".join("file '" + p.as_posix().replace("'", "'\\''") + "'" for p in edited), encoding="utf-8")
        final = folder / "final.mp4"
        await self._command("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", 0,
            "-i", concat, "-ss", start, "-i", pcm, "-t", end-start,
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", final)
        probe = json.loads(await self._command("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", final))
        if not any(s["codec_type"] == "audio" for s in probe["streams"]) or abs(float(probe["format"]["duration"]) - (end-start)) > 0.2:
            raise RuntimeError("成片音轨或时长校验失败，未登记输出")
        quality = await self.motion_quality(base, performance, start, end)
        (folder / "quality_report.json").write_text(json.dumps(quality, indent=2), encoding="utf-8")
        stream = next((v for v in probe["streams"] if v["codec_type"] == "video"), None)
        if not stream or (stream.get("width"), stream.get("height")) != (width, height):
            raise RuntimeError("成片分辨率校验失败，未登记输出")
        try:
            validate_video_stream(stream, end-start, fps)
        except ValueError as exc:
            raise RuntimeError(str(exc)+"，未登记输出") from exc
        artifact_id = "VID-" + uuid.uuid4().hex
        motion_manifest = {"motionDirectory": str(motion_folder), "stats": stats,
                           "performanceMode": performance_mode, "cameraPlan": episode_camera_plan}
        (folder / "motion_manifest.json").write_text(json.dumps(motion_manifest, indent=2), encoding="utf-8")
        self.artifacts.register(artifact_id, "video", str(final), digest, req, "multitalk-dual-v1")
        return {"artifactIds": [artifact_id], "meta": {"durationSec": end-start,
            "resolution": f"{width}x{height}", "fps": fps, "path": str(final), "motionDirectory": str(motion_folder), "performancePlan": performance,
            "performanceMode": performance_mode, "generationProfile": generation_profile, "motionReferenceArtifactId": req.get("motionReferenceArtifactId"), "cameraPlan": episode_camera_plan, "generationStats": stats,
            "retainedPerformance": "source" if stats["retained"] else None,
            "listeningPlan": dialogue_plan, "automaticReferenceArtifactIds": automatic_references,
            "cameraMode": req.get("cameraMode", "speaker"), "qualityStatus": "needs_review", "qualityReport": quality}}

    async def _compose_listener(self, reference, original, clip, regions, start, end):
        params = reference["paramsSnapshot"]
        if start != params.get("rangeStartSec") or abs(end-params.get("rangeEndSec", -1)) > 0.001:
            raise RuntimeError("动作参考与合成片段区间不一致")
        filters = listener_layer_filter(params["listenerSpeaker"], params["blendBand"], regions)
        probe = json.loads(await self._command("ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_streams", "-of", "json", original))["streams"][0]
        # Preserve the original segment's color metadata: a change resets ffmpeg's
        # fps/trim filters when concatenated streams are decoded later.
        colors = ["-color_range", probe.get("color_range", "unknown"),
                  "-colorspace", probe.get("color_space", "unspecified"),
                  "-color_trc", probe.get("color_transfer", "unspecified"),
                  "-color_primaries", probe.get("color_primaries", "unspecified")]
        color_range = {"tv": "limited", "pc": "full"}.get(probe.get("color_range"), "unspecified")
        filters = filters.removesuffix("[v]") + "[mixed];[mixed]setparams=" + ":".join([
            "range="+color_range, "color_primaries="+probe.get("color_primaries", "unknown"),
            "color_trc="+probe.get("color_transfer", "unknown"),
            "colorspace="+probe.get("color_space", "unknown")]) + "[v]"
        await self._command("ffmpeg", "-v", "error", "-y", "-ss", 0.32, "-i", reference["path"],
            "-i", original, "-filter_complex", filters, "-map", "[v]", "-t", end-start,
            "-frames:v", max(1, round((end-start)*25)),
            "-an", "-c:v", "libx264", "-crf", 19, "-pix_fmt", "yuv420p", *colors, clip.with_suffix(".partial.mp4"))
        clip.with_suffix(".partial.mp4").replace(clip)

    async def _prepare_engine(self, client):
        await self._health(client)
        queue = (await client.get("/queue")).json()
        if queue.get("queue_running") or queue.get("queue_pending"):
            # A restart may be reattaching to an ongoing model task. Never free it.
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

    async def motion_quality(self, video, plan, start, end):
        report = {"method": "region_frame_difference", "automaticAcceptance": False, "listeners": []}
        for part in plan:
            a, b = max(start, part["start"]), min(end, part["end"])
            if b-a < 1:
                continue
            listener = part["listener"]
            x = "0" if listener == "A" else "iw/2"
            raw = await self._command("ffmpeg", "-v", "error", "-ss", a-start, "-i", video,
                "-t", b-a, "-vf", f"crop=iw/2:ih:{x}:0,fps=10,scale=96:96,format=gray", "-f", "rawvideo", "-")
            size = 96*96
            frames = [raw[i:i+size] for i in range(0, len(raw)-size+1, size)]
            differences = [sum(abs(x-y) for x,y in zip(f,g))/size for f,g in zip(frames,frames[1:])]
            report["listeners"].append({"speaker": listener, "start": a, "end": b,
                "meanDifference": sum(differences)/len(differences) if differences else 0,
                "repeatedPairs": sum(d == 0 for d in differences), "frames": len(frames),
                "requiresVisualReview": True})
        return report

    @staticmethod
    def performance_plan(project, revision_id):
        timeline = project["audioTimeline"]
        revision = next(r for r in project["scriptRevisions"] if r["id"] == revision_id)
        speakers = {t["id"]: t["speaker"] for t in revision["turns"]}
        return [{"start": timeline["unitOffsets"][u["id"]] / timeline["sampleRate"],
                 "end": (timeline["unitOffsets"][u["id"]] + u["sampleCount"]) / timeline["sampleRate"],
                 "speaker": speakers[u["turnId"]], "listener": "B" if speakers[u["turnId"]] == "A" else "A",
                 "listenerState": "attentive", "mouth": "closed", "gaze": "speaker", "motion": "gentle_nod_and_posture"}
                for u in timeline["units"]]

    @staticmethod
    def _mask_png(path, region):
        width, height = 864, 480
        x, y, w, h = (float(region[k]) for k in ("x", "y", "width", "height"))
        if not all(math.isfinite(v) for v in (x, y, w, h)) or not (0 <= x < x+w <= 1 and 0 <= y < y+h <= 1):
            raise RuntimeError("人物区域必须为画面内有效矩形")
        left, right, top, bottom = int(x*width), int((x+w)*width), int(y*height), int((y+h)*height)
        rows = b"".join(b"\0" + (b"\0"*left + b"\xff"*(right-left) + b"\0"*(width-right)
            if top <= row < bottom else b"\0"*width) for row in range(height))
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind+data))
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
                         + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))

    @staticmethod
    async def _health(client):
        try:
            # system_stats performs CUDA memory queries and may wait for an active
            # kernel; queue is a CPU-only liveness endpoint during sampling.
            response = await client.get("/queue")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError("数字人引擎未启动或已崩溃，请查看引擎日志后重试；已完成镜头会保留") from exc

    async def _shot(self, client, digest, index, start, end, rate, data, tracks, folder, clip, previous_clip=None, performance=None, seed=1, performance_mode="legacy", regions=None, generation_profile="accepted", reference=None, camera=None):
        tag = f"{digest}_{index:04d}"
        for kind, track in (("a", tracks["A"]), ("b", tracks["B"]), ("mix", data)):
            with wave.open(str(self.input_dir / f"{tag}_{kind}.wav"), "wb") as writer:
                writer.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                # Silent warm-up prevents the model's unstable initial frames entering the edit.
                writer.writeframes(bytes(round(0.32*rate)*2) + bytes(track[round(start*rate)*2:round(end*rate)*2]))
        wf = json.loads(Path(__file__).with_name("multitalk_workflow.json").read_text(encoding="utf-8"))
        reference_filename = None
        if reference:
            params = reference["paramsSnapshot"]
            if start != params.get("rangeStartSec") or abs(end-params.get("rangeEndSec", -1)) > 0.001:
                raise RuntimeError("动作参考与片段区间不一致")
            reference_filename = f"{tag}_motion.mp4"
            # VHS lazily reads audio even when only its image output is connected.
            # Silent I2V references otherwise crash the engine worker on completion.
            await self._prepare_motion_reference(reference["path"], self.input_dir / reference_filename)
        configure_generation_profile(wf, generation_profile, reference_filename)
        wf["133"]["inputs"]["image"] = f"{digest}.png"
        if previous_clip:
            reference = self.input_dir / f"{tag}_reference.png"
            await self._command("ffmpeg", "-v", "error", "-y", "-sseof", -0.04, "-i", previous_clip,
                                "-frames:v", 1, reference)
            if not reference.is_file():
                raise RuntimeError("无法读取前一镜头的末帧，未开始下一镜头")
            wf["133"]["inputs"]["image"] = reference.name
        roles = " ".join(
            f"From {max(0, part['start']-start):.1f} to {min(end, part['end'])-start:.1f} seconds, "
            f"{('the man on the left' if part['speaker']=='A' else 'the woman on the right')} speaks; "
            f"{('the woman on the right' if part['speaker']=='A' else 'the man on the left')} listens with closed lips, "
            "turns toward the speaker, gently nods once and adjusts shoulders naturally."
            for part in (performance or []) if part['end'] > start and part['start'] < end)
        wf["135"]["inputs"]["positive_prompt"] = (
            "The woman on the right immediately turns her head left toward the man and gives a gentle nod while he talks. "
            "The man turns toward her and nods when she speaks. A continuous live-action podcast conversation. "
            "The silent listener actively reacts from the beginning: a natural head turn toward the speaker, "
            "gentle nod, blinking, breathing and subtle shoulder movement, with lips closed. "
            "The speaking host makes conversational hand gestures. Neither person holds a frozen portrait pose. "
            "Preserve identities, clothing, microphones, hands and stable studio. " + roles)
        wf["199"]["inputs"]["seed"] = int(seed)
        if performance_mode == "adaptive":
            wf["135"]["inputs"]["positive_prompt"] = adaptive_prompt(performance or [], regions, start, end)
        wf["199"]["inputs"]["cfg"] = 2.0
        wf["250"]["inputs"]["image"] = f"{digest}_A.png"
        wf["252"]["inputs"]["image"] = f"{digest}_B.png"
        for node, kind in (("218", "a"), ("241", "b"), ("242", "mix")):
            wf[node]["inputs"]["audio"] = f"{tag}_{kind}.wav"
        if camera:
            from .over_shoulder_video import configure_camera_workflow
            configure_camera_workflow(wf, camera)
        frame_count = 4*math.ceil(((end-start)+0.32)*25/4)+1
        wf["223"]["inputs"] = {"expression": str(frame_count)}
        wf["192"]["inputs"]["frame_window_size"] = frame_count
        frames = folder / f"frames_{index:04d}"
        frames.mkdir(exist_ok=True)
        wf["192"]["inputs"]["output_path"] = str(frames)
        wf["229"]["inputs"]["filename_prefix"] = f"completion/{tag}"
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
            if not history: continue
            if history.get("status", {}).get("status_str") != "success":
                raise RuntimeError("数字人采样失败：" + json.dumps(history.get("status"), ensure_ascii=False)[-2500:])
            candidates = sorted(frames.rglob("frame_00000.png"))
            if not candidates:
                raise RuntimeError("引擎未保存真实动画帧；完成标记不能作为视频")
            source = candidates[-1].parent
            if len(list(source.glob("frame_*.png"))) < math.floor(((end-start)+0.32)*25):
                raise RuntimeError("动画帧不足，未采用该镜头")
            await self._command("ffmpeg", "-v", "error", "-y", "-framerate", 25,
                "-i", source / "frame_%05d.png", "-ss", 0.32, "-t", end-start, "-an", "-c:v", "libx264",
                "-crf", 19, "-pix_fmt", "yuv420p", clip.with_suffix(".partial.mp4"))
            clip.with_suffix(".partial.mp4").replace(clip)
            return
        raise RuntimeError("镜头生成超时；已完成镜头保留，可重试")

    async def _automatic_reference(self, client, project, visual, planned, previous, image):
        anchor_source = previous or image
        params = {"purpose": "motionReference", "projectId": project["id"],
            "visualArtifactId": visual["masterImage"]["artifactId"], "rangeStartSec": planned["start"],
            "rangeEndSec": planned["end"], "listenerSpeaker": planned["listener"],
            "blendBand": planned["blendBand"], "label": planned["label"], "plannedReaction": planned,
            "anchorHash": self.artifacts._hash_file(str(anchor_source)), "implementation": self.motion_implementation_hash}
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        cached = self.artifacts.by_hash(key)
        if cached and Path(cached["path"]).is_file() and cached["fileHash"] == self.artifacts._hash_file(cached["path"]):
            return cached
        root = self.artifacts.root / "motion_references"
        root.mkdir(exist_ok=True)
        anchor = self.input_dir / f"listening_{key}.png"
        if previous:
            await self._command("ffmpeg", "-v", "error", "-y", "-sseof", -0.04, "-i", previous, "-frames:v", 1, anchor)
        else:
            shutil.copyfile(image, anchor)
        template = json.loads(Path(__file__).with_name("multitalk_workflow.json").read_text(encoding="utf-8"))
        workflow = reference_workflow(template, anchor.name, planned, f"completion/listening_{key}")
        record = root / f"{key}.job.json"
        prompt = None
        if record.is_file():
            saved = json.loads(record.read_text(encoding="utf-8"))
            history = (await client.get(f"/history/{saved['promptId']}")).json().get(saved["promptId"])
            queue = (await client.get("/queue")).json()
            queued = any(item[1] == saved["promptId"] for item in queue.get("queue_running", [])+queue.get("queue_pending", []))
            if saved.get("workflow") == workflow and (history or queued):
                prompt = saved["promptId"]
        if prompt is None:
            response = await client.post("/prompt", json={"prompt": workflow, "client_id": "listening_"+key})
            response.raise_for_status()
            submitted = response.json()
            if submitted.get("node_errors"):
                raise RuntimeError("倾听动作工作流不可用："+str(submitted["node_errors"]))
            prompt = submitted["prompt_id"]
            temporary = record.with_suffix(".partial.json")
            temporary.write_text(json.dumps({"promptId": prompt, "workflow": workflow}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(record)
        deadline = time.monotonic()+3600
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            await self._health(client)
            history = (await client.get(f"/history/{prompt}")).json().get(prompt)
            if not history: continue
            if history.get("status", {}).get("status_str") != "success":
                raise RuntimeError("倾听动作生成失败："+str(history.get("status")))
            videos = history.get("outputs", {}).get("229", {}).get("gifs", [])
            video = next((v for v in videos if v.get("filename", "").endswith(".mp4")), None)
            if not video or Path(video["filename"]).name != video["filename"]:
                raise RuntimeError("倾听动作没有真实视频输出")
            response = await client.get("/view", params={k:video[k] for k in ("filename", "subfolder", "type")})
            response.raise_for_status()
            target = root / f"{key}-{uuid.uuid4().hex}.mp4"
            temporary = target.with_suffix(".partial.mp4")
            temporary.write_bytes(response.content)
            probe = json.loads(await self._command("ffprobe", "-v", "error", "-show_streams", "-of", "json", temporary))
            stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
            if float(stream.get("duration", 0)) < planned["end"]-planned["start"]+0.32-0.04:
                raise RuntimeError("倾听动作视频短于计划")
            temporary.replace(target)
            params["sourcePromptId"] = prompt
            return self.artifacts.register("VID-REF-"+uuid.uuid4().hex, "video", str(target), key, params, "dialogue-listening-v1")
        raise RuntimeError("倾听动作生成超时，可通过同一计划重试")

    async def _prepare_motion_reference(self, source, target):
        await self._command("ffmpeg", "-v", "error", "-y", "-i", source,
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-map", "0:v:0",
            "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-shortest", target)
