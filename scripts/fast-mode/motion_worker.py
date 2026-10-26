"""Pinned full-frame motion rendering. Importing this module needs no GPU libs.

Two-shot roles are composited using explicit disjoint body rectangles, never
by stacking two complete scenes. OTS keeps the original foreground untouched.
"""
import hashlib
import json
import math
import re
import subprocess
import time
import uuid
from collections import OrderedDict
from pathlib import Path


def _int(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("Expected bounded integer frame/dimension")
    return value


def region_box(region, width, height):
    if not isinstance(region, dict) or set(region) != {"x", "y", "width", "height"}:
        raise ValueError("Explicit normalized bodyRegion required")
    x, y, w, h = (region[k] for k in ("x", "y", "width", "height"))
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (x, y, w, h)):
        raise ValueError("Invalid bodyRegion numbers")
    if not (0 <= x < x+w <= 1 and 0 <= y < y+h <= 1):
        raise ValueError("bodyRegion outside frame")
    box = (math.floor(x*width), math.floor(y*height), math.ceil((x+w)*width), math.ceil((y+h)*height))
    return box


def disjoint(first, second):
    return first[2] <= second[0] or second[2] <= first[0] or first[3] <= second[1] or second[3] <= first[1]


def validate_request(request):
    """Pure CPU contract: exact clocks, visible roles, source indices and geometry."""
    assets = request["motionAssets"]
    plan, cameras, clips = assets["plan"], assets["cameras"], assets["clips"]
    count = _int(plan["totalFrames"], 1)
    samples, rate = _int(request["sampleCount"], 1), _int(request["sampleRate"], 1)
    if (count > 7500 or rate > 384000 or samples > 300*rate or plan["fps"] != 25
            or count != (samples*25+rate-1)//rate or plan["totalSamples"] != samples or plan["sampleRate"] != rate):
        raise ValueError("Motion plan does not match bounded sample clock")
    if not 1 <= len(plan["cameras"]) <= 8 or len(clips) > 128:
        raise ValueError("Too many motion assets")
    expanded, geometry = {}, {}
    for planned in plan["cameras"]:
        camera_id = planned["cameraId"]
        if not isinstance(camera_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", camera_id) or camera_id in expanded:
            raise ValueError("Invalid/duplicate camera identifier")
        camera = cameras[camera_id]
        if camera["mode"] == "twoShot":
            roles = {"A", "B"}
        elif camera["mode"] == "overShoulder" and camera.get("subjectSpeaker") in ("A", "B"):
            roles = {camera["subjectSpeaker"]}
        else:
            raise ValueError("Unknown camera mode or subject")
        if set(planned["roles"]) != roles:
            raise ValueError("Only visible camera subjects may be driven")
        sequences, dimensions, role_boxes = {}, set(), {}
        for role, segments in planned["roles"].items():
            sequence, cursor, boxes = [], 0, set()
            if not segments or len(segments) > count:
                raise ValueError("Missing or excessive segments")
            for segment in segments:
                start, end = _int(segment["outputStartFrame"]), _int(segment["outputEndFrame"], 1)
                if start != cursor or not start < end <= count or segment.get("direction") != 1:
                    raise ValueError("Segment gap/overlap/reverse playback")
                clip_id = segment["clipId"]
                clip = clips[clip_id]
                if (clip.get("role") != role or clip.get("cameraId") != camera_id
                        or clip.get("state") != segment["state"]):
                    raise ValueError("Clip role/camera/state binding mismatch")
                width, height, frames, fps = (_int(clip[k], 1) for k in ("width", "height", "frames", "fps"))
                if width*height > 4096*2160 or width % 2 or height % 2 or frames > 36000 or fps > 120:
                    raise ValueError("Native media exceeds worker limits")
                dimensions.add((width, height, fps))
                if clip["fileHash"] != segment["fileHash"] or not re.fullmatch(r"[a-f0-9]{64}", clip["fileHash"]):
                    raise ValueError("Clip/plan hash mismatch")
                indices = segment["sourceFrames"]
                if not isinstance(indices, list) or len(indices) != end-start:
                    raise ValueError("Incomplete exact source frame mapping")
                if (not indices or segment.get("sourceStartFrame") != indices[0]
                        or segment.get("sourceEndFrame") != indices[-1]+1
                        or indices != [indices[0]+i*fps//25 for i in range(len(indices))]):
                    raise ValueError("Source mapping is not forward native-rate playback")
                if segment["state"] not in ("speaking", "listening", "responding"):
                    raise ValueError("Unknown motion state")
                coordinates = clip["coordinates"]
                if not isinstance(coordinates, list) or len(coordinates) != frames:
                    raise ValueError("Incomplete confirmed face coordinates")
                body = region_box(clip.get("bodyRegion"), width, height)
                boxes.add(body)
                for index in indices:
                    usable_start, usable_end = _int(clip["usableStartFrame"]), _int(clip["usableEndFrame"], 1)
                    if not 0 <= usable_start < usable_end <= frames:
                        raise ValueError("Invalid clip usable range")
                    if type(index) is not int or not usable_start <= index < usable_end:
                        raise ValueError("Source frame outside clip")
                    box = coordinates[index]
                    if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
                        raise ValueError("Invalid confirmed face box")
                    x1, y1, x2, y2 = box
                    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                        raise ValueError("Face outside native frame")
                    if camera["mode"] == "twoShot" and not (body[0] <= x1 < x2 <= body[2] and body[1] <= y1 < y2 <= body[3]):
                        raise ValueError("Body mask must contain entire selected face")
                    sequence.append((clip_id, index, segment["state"] != "listening"))
                cursor = end
            if cursor != count or len(boxes) != 1:
                raise ValueError("Incomplete timeline or inconsistent role masks")
            sequences[role], role_boxes[role] = sequence, next(iter(boxes))
        if len(dimensions) != 1:
            raise ValueError("Camera clips must share native width, height and fps")
        if camera["mode"] == "twoShot" and not disjoint(role_boxes["A"], role_boxes["B"]):
            raise ValueError("Two-shot body masks overlap; explicit safe composition required")
        expanded[camera_id] = sequences
        geometry[camera_id] = {"native": next(iter(dimensions)), "bodyBoxes": role_boxes, "mode": camera["mode"]}
    edits, cursor = request["cameraEditPlan"], 0
    if not isinstance(edits, list) or not 1 <= len(edits) <= count:
        raise ValueError("A bounded camera edit plan is required")
    for edit in edits:
        start, end = _int(edit["startFrame"]), _int(edit["endFrame"], 1)
        if edit["cameraId"] not in expanded or start != cursor or not start < end <= count:
            raise ValueError("Camera edit plan has gap, overlap or unavailable camera")
        cursor = end
    if cursor != count:
        raise ValueError("Camera edit plan must cover entire timeline")
    return expanded, geometry


def compose_frame(role_frames, body_boxes):
    """Keep A's whole scene and replace only B's explicitly independent body."""
    if set(role_frames) != {"A", "B"} or not disjoint(body_boxes["A"], body_boxes["B"]):
        raise ValueError("Unsafe two-shot composition")
    frame = role_frames["A"].copy()
    x1, y1, x2, y2 = body_boxes["B"]
    frame[y1:y2, x1:x2] = role_frames["B"][y1:y2, x1:x2]
    return frame


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_latent(tensor, torch):
    # MuseTalk 1.5 concatenates masked and reference SD-VAE latents.
    return (isinstance(tensor, torch.Tensor) and tuple(tensor.shape) == (1, 8, 32, 32)
            and tensor.dtype in (torch.float16, torch.float32) and bool(torch.isfinite(tensor).all()))


class ClipReader:
    """Small decoded-frame LRU; long clips never expand fully into RAM."""
    def __init__(self, clip, cv2, cache):
        self.clip, self.cv2, self.cache, self.next = clip, cv2, cache, 0
        self.cap = cv2.VideoCapture(clip["path"])
        if not self.cap.isOpened():
            raise ValueError("Motion video cannot be opened")
        actual = (round(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)), round(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                  round(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)), self.cap.get(cv2.CAP_PROP_FPS))
        if actual[:3] != (clip["width"], clip["height"], clip["frames"]) or abs(actual[3]-clip["fps"]) > .001:
            self.cap.release()
            raise ValueError("Decoded native specifications changed")

    def get(self, index):
        frames, key = self.cache["frames"], (self.clip["fileHash"], index)
        if key in frames:
            frames.move_to_end(key)
            return frames[key].copy()
        if self.next != index:
            if not self.cap.set(self.cv2.CAP_PROP_POS_FRAMES, index):
                raise ValueError("Cannot seek to scheduled motion frame")
        ok, frame = self.cap.read()
        if not ok or frame.shape[:2] != (self.clip["height"], self.clip["width"]):
            raise ValueError("Scheduled source frame is missing")
        self.next = index+1
        frames[key] = frame
        self.cache["bytes"] += frame.nbytes
        while self.cache["bytes"] > 64 * 1024 * 1024:
            _, discarded = frames.popitem(last=False)
            self.cache["bytes"] -= discarded.nbytes
        return frame.copy()

    def close(self):
        self.cap.release()


def _encoder(path, width, height, crf):
    return subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", "25", "-i", "pipe:0", "-an", "-c:v", "libx264",
        "-preset", "fast", "-crf", str(crf), "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)


def run_motion(request, runtime):
    """Use worker-initialized real MuseTalk models; no fallback/static replacement."""
    expanded, geometry = validate_request(request)
    rt = runtime if isinstance(runtime, dict) else vars(runtime)
    torch, cv2 = rt["torch"], rt["cv2"]
    with torch.inference_mode():
        return _run(request, expanded, geometry, rt, torch, cv2)


def _run(request, expanded, geometry, rt, torch, cv2):
    started = time.monotonic()
    root, out = Path(rt["ROOT"]), Path(request["outputDir"])
    out.mkdir(parents=True, exist_ok=True)
    clips, count = request["motionAssets"]["clips"], request["motionAssets"]["plan"]["totalFrames"]
    quality = rt["quality"]
    readers, features, latent_banks = {}, {}, {}
    used = {item[0] for roles in expanded.values() for sequence in roles.values() for item in sequence}
    for clip_id in used:
        if file_hash(clips[clip_id]["path"]) != clips[clip_id]["fileHash"]:
            raise ValueError("Motion artifact content changed before worker decode")
    try:
        frame_cache = {"frames": OrderedDict(), "bytes": 0}
        for clip_id in used:
            readers[clip_id] = ClipReader(clips[clip_id], cv2, frame_cache)
        # One authoritative audio feature sequence per role, reused for every camera.
        for role in {r for roles in expanded.values() for r in roles}:
            values, samples = rt["processor"].get_audio_feature(request["tracks"][role])
            chunks = rt["processor"].get_whisper_chunk(values, "cuda", torch.float16, rt["whisper"], samples, fps=25)
            if len(chunks) < count-1 or not len(chunks):
                raise ValueError("Authoritative role audio is too short")
            if len(chunks) < count:
                chunks = torch.cat([chunks, chunks[-1:]])
            features[role] = chunks[:count].cpu()
        cache_root = root / "cache/motion_latents_v1"
        cache_root.mkdir(parents=True, exist_ok=True)
        vae_identity = {"config": file_hash(root / "models/sd-vae/config.json"),
                        "weights": file_hash(root / "models/sd-vae/diffusion_pytorch_model.bin")}
        required = {clip_id: set() for clip_id in used}
        for roles in expanded.values():
            for sequence in roles.values():
                for clip_id, source, active in sequence:
                    if active:
                        required[clip_id].add(source)
        for clip_id in used:
            clip = clips[clip_id]
            key = hashlib.sha256(json.dumps({"video": clip["fileHash"], "coordinates": clip["coordinates"],
                "vae": vae_identity, "recipe": "native-face-lanczos256-v1"}, sort_keys=True).encode()).hexdigest()
            path = cache_root / (key + ".pt")
            cached = torch.load(path, map_location="cpu", weights_only=True) if path.exists() else {}
            bank = cached.get("latents", {}) if cached.get("cacheKey") == key else {}
            if not isinstance(bank, dict) or any(type(i) is not int or not 0 <= i < clip["frames"]
                                                or not _valid_latent(value, torch) for i, value in bank.items()):
                raise ValueError("Cached motion latents are invalid; remove corrupt cache before retry")
            changed = False
            for index in sorted(required[clip_id] - set(bank)):
                frame = readers[clip_id].get(index)
                x1, y1, x2, y2 = clip["coordinates"][index]
                crop = cv2.resize(frame[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4)
                bank[index] = rt["vae"].get_latents_for_unet(crop).cpu()
                if not _valid_latent(bank[index], torch):
                    raise ValueError("VAE returned invalid motion latents")
                changed = True
            if changed:
                temporary = path.with_name(key + "." + uuid.uuid4().hex + ".partial")
                torch.save({"cacheKey": key, "latents": bank}, temporary)
                temporary.replace(path)
            latent_banks[clip_id] = bank
        stats, paths = {}, {}
        for camera_id, roles in expanded.items():
            width, height, _ = geometry[camera_id]["native"]
            target = out / ("camera_" + camera_id + ".mp4")
            partial = target.with_suffix(".partial.mp4")
            process = _encoder(partial, width, height, quality["roleCrf"])
            mouth_frames = {role: 0 for role in roles}
            try:
                for first in range(0, count, 8):
                    stop = min(count, first+8)
                    rendered = {role: {} for role in roles}
                    for role, sequence in roles.items():
                        batch = [i for i in range(first, stop) if sequence[i][2]]
                        if not batch:
                            continue
                        latents = torch.cat([latent_banks[sequence[i][0]][sequence[i][1]] for i in batch]).cuda().half()
                        cond = rt["pe"](features[role][batch].cuda().half())
                        predicted = rt["unet"].model(latents, torch.tensor([0], device="cuda"), encoder_hidden_states=cond).sample
                        if len(predicted) != len(batch) or not bool(torch.isfinite(predicted).all()):
                            raise ValueError("Mouth model produced incomplete or nonfinite predictions")
                        faces = rt["vae"].decode_latents(predicted)
                        if len(faces) != len(batch):
                            raise ValueError("VAE decoded fewer mouth frames than requested")
                        for index, face in zip(batch, faces):
                            if len(face.shape) != 3 or face.shape[2] != 3 or not rt["np"].isfinite(face).all():
                                raise ValueError("VAE decoded invalid face pixels")
                            clip_id, source, _ = sequence[index]
                            canvas = readers[clip_id].get(source)
                            box = clips[clip_id]["coordinates"][source]
                            x1, y1, x2, y2 = box
                            face = cv2.resize(face, (x2-x1, y2-y1), interpolation=cv2.INTER_LANCZOS4)
                            rendered[role][index] = rt["get_image"](canvas, face, box, fp=rt["parser"], mode="jaw")
                            mouth_frames[role] += 1
                    for index in range(first, stop):
                        role_frames = {}
                        for role, sequence in roles.items():
                            clip_id, source, _ = sequence[index]
                            role_frames[role] = rendered[role][index] if index in rendered[role] else readers[clip_id].get(source)
                        frame = compose_frame(role_frames, geometry[camera_id]["bodyBoxes"]) if len(roles) == 2 else next(iter(role_frames.values()))
                        process.stdin.write(frame.tobytes())
                    if first % 200 == 0:
                        print(f"motion {camera_id} {first}/{count}", flush=True)
            finally:
                try:
                    process.stdin.close()
                finally:
                    code = process.wait()
            if code:
                raise RuntimeError("Motion camera encoding failed")
            partial.replace(target)
            paths[camera_id] = target
            stats[camera_id] = {"nativeSize": [width, height], "mouthFrames": mouth_frames,
                                "visibleRoles": sorted(roles), "compositing": "explicit-disjoint-body-regions" if len(roles) == 2 else "full-frame-original-foreground"}
        _edit(request, paths, out, quality)
        for clip_id in used:
            if file_hash(clips[clip_id]["path"]) != clips[clip_id]["fileHash"]:
                raise ValueError("Motion artifact changed during rendering")
        metrics = {"wallSeconds": time.monotonic()-started, "durationSec": request["sampleCount"]/request["sampleRate"],
                   "frames": count, "motionSource": "pinned-reviewed-motion-pack", "packId": request["motionAssets"]["packId"],
                   "packVersion": request["motionAssets"]["packVersion"], "engine": "MuseTalk 1.5",
                   "audioFeaturePasses": len(features), "cameras": stats, "seamsRequireVisualReview": True,
                   "peakCudaGB": torch.cuda.max_memory_allocated()/1e9}
        (out / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        print(json.dumps(metrics), flush=True)
        return metrics
    finally:
        for reader in readers.values():
            reader.close()


def _edit(request, paths, out, quality):
    width, height = map(int, request.get("resolution", "1920x1080").split("x"))
    fps = request.get("fps", 25)
    if (width, height) not in {(854, 480), (1280, 720), (1920, 1080)} or fps not in (25, 30):
        raise ValueError("Unsupported export specification")
    camera_ids = sorted(paths)
    command = ["ffmpeg", "-v", "error", "-y"]
    for camera_id in camera_ids:
        command += ["-i", str(paths[camera_id])]
    command += ["-i", request["master"]]
    filters, uses = [], {id: [] for id in camera_ids}
    edits = request["cameraEditPlan"]
    for index, edit in enumerate(edits):
        uses[edit["cameraId"]].append(index)
    for camera_id, indices in uses.items():
        if indices:
            source = camera_ids.index(camera_id)
            filters.append(f"[{source}:v]split={len(indices)}" + "".join(f"[in{i}]" for i in indices))
    for i, edit in enumerate(edits):
        filters.append(f"[in{i}]trim=start_frame={edit['startFrame']}:end_frame={edit['endFrame']},setpts=PTS-STARTPTS,"
                       f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},"
                       + (quality["filter"]+"," if quality.get("filter") else "") + f"setsar=1[shot{i}]")
    filters.append("".join(f"[shot{i}]" for i in range(len(edits))) + f"concat=n={len(edits)}:v=1:a=0,fps={fps}[v]")
    subprocess.run(command + ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", f"{len(camera_ids)}:a:0",
        "-t", str(request["sampleCount"]/request["sampleRate"]), "-c:v", "libx264", "-preset", "fast", "-crf", str(quality["crf"]),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out/"final.mp4")], check=True)
