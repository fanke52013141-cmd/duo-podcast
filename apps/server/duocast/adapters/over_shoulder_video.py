"""Two genuine camera images, single-subject animation, sample-clock editing."""
import hashlib
import json
import math
import shutil
import uuid
import wave

import httpx

from ..services.over_shoulder import camera_pair, shot_plan, shot_chunks
from ..services.generation_profiles import validate_video_stream


def read_marker(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


async def validate_native_clip(provider, clip, duration):
    probe = json.loads(await provider._command("ffprobe", "-v", "error", "-show_streams", "-of", "json", clip))
    stream = next((s for s in probe["streams"] if s["codec_type"] == "video"), None)
    if not stream or stream.get("r_frame_rate") != "25/1" or not math.floor(duration * 25) <= int(stream.get("nb_frames", 0)) <= math.ceil(duration * 25):
        raise RuntimeError("过肩机位动画帧数不足或时间轴异常，未采用该片段")
    if abs(float(stream.get("duration", 0)) - duration) > .041:
        raise RuntimeError("过肩机位动画时长异常，未采用该片段")


async def smooth_continuation(provider, previous, part, folder, index, count, fps, width, height):
    """Bridge the pose discontinuity with motion interpolation, without a dissolve."""
    frames = min(count - 2, max(2, round(.12 * fps)))
    if frames < 2:
        return 0
    images = folder / f"continuity_{index}"
    images.mkdir()
    await provider._command("ffmpeg", "-v", "error", "-y", "-sseof", -1 / fps, "-i", previous,
        "-vf", "scale=854:480", "-frames:v", 1, images / "frame_0000.png")
    shutil.copyfile(images / "frame_0000.png", images / "frame_0001.png")
    await provider._command("ffmpeg", "-v", "error", "-y", "-i", part,
        "-vf", f"select='between(n,{frames},{frames + 1})',scale=854:480", "-vsync", 0,
        "-frames:v", 2, "-start_number", 2, images / "frame_%04d.png")
    shutil.copyfile(images / "frame_0003.png", images / "frame_0004.png")
    bridge = images / "bridge.mp4"
    await provider._command("ffmpeg", "-v", "error", "-y", "-framerate", f"{fps}/{frames}",
        "-i", images / "frame_%04d.png", "-vf",
        f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,trim=start_frame={frames}:end_frame={2 * frames},setpts=PTS-STARTPTS",
        "-frames:v", frames, "-an", "-c:v", "libx264", "-crf", 19, "-pix_fmt", "yuv420p", bridge)
    stream = next(s for s in json.loads(await provider._command("ffprobe", "-v", "error", "-show_streams", "-of", "json", bridge))["streams"] if s["codec_type"] == "video")
    validate_video_stream(stream, frames / fps, fps)
    smoothed = part.with_suffix(".smooth.mp4")
    await provider._command("ffmpeg", "-v", "error", "-y", "-i", bridge, "-i", part, "-filter_complex",
        f"[0:v]scale={width}:{height},setsar=1[bridge];[1:v]trim=start_frame={frames},setpts=PTS-STARTPTS[tail];[bridge][tail]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-frames:v", count, "-an", "-c:v", "libx264", "-preset", "fast", "-crf", 19, "-pix_fmt", "yuv420p", smoothed)
    smoothed.replace(part)
    return frames


async def continuity_measure(provider, previous, part, fps, region):
    """Measure the whole bridge window, so a delayed jump remains visible."""
    x, y, w, h = (region[k] for k in ("x", "y", "width", "height"))
    transform = f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale=96:54,format=gray"
    tail = await provider._command("ffmpeg", "-v", "error", "-sseof", -1 / fps, "-i", previous,
        "-vf", transform, "-frames:v", 1, "-f", "rawvideo", "pipe:1")
    head = await provider._command("ffmpeg", "-v", "error", "-i", part, "-vf", transform,
        "-frames:v", round(.12 * fps) + 2, "-f", "rawvideo", "pipe:1")
    size = 96 * 54
    if len(tail) != size or len(head) < 2 * size or len(head) % size:
        raise RuntimeError("续接质量检查未取得完整帧")
    sequence = [tail] + [head[i:i + size] for i in range(0, len(head), size)]
    differences = [sum(abs(a - b) for a, b in zip(left, right)) / size
                   for left, right in zip(sequence, sequence[1:])]
    return {"boundaryMeanGrayChange": differences[0], "windowMaxMeanGrayChange": max(differences)}


def configure_camera_workflow(workflow, camera):
    subject = camera["subjectSpeaker"]
    workflow["198"]["inputs"]["audio_1"] = ["218" if subject == "A" else "241", 0]
    workflow["198"]["inputs"].pop("audio_2", None)
    workflow["198"]["inputs"]["ref_target_masks"] = ["251", 0]
    workflow["135"]["inputs"]["positive_prompt"] = (
        "A live-action over-the-shoulder podcast conversation, fixed camera. "
        f"The sharply focused {'man' if subject == 'A' else 'woman'} speaks naturally to the opposite host, "
        "with synchronized lips, expressive eyes, natural blinking, breathing, subtle head movement "
        "and small conversational hand gestures. Maintain eye contact toward the opposite host. "
        "The blurred foreground host shows only back of head and shoulder, with subtle breathing "
        "and occasional small listening movement, face remains hidden. Preserve both identities, "
        "clothing, microphone, anatomy, studio, framing and camera angle. No camera movement.")


async def render_over_shoulder(provider, req, visual, progress):
    if req.get("generationProfile", "accepted") != "accepted" or req.get("reuseMotionArtifactId"):
        raise RuntimeError("过肩机位使用标准生成配置和自动镜头缓存")
    width, height = map(int, req.get("resolution", "854x480").split("x"))
    fps = int(req.get("fps", 25))
    if (width, height) not in {(854, 480), (1280, 720), (1920, 1080)} or fps not in {25, 30}:
        raise RuntimeError("不支持的导出规格")
    project = req["projectSnapshot"]
    pair = camera_pair(visual)
    plan = shot_plan(project, visual)
    timeline = project["audioTimeline"]
    rate, total_samples = timeline["sampleRate"], timeline["sampleCount"]
    total = total_samples / rate
    start, end = req.get("rangeStartSec", 0), req.get("rangeEndSec", total)
    if not 0 <= start < end <= total + .001:
        raise RuntimeError("非法过肩生成区间")
    master = provider._asset(timeline["masterAudioAssetId"])
    images = {speaker: provider._asset(camera["masterImage"]["artifactId"]) for speaker, camera in pair.items()}
    image_hashes = {speaker: provider.artifacts._hash_file(str(path)) for speaker, path in images.items()}
    for speaker, camera in pair.items():
        if camera["masterImage"].get("fileHash") != image_hashes[speaker]:
            raise RuntimeError("机位图片完整性已改变，请重新上传")
    digest = hashlib.sha256(json.dumps(req, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    folder = provider.artifacts.root / "video" / digest / uuid.uuid4().hex
    folder.mkdir(parents=True)
    motion_digest = hashlib.sha256(json.dumps({"strategy": "ots-single-subject-v1", "audio": provider.artifacts._hash_file(str(master)),
        "images": image_hashes, "cameras": pair, "plan": plan, "seed": req.get("seed", 1),
        "implementation": provider.motion_implementation_hash}, sort_keys=True).encode()).hexdigest()
    motion = provider.artifacts.root / "video_motion" / motion_digest
    motion.mkdir(parents=True, exist_ok=True)
    pcm = folder / "master.wav"
    await provider._command("ffmpeg", "-v", "error", "-y", "-i", master, "-ar", rate, "-ac", 1, "-c:a", "pcm_s16le", pcm)
    with wave.open(str(pcm)) as reader:
        data = reader.readframes(reader.getnframes())
    if len(data) // 2 < total_samples:
        raise RuntimeError("母轨文件短于权威时间轨")
    tracks = {s: bytearray(len(data)) for s in pair}
    revision = next(r for r in project["scriptRevisions"] if r["id"] == timeline["revisionId"])
    speakers = {t["id"]: t["speaker"] for t in revision["turns"]}
    for unit in timeline["units"]:
        offset = timeline["unitOffsets"][unit["id"]]
        stop = offset + unit["sampleCount"]
        tracks[speakers[unit["turnId"]]][offset * 2:stop * 2] = data[offset * 2:stop * 2]
    stats = {"generated": 0, "cached": 0, "retained": 0, "composited": 0, "engineUsed": False}
    rendered = []
    # Always generate canonical segments from the same turn boundaries. Preview
    # and full export share these immutable clips, independent of preview range.
    async with httpx.AsyncClient(base_url=provider.base_url, timeout=30, trust_env=False) as client:
        for shot in plan:
            previous = None
            for cursor, stop in shot_chunks(shot, rate):
                a, b = cursor / rate, stop / rate
                if b <= start or a >= end:
                    continue
                speaker = shot["speaker"]
                tag = f"{motion_digest}_{speaker}"
                clip = motion / f"shot_{cursor}_{stop}_{speaker}.mp4"
                marker = clip.with_suffix(".complete.json")
                saved = read_marker(marker)
                valid = clip.is_file() and saved.get("fileHash") == provider.artifacts._hash_file(str(clip))
                if valid:
                    try:
                        await validate_native_clip(provider, clip, b - a)
                    except RuntimeError:
                        valid = False
                if progress: progress(f"过肩 {speaker} 发言机位 {a:.2f}–{b:.2f}s：" + ("复用" if valid else "生成"))
                if not valid:
                    if req.get("cacheOnly"):
                        raise RuntimeError("机位动画缓存缺失，请重新生成")
                    if not stats["engineUsed"]:
                        await provider._prepare_engine(client)
                        stats["engineUsed"] = True
                    shutil.copyfile(images[speaker], provider.input_dir / f"{tag}.png")
                    provider._mask_png(provider.input_dir / f"{tag}_A.png", pair[speaker]["subjectRegion"])
                    provider._mask_png(provider.input_dir / f"{tag}_B.png", pair[speaker]["subjectRegion"])
                    await provider._shot(client, tag, cursor, a, b, rate, data, tracks, motion, clip,
                        previous_clip=previous, seed=req.get("seed", 1), camera=pair[speaker])
                    await validate_native_clip(provider, clip, b - a)
                    temporary = marker.with_suffix(".partial.json")
                    temporary.write_text(json.dumps({"fileHash": provider.artifacts._hash_file(str(clip)), "cameraAssetId": shot["cameraAssetId"]}), encoding="utf-8")
                    temporary.replace(marker)
                    stats["generated"] += 1
                else:
                    stats["cached"] += 1
                rendered.append((clip, a, b, shot))
                previous = clip
    edits, camera_plan, transitions = [], [], []
    if progress: progress("过肩：合并配音并校验声画")
    for n, (clip, a, b, shot) in enumerate(rendered):
        left, right = max(a, start), min(b, end)
        count = round((right - start) * fps) - round((left - start) * fps)
        if count <= 0: continue
        part = folder / f"edit_{n}.mp4"
        # Sub-frame quantization at a turn boundary may need one held frame.
        # This is bounded to one native frame; the final stream is validated.
        transform = f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,tpad=stop_mode=clone:stop_duration=0.04"
        same_camera = bool(edits and camera_plan[-1]["cameraAssetId"] == shot["cameraAssetId"] and abs(camera_plan[-1]["end"] - left) < .001)
        await provider._command("ffmpeg", "-v", "error", "-y", "-ss", left - a, "-i", clip,
            "-an", "-vf", transform, "-frames:v", count, "-c:v", "libx264", "-preset", "fast", "-crf", 19, "-pix_fmt", "yuv420p", part)
        if same_camera:
            before = await continuity_measure(provider, edits[-1], part, fps, pair[shot["speaker"]]["subjectRegion"])
            frames = await smooth_continuation(provider, edits[-1], part, folder, n, count, fps, width, height)
            if frames:
                after = await continuity_measure(provider, edits[-1], part, fps, pair[shot["speaker"]]["subjectRegion"])
                transitions.append({"atSec": left - start, "timelineAtSec": left,
                    "frameAtSec": round((left - start) * fps) / fps,
                    "frames": frames, "cameraAssetId": shot["cameraAssetId"], "method": "opticalFlow",
                    "qualityCheck": {"before": before, "after": after,
                        "needsReview": after["windowMaxMeanGrayChange"] > before["windowMaxMeanGrayChange"] + .5,
                        "metric": "subjectGrayDifference96x54", "regressionTolerance": .5}})
        edits.append(part)
        camera_plan.append({"start": left, "end": right, "camera": shot["speaker"], "cameraAssetId": shot["cameraAssetId"], "mode": "overShoulder"})
    concat = folder / "clips.txt"
    concat.write_text("\n".join("file '" + p.as_posix().replace("'", "'\\''") + "'" for p in edits), encoding="utf-8")
    final = folder / "final.mp4"
    await provider._command("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", 0, "-i", concat,
        "-ss", start, "-i", pcm, "-t", end - start, "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", final)
    probe = json.loads(await provider._command("ffprobe", "-v", "error", "-show_streams", "-of", "json", final))
    stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
    validate_video_stream(stream, end - start, fps)
    if not any(s["codec_type"] == "audio" and abs(float(s.get("duration", 0)) - (end - start)) < .1 for s in probe["streams"]):
        raise RuntimeError("过肩成片音轨时长校验失败")
    meta = {"path": str(final), "durationSec": end - start, "resolution": f"{width}x{height}", "fps": fps,
        "motionDirectory": str(motion), "generationStats": stats, "cameraPlan": camera_plan,
        "cameraMode": req.get("cameraMode", "speaker"), "visualMode": "overShoulder", "generationProfile": "accepted", "qualityStatus": "needs_review", "continuityTransitions": transitions}
    (folder / "motion_manifest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    artifact_id = "VID-" + uuid.uuid4().hex
    provider.artifacts.register(artifact_id, "video", str(final), digest, req, "ots-single-subject-v1")
    if progress: progress("过肩视频校验完成")
    return {"artifactIds": [artifact_id], "meta": meta}
