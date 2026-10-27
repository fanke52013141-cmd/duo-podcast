"""Frozen short-window expert proposals, not evidence of model motion retention."""
from __future__ import annotations

import hashlib
import json
import re


VERSION = "expert-motion-short-reference-v1"


def _int(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"Invalid {label}")
    return value


def _identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError(f"Invalid {label}")
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Expected source SHA256")
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _box(region, width, height):
    import math
    if not isinstance(region, dict) or set(region) != {"x", "y", "width", "height"}:
        raise ValueError("Explicit body region required")
    x, y, w, h = (region[k] for k in ("x", "y", "width", "height"))
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (x, y, w, h)) or not (
            0 <= x < x + w <= 1 and 0 <= y < y + h <= 1):
        raise ValueError("Body region outside frame")
    return (round(x * width), round(y * height), round((x + w) * width), round((y + h) * height))


def _expand_assets(assets, count):
    """Recheck bindings at the expert boundary; never discover faces by position."""
    cameras, clips, expanded, geometry = assets["cameras"], assets["clips"], {}, {}
    if not isinstance(clips, dict) or len(clips) > 128 or not 1 <= len(assets["plan"]["cameras"]) <= 8:
        raise ValueError("Expert source limits exceeded")
    for planned in assets["plan"]["cameras"]:
        camera_id = _identifier(planned["cameraId"], "camera ID")
        if camera_id in expanded:
            raise ValueError("Duplicate camera plan")
        camera = cameras[camera_id]
        if camera["mode"] == "twoShot":
            roles = {"A", "B"}
        elif camera["mode"] == "overShoulder" and camera.get("subjectSpeaker") in {"A", "B"}:
            roles = {camera["subjectSpeaker"]}
            if camera.get("foregroundSpeaker") not in {"A", "B"} - roles:
                raise ValueError("Over-shoulder subject/foreground must differ")
        else:
            raise ValueError("Unsupported expert camera")
        if set(planned["roles"]) != roles:
            raise ValueError("Only visible camera subjects may receive audio")
        dimensions, masks, sequences = set(), {}, {}
        for role, segments in planned["roles"].items():
            sequence, cursor, bodies = [], 0, set()
            if not segments or len(segments) > count:
                raise ValueError("Missing/excessive source segments")
            for segment in segments:
                start, end = (_int(segment[k], k) for k in ("outputStartFrame", "outputEndFrame"))
                if start != cursor or not start < end <= count or segment.get("direction") != 1:
                    raise ValueError("Noncontiguous or reverse source plan")
                clip_id = _identifier(segment["clipId"], "clip ID")
                clip = clips[clip_id]
                if clip.get("role") != role or clip.get("cameraId") != camera_id or clip.get("state") != segment["state"]:
                    raise ValueError("Expert source role/camera/state mismatch")
                if segment["state"] not in {"speaking", "listening", "responding"}:
                    raise ValueError("Unknown source state")
                digest = _digest(clip["fileHash"])
                if digest != segment["fileHash"]:
                    raise ValueError("Expert source/plan hash differs")
                width, height, frames, fps = (_int(clip[k], k, 1) for k in ("width", "height", "frames", "fps"))
                if width * height > 4096 * 2160 or width % 2 or height % 2 or frames > 36000 or fps > 120:
                    raise ValueError("Expert native source limits exceeded")
                dimensions.add((width, height, fps))
                body = _box(clip["bodyRegion"], width, height)
                bodies.add(body)
                first, last = (_int(clip[k], k) for k in ("usableStartFrame", "usableEndFrame"))
                indices, boxes = segment["sourceFrames"], clip["coordinates"]
                if not 0 <= first < last <= frames or not isinstance(boxes, list) or len(boxes) != frames:
                    raise ValueError("Missing confirmed source coordinates/range")
                if not isinstance(indices, list) or len(indices) != end - start or not indices:
                    raise ValueError("Incomplete source mapping")
                if any(type(i) is not int or not first <= i < last for i in indices):
                    raise ValueError("Source frame outside adopted interval")
                if indices != [indices[0] + i * fps // 25 for i in range(len(indices))]:
                    raise ValueError("Source mapping changes native playback rate")
                if segment.get("sourceStartFrame") != indices[0] or segment.get("sourceEndFrame") != indices[-1] + 1:
                    raise ValueError("Source range differs from exact mapping")
                for index in indices:
                    box = boxes[index]
                    if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
                        raise ValueError("Missing/invalid selected face box")
                    x1, y1, x2, y2 = box
                    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                        raise ValueError("Selected face outside native frame")
                    if not (body[0] <= x1 < x2 <= body[2] and body[1] <= y1 < y2 <= body[3]):
                        raise ValueError("Expert audio mask region must contain its selected face")
                    sequence.append({"clipId": clip_id, "sourceFrame": index, "videoFileHash": digest,
                                     "faceBox": list(box), "state": segment["state"]})
                cursor = end
            if cursor != count or len(bodies) != 1:
                raise ValueError("Incomplete source clock or changing body masks")
            masks[role], sequences[role] = next(iter(bodies)), sequence
        if len(dimensions) != 1:
            raise ValueError("A camera must have one native geometry/rate")
        if len(roles) == 2:
            a, b = masks["A"], masks["B"]
            if not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]):
                raise ValueError("Two-shot body regions overlap")
        expanded[camera_id] = sequences
        geometry[camera_id] = {"native": list(next(iter(dimensions))), "bodyBoxes": masks,
                               "mode": camera["mode"], "visibleRoles": sorted(roles)}
    return expanded, geometry


