"""Deterministic sample-clock planning; no decoding, GPU, or inferred seam approval."""
from __future__ import annotations

import hashlib
import math
from typing import Any

from ..domain.motion_pack import MotionPack, ShotStyle, clip_readiness, readiness


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _boundary(metadata, clip, frame):
    record = metadata.get(clip.id, {})
    if not isinstance(record, dict) or record.get("fileHash") != clip.media.file_hash:
        return None
    frames = record.get("frames", {})
    entry = frames.get(str(frame)) if isinstance(frames, dict) else None
    if not isinstance(entry, dict):
        return None
    vectors = [entry.get(key) for key in ("pose", "velocity")]
    if any(not isinstance(v, list) or not v or len(v) > 256 or
           any(type(n) not in (int, float) or not math.isfinite(n) for n in v) for v in vectors):
        return None
    return vectors


def _seam(metadata, previous, current):
    if previous is None:
        return {"status": "initial"}
    old_clip, old_frame = previous
    new_clip, new_frame = current
    before, after = _boundary(metadata, old_clip, old_frame), _boundary(metadata, new_clip, new_frame)
    if before is None or after is None or any(len(a) != len(b) for a, b in zip(before, after)):
        return {"status": "unverified", "reason": "pose_velocity_evidence_missing_or_incompatible"}
    return {"status": "measured", "evidenceSource": "supplied_hash_bound_metadata",
            "poseDistance": math.dist(before[0], after[0]),
            "velocityDistance": math.dist(before[1], after[1]), "naturalnessVerified": False}


def build_motion_schedule(pack: MotionPack, style: ShotStyle, turns: list[dict], *,
                          sample_rate: int, total_samples: int, output_fps: int = 25,
                          seed: str = "0", seam_metadata: dict | None = None) -> dict[str, Any]:
    """Plan adopted clips against integer audio samples.

    Caller must verify pinned artifact integrity with MotionPackStore first.
    Half-open output/source ranges use exact sourceFrames for rate conversion.
    Turns accept startSample/endSample, optional sampleCount, and optional state
    speaking/responding. Overlap is rejected; silence maps to listening. Only
    visible subject roles are rendered in an over-shoulder view.
    """
    pack = MotionPack.model_validate(pack.model_dump(mode="json", by_alias=True))
    _integer(sample_rate, "sample_rate", 1)
    _integer(total_samples, "total_samples", 1)
    _integer(output_fps, "output_fps", 1)
    if output_fps > 120 or sample_rate > 384000:
        raise ValueError("Clock exceeds supported bounds")
    count = (total_samples * output_fps + sample_rate - 1) // sample_rate
    if count > 90000 or not isinstance(turns, list) or len(turns) > 10000:
        raise ValueError("Schedule exceeds resource bounds")
    if not isinstance(seed, str) or len(seed) > 256:
        raise ValueError("Seed must be a short string")
    metadata = seam_metadata or {}
    if not isinstance(metadata, dict):
        raise ValueError("Seam metadata must be an object")
    checked = []
    for turn in turns:
        if not isinstance(turn, dict) or turn.get("speaker") not in ("A", "B"):
            raise ValueError("Turn requires speaker A or B")
        start = _integer(turn.get("startSample"), "startSample")
        end = _integer(turn.get("endSample"), "endSample", 1)
        if not start < end <= total_samples:
            raise ValueError("Turn range lies outside audio")
        if "sampleCount" in turn and _integer(turn["sampleCount"], "sampleCount", 1) != end - start:
            raise ValueError("Turn sampleCount mismatch")
        state = turn.get("state", "speaking")
        if state not in ("speaking", "responding"):
            raise ValueError("Turn state must be speaking or responding")
        checked.append((start, end, turn["speaker"], state))
    checked.sort()
    if any(a[1] > b[0] for a, b in zip(checked, checked[1:])):
        raise ValueError("Overlapping turns are unsupported")
    ready = readiness(pack, style)
    if not ready["ready"]:
        raise ValueError("Motion pack is not ready: " + ",".join(ready["missing"]))
    result = {"formatVersion": 1, "packId": pack.id, "packVersion": pack.version,
              "sampleRate": sample_rate, "totalSamples": total_samples, "fps": output_fps,
              "totalFrames": count, "seed": seed, "cameras": [], "issues": [],
              "method": "integer_sample_clock_forward_clips_v1"}
    # Frame timestamps are evaluated as rational numbers; no float accumulation.
    states = {role: ["listening"] * count for role in "AB"}
    for start, end, speaker, state in checked:
        first = (start * output_fps + sample_rate - 1) // sample_rate
        last = (end * output_fps + sample_rate - 1) // sample_rate
        states[speaker][first:last] = [state] * (last - first)
    for camera_id in ready["cameraIds"]:
        camera = next(c for c in pack.cameras if c.id == camera_id)
        roles = "AB" if camera.mode == "twoShot" else (camera.subject_speaker,)
        planned = {"cameraId": camera_id, "roles": {}}
        for role in roles:
            segments, cursor, previous = [], 0, None
            while cursor < count:
                state = states[role][cursor]
                boundary = cursor + 1
                while boundary < count and states[role][boundary] == state:
                    boundary += 1
                bank = [c for c in pack.clips if c.camera_id == camera_id and c.role == role
                        and c.state == state and not clip_readiness(pack, c)]
                if not bank:
                    raise ValueError(f"No adopted {state} clip for {camera_id}/{role}")
                while cursor < boundary:
                    candidates = [c for c in bank if previous is None or c.id != previous[0].id] or bank
                    def key(clip):
                        seam = _seam(metadata, previous, (clip, clip.usable_start_frame))
                        distance = seam.get("poseDistance", float("inf")) + seam.get("velocityDistance", 0)
                        tie = hashlib.sha256(f"{seed}:{camera_id}:{role}:{cursor}:{clip.id}".encode()).hexdigest()
                        return distance, tie
                    clip = min(candidates, key=key)
                    length = ((clip.usable_end_frame - clip.usable_start_frame) * output_fps + clip.fps - 1) // clip.fps
                    length = min(length, boundary - cursor)
                    source = [clip.usable_start_frame + i * clip.fps // output_fps for i in range(length)]
                    seam = _seam(metadata, previous, (clip, source[0]))
                    segment = {"outputStartFrame": cursor, "outputEndFrame": cursor + length,
                               "clipId": clip.id, "artifactId": clip.media.artifact_id,
                               "fileHash": clip.media.file_hash, "state": state, "direction": 1,
                               "sourceStartFrame": source[0], "sourceEndFrame": source[-1] + 1,
                               "sourceFrames": source, "seam": seam}
                    segments.append(segment)
                    if seam["status"] == "unverified":
                        result["issues"].append({"code": "SEAM_UNVERIFIED", "cameraId": camera_id,
                                                 "role": role, "outputFrame": cursor})
                    if previous and previous[0].id == clip.id:
                        result["issues"].append({"code": "SINGLE_CLIP_REPEAT", "cameraId": camera_id,
                                                 "role": role, "outputFrame": cursor})
                    previous = clip, source[-1]
                    cursor += length
            planned["roles"][role] = segments
        result["cameras"].append(planned)
    return result
