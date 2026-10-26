"""Hash-bound incremental work proposals. A key hit is never a finished GPU job."""
from __future__ import annotations

import hashlib
import json
import re

VERSION = "motion-segments-v2-whisper30"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _int(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"Invalid {name}")
    return value


def _digest(value):
    if not isinstance(value, str) or re.fullmatch("[a-f0-9]{64}", value) is None:
        raise ValueError("Expected SHA256 digest")
    return value


def build_motion_segment_cache_plan(motion_assets: dict, utterances: list[dict], *,
                                    sample_rate: int, total_samples: int, role_audio: dict,
                                    render_spec: dict, runtime_fingerprint: str,
                                    previous: dict | None = None, context_frames: int = 5) -> dict:
    """Hash exact PCM16 mono context and source frames for every camera/role.

    role_audio maps A/B to {pcm: bytes, fileHash?: sha256}; the optional hash is
    checked against actual bytes. Render context must be rendered then trimmed
    to outputStartFrame/outputEndFrame. Downstream cache storage must separately
    verify output existence/hash/native specs before consuming a reuse candidate.
    """
    _int(sample_rate, "sample rate", 1)
    _int(total_samples, "sample count", 1)
    _int(context_frames, "context frames")
    if sample_rate > 384000 or total_samples > sample_rate * 300 or context_frames > 50:
        raise ValueError("Segment plan resource bound exceeded")
    if not isinstance(render_spec, dict) or set(render_spec) - {"fps", "resolution", "fastQuality"}:
        raise ValueError("Unsupported render specification")
    if render_spec.get("fps", 25) not in (25, 30) or render_spec.get("resolution", "1920x1080") not in ("854x480", "1280x720", "1920x1080"):
        raise ValueError("Unsupported output geometry")
    _digest(runtime_fingerprint)
    plan, clips = motion_assets["plan"], motion_assets["clips"]
    count = (total_samples * 25 + sample_rate - 1) // sample_rate
    if (plan["fps"] != 25 or plan["totalFrames"] != count or plan["totalSamples"] != total_samples
            or plan["sampleRate"] != sample_rate or count > 7500):
        raise ValueError("Motion/audio sample clock mismatch")
    if not isinstance(utterances, list) or len(utterances) > 10000:
        raise ValueError("Utterance bounds exceeded")
    turns, ids, boundaries = [], set(), {0, count}
    for utterance in utterances:
        id, role = utterance.get("id"), utterance.get("speaker")
        start, end = utterance.get("startSample"), utterance.get("endSample")
        _int(start, "utterance start")
        _int(end, "utterance end", 1)
        if not isinstance(id, str) or not id or id in ids or role not in ("A", "B") or not start < end <= total_samples:
            raise ValueError("Invalid utterance identity or range")
        ids.add(id)
        turns.append((start, end, id, role))
        boundaries.update(((start*25 + sample_rate-1)//sample_rate, (end*25 + sample_rate-1)//sample_rate))
    turns.sort()
    if any(a[1] > b[0] for a, b in zip(turns, turns[1:])):
        raise ValueError("Overlapping utterances unsupported")
    pcm, track_hashes = {}, {}
    for role in "AB":
        value = role_audio.get(role, {})
        data = value.get("pcm")
        if not isinstance(data, bytes) or len(data) != total_samples * 2:
            raise ValueError("Authoritative PCM16 mono track length mismatch")
        digest = hashlib.sha256(data).hexdigest()
        if value.get("fileHash") is not None and value["fileHash"] != digest:
            raise ValueError("Authoritative PCM hash mismatch")
        pcm[role], track_hashes[role] = data, digest
    old_keys = set()
    if previous is not None:
        if previous.get("checksVersion") != VERSION:
            raise ValueError("Unsupported previous segment plan")
        old_keys = {s["cacheKey"] for s in previous.get("segments", [])}
    result = {"formatVersion": 1, "checksVersion": VERSION, "sampleRate": sample_rate,
              "totalSamples": total_samples, "totalFrames": count, "trackHashes": track_hashes,
              "segments": [], "invalidations": [], "cacheOutputsVerified": False,
              "contextPolicy": {"framesBefore": context_frames, "framesAfter": context_frames,
                                "units": "25fps_frames", "trimContextBeforeAssembly": True,
                                "audioEncoderWindowSeconds": 30, "audioFeatureRate": 50,
                                "chunkFeatureOffsets": [-4, 5],
                                "nonNativeResamplePolicy": "whole_role_track_dependency",
                                "modelReceptiveFieldVerified": True}}
    points = sorted(boundaries)
    for camera in plan["cameras"]:
        camera_id = camera["cameraId"]
        for role, segments in camera["roles"].items():
            expanded, cursor = [], 0
            for segment in segments:
                if segment["outputStartFrame"] != cursor or segment["outputEndFrame"] <= cursor:
                    raise ValueError("Invalid motion segment continuity")
                indices = segment["sourceFrames"]
                if len(indices) != segment["outputEndFrame"] - cursor:
                    raise ValueError("Incomplete motion source mapping")
                clip = clips[segment["clipId"]]
                digest = _digest(clip["fileHash"])
                for frame in indices:
                    if type(frame) is not int or not 0 <= frame < clip["frames"]:
                        raise ValueError("Invalid source frame")
                    expanded.append((segment["clipId"], digest, frame, segment["state"],
                                     clip["coordinates"][frame]))
                cursor = segment["outputEndFrame"]
            if cursor != count:
                raise ValueError("Motion plan does not cover audio")
            for start, end in zip(points, points[1:]):
                context_start, context_end = max(0, start-context_frames), min(count, end+context_frames)
                sample_start = context_start * sample_rate // 25
                sample_end = min(total_samples, (context_end*sample_rate+24)//25)
                audio_hash = hashlib.sha256(pcm[role][sample_start*2:sample_end*2]).hexdigest()
                # Vendor AudioProcessor encodes each complete 30s mel window with
                # self attention. A 25fps output chunk reads 50Hz indices
                # [2*i-4, 2*i+6), crossing adjacent 1500-feature windows.
                actual_features = total_samples * 50 // sample_rate
                feature_start = max(0, context_start*2-4)
                feature_end = min(actual_features, (context_end-1)*2+6)
                window_indices = range(feature_start//1500, (max(feature_start, feature_end-1))//1500+1)
                audio_windows = []
                for window in window_indices:
                    a, b = window*30*sample_rate, min(total_samples, (window+1)*30*sample_rate)
                    if a < b:
                        audio_windows.append({"windowIndex": window, "startSample": a, "endSample": b,
                                              "pcmHash": hashlib.sha256(pcm[role][a*2:b*2]).hexdigest()})
                # librosa's non-native resampling filter can depend on audio
                # outside a nominal window. Until its receptive field is pinned,
                # invalidate the whole role track rather than invent a short halo.
                resample_hash = track_hashes[role] if sample_rate != 16000 else None
                nearby = [id for a, b, id, _ in turns if a < sample_end and b > sample_start]
                dependencies_ranges = ([(0, total_samples)] if resample_hash else
                                       [(w["startSample"], w["endSample"]) for w in audio_windows])
                affected = [id for a, b, id, speaker in turns if speaker == role and
                            any(a < hi and b > lo for lo, hi in dependencies_ranges)]
                source = expanded[context_start:context_end]
                dependencies = {"version": VERSION, "camera": motion_assets["cameras"][camera_id],
                    "cameraId": camera_id, "role": role, "sources": source,
                    "clipGeometry": {id: {key: clips[id].get(key) for key in ("width", "height", "fps", "bodyRegion")}
                                     for id in sorted({item[0] for item in source})},
                    "audioPcmHash": audio_hash, "sampleRate": sample_rate,
                    "whisperWindows": audio_windows, "resampleTrackHash": resample_hash,
                    "actualWhisperFeatureLength": actual_features,
                    "sampleCount": sample_end-sample_start,
                    "runtimeFingerprint": runtime_fingerprint,
                    "trimStartFrames": start-context_start, "trimEndFrames": end-context_start,
                    "frameSamplePhase": context_start*sample_rate % 25}
                key = _hash(dependencies)
                reuse = key in old_keys
                record = {"cameraId": camera_id, "role": role, "outputStartFrame": start,
                    "outputEndFrame": end, "contextStartFrame": context_start, "contextEndFrame": context_end,
                    "contextStartSample": sample_start, "contextEndSample": sample_end,
                    "affectedUtteranceIds": affected, "renderContextUtteranceIds": nearby,
                    "cacheKey": key, "audioPcmHash": audio_hash,
                    "encodeKey": _hash({"motionKey": key, "renderSpec": render_spec}),
                    "audioEncoderWindows": audio_windows,
                    "resampleWholeTrackDependency": sample_rate != 16000,
                    "reuseCandidate": reuse, "outputVerified": False,
                    "reason": "matching_dependency_key_requires_output_verification" if reuse else "new_or_changed_context_dependencies"}
                result["segments"].append(record)
                if not reuse:
                    result["invalidations"].append({k: record[k] for k in (
                        "cameraId", "role", "outputStartFrame", "outputEndFrame", "affectedUtteranceIds", "reason")})
    result["retiredKeys"] = sorted(old_keys - {s["cacheKey"] for s in result["segments"]})
    return result