def build_expert_motion_plan(motion_assets, camera_edit_plan, *, sample_rate, total_samples,
                             seed=1, core_frames=64, context_frames=8):
    """Propose <=81-frame native model calls with trims on the 25fps clock.

    Boundary context repeats reference edge frames only outside visible cores.
    These are source instructions, not proof that a model retained their motion.
    Caller must first verify pack adoption, registered paths and physical hashes.
    """
    _int(sample_rate, "sample rate", 1)
    _int(total_samples, "sample count", 1)
    _int(core_frames, "core frames", 1)
    _int(context_frames, "context frames")
    _int(seed, "seed")
    if sample_rate > 384000 or total_samples > sample_rate * 300 or seed > 2**63 - 1:
        raise ValueError("Expert request limits exceeded")
    if core_frames > 64 or context_frames > 8:
        raise ValueError("Expert model window exceeds validated planning bound")
    pack_id = _identifier(motion_assets["packId"], "pack ID")
    version = _identifier(motion_assets["packVersion"], "pack version")
    count = (total_samples * 25 + sample_rate - 1) // sample_rate
    plan = motion_assets["plan"]
    if motion_assets.get("formatVersion") != 1 or any(
            plan.get(key, expected) != expected for key, expected in (("packId", pack_id), ("packVersion", version))):
        raise ValueError("Expert source format or pinned plan identity differs")
    if plan.get("fps") != 25 or plan.get("totalFrames") != count or plan.get("sampleRate") != sample_rate or plan.get("totalSamples") != total_samples:
        raise ValueError("Expert authoritative sample clock differs")
    expanded, geometry = _expand_assets(motion_assets, count)
    if not isinstance(camera_edit_plan, list) or not 1 <= len(camera_edit_plan) <= count:
        raise ValueError("Missing/bounded real-camera edit plan required")
    segments, cursor = [], 0
    for edit in camera_edit_plan:
        camera_id = edit["cameraId"]
        start, end = (_int(edit[k], k) for k in ("startFrame", "endFrame"))
        if camera_id not in expanded or start != cursor or not start < end <= count:
            raise ValueError("Expert camera plan has gap/overlap/unavailable camera")
        for left in range(start, end, core_frames):
            right = min(end, left + core_frames)
            desired = context_frames + right - left + context_frames
            model_frames = 4 * ((desired - 1 + 3) // 4) + 1
            if model_frames > 81:
                raise ValueError("Expert model call exceeds 81 frames")
            raw_frames = list(range(left - context_frames, right + context_frames))
            raw_frames += [raw_frames[-1]] * (model_frames - len(raw_frames))
            source_frames = [min(count - 1, max(0, frame)) for frame in raw_frames]
            source_by_role = {role: [sequence[frame] for frame in source_frames]
                              for role, sequence in expanded[camera_id].items()}
            context_start, context_end = max(0, left - context_frames), min(count, right + context_frames)
            audio_start = min(total_samples, context_start * sample_rate // 25)
            audio_end = min(total_samples, context_end * sample_rate // 25)
            prefix = max(0, context_frames - left) * sample_rate // 25
            model_samples = (model_frames * sample_rate + 24) // 25
            item = {"packId": pack_id, "packVersion": version, "cameraId": camera_id,
                "outputStartFrame": left, "outputEndFrame": right,
                "coreStartSample": min(total_samples, left * sample_rate // 25),
                "coreEndSample": min(total_samples, right * sample_rate // 25),
                "modelFrames": model_frames, "modelFps": 25,
                "trimStartFrame": context_frames, "trimEndFrame": context_frames + right - left,
                "contextStartFrame": context_start, "contextEndFrame": context_end,
                "referenceOutputFrames": source_frames,
                "sourceByRole": source_by_role, "geometry": geometry[camera_id],
                "audioSlice": {"startSample": audio_start, "endSample": audio_end,
                    "prefixSilentSamples": prefix,
                    "suffixSilentSamples": model_samples - prefix - (audio_end - audio_start),
                    "modelSampleCount": model_samples,
                    "rounding": "source_frame_boundaries_floor_model_duration_ceil"},
                "padding": {"prefixFrames": max(0, context_frames - left),
                    "suffixFrames": max(0, right + context_frames - count),
                    "quantizationFrames": model_frames - desired,
                    "policy": "edge_reference_only_outside_visible_core"},
                "controlTrace": {"basis": "source_plan_only", "runtimeCompatibilityVerified": False,
                    "referenceMotionRetentionVerified": False, "audioLipSyncVerified": False}}
            signature = _hash({"version": VERSION, "requestSeed": seed, "segment": item})
            item["seed"] = int(signature[:16], 16) % (2**63)
            item["dependencyHash"] = signature
            segments.append(item)
        cursor = end
    if cursor != count:
        raise ValueError("Expert camera plan does not cover complete timeline")
    return {"formatVersion": 1, "implementation": VERSION, "packId": pack_id, "packVersion": version,
            "sampleRate": sample_rate, "totalSamples": total_samples, "totalFrames": count,
            "nativeFps": 25, "segments": segments, "finalAudio": "unchanged_authoritative_master",
            "finalTrimSamples": total_samples, "qualityStatus": "unverified",
            "limitations": ["Plan does not execute a model", "Reference latent conditioning does not prove motion retention",
                            "All generated segments require actual media and visual acceptance"]}
