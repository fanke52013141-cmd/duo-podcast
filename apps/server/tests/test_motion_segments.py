import copy
import hashlib

import pytest

from duocast.services.motion_segments import build_motion_segment_cache_plan


def inputs():
    clips = {"a": {"fileHash": "a"*64, "width": 100, "height": 80, "fps": 25, "frames": 150,
                    "coordinates": [[1, 2, 30, 40]] * 150, "bodyRegion": {"x": 0, "y": 0, "width": 1, "height": 1}}}
    assets = {"clips": clips, "cameras": {"dual": {"mode": "twoShot"}}, "plan": {
        "fps": 25, "totalFrames": 150, "sampleRate": 1000, "totalSamples": 6000,
        "cameras": [{"cameraId": "dual", "roles": {role: [{"outputStartFrame": 0, "outputEndFrame": 150,
          "clipId": "a", "state": "speaking", "sourceFrames": list(range(150))}] for role in "AB"}}]}}
    turns = [{"id": "u1", "speaker": "A", "startSample": 0, "endSample": 2000},
             {"id": "u2", "speaker": "B", "startSample": 2000, "endSample": 4000},
             {"id": "u3", "speaker": "A", "startSample": 4000, "endSample": 6000}]
    return assets, turns, {"sample_rate": 1000, "total_samples": 6000,
        "role_audio": {r: {"pcm": b"\x00\x00" * 6000} for r in "AB"},
        "render_spec": {"fps": 25, "resolution": "1280x720"}, "runtime_fingerprint": "c"*64}


def test_exact_context_and_previous_keys_are_only_candidates():
    assets, turns, kwargs = inputs()
    result = build_motion_segment_cache_plan(assets, turns, **kwargs)
    replay = build_motion_segment_cache_plan(assets, turns, **kwargs, previous=result)
    assert len(result["segments"]) == 6
    assert all(s["reuseCandidate"] and not s["outputVerified"] for s in replay["segments"])
    assert not replay["cacheOutputsVerified"] and replay["invalidations"] == []
    for s in replay["segments"]:
        assert s["contextStartSample"] == s["contextStartFrame"] * 40
        assert s["contextEndSample"] == s["contextEndFrame"] * 40


def test_changed_word_only_invalidates_actual_role_audio_context():
    assets, turns, kwargs = inputs()
    previous = build_motion_segment_cache_plan(assets, turns, **kwargs)
    data = bytearray(kwargs["role_audio"]["A"]["pcm"])
    data[200:202] = b"\x01\x00"
    kwargs["role_audio"]["A"]["pcm"] = bytes(data)
    changed = build_motion_segment_cache_plan(assets, turns, **kwargs, previous=previous)
    assert [(s["role"], s["outputStartFrame"]) for s in changed["segments"] if not s["reuseCandidate"]] == [("A", 0), ("A", 50), ("A", 100)]


def test_boundary_edit_invalidates_neighbor_context_honestly():
    assets, turns, kwargs = inputs()
    previous = build_motion_segment_cache_plan(assets, turns, **kwargs)
    data = bytearray(kwargs["role_audio"]["A"]["pcm"])
    data[3998:4000] = b"\x01\x00"
    kwargs["role_audio"]["A"]["pcm"] = bytes(data)
    changed = build_motion_segment_cache_plan(assets, turns, **kwargs, previous=previous)
    assert len(changed["invalidations"]) == 3
    assert all(s["role"] == "A" for s in changed["invalidations"])


def test_encoding_changes_reuse_motion_but_change_encode_key():
    assets, turns, kwargs = inputs()
    previous = build_motion_segment_cache_plan(assets, turns, **kwargs)
    kwargs["render_spec"] = {"fps": 30, "resolution": "1920x1080", "fastQuality": "crisp"}
    changed = build_motion_segment_cache_plan(assets, turns, **kwargs, previous=previous)
    assert all(s["reuseCandidate"] for s in changed["segments"])
    assert all(a["encodeKey"] != b["encodeKey"] for a, b in zip(previous["segments"], changed["segments"]))


@pytest.mark.parametrize("change", ["camera", "video", "coordinates", "runtime", "duration"])
def test_motion_dependency_matrix(change):
    assets, turns, kwargs = inputs()
    previous = build_motion_segment_cache_plan(assets, turns, **kwargs)
    if change == "camera":
        assets["cameras"]["dual"]["subjectRegion"] = {"x": .1}
    elif change == "video":
        assets["clips"]["a"]["fileHash"] = "d"*64
    elif change == "coordinates":
        assets["clips"]["a"]["coordinates"] = [[2, 2, 30, 40]] * 150
    elif change == "runtime":
        kwargs["runtime_fingerprint"] = "e"*64
    else:
        turns[0]["endSample"] = 1800
        turns[1]["startSample"] = 1800
    changed = build_motion_segment_cache_plan(assets, turns, **kwargs, previous=previous)
    assert changed["invalidations"]


def test_actual_audio_hash_and_clock_rejected_when_false():
    assets, turns, kwargs = inputs()
    kwargs["role_audio"]["A"]["fileHash"] = "f" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        build_motion_segment_cache_plan(assets, turns, **kwargs)
    kwargs["role_audio"]["A"].pop("fileHash")
    kwargs["sample_rate"] = 999
    with pytest.raises(ValueError, match="clock mismatch"):
        build_motion_segment_cache_plan(assets, turns, **kwargs)


def test_whisper_boundary_chunks_depend_on_both_thirty_second_windows():
    assets, _, kwargs = inputs()
    samples, frames = 16000*60, 1500
    kwargs.update(sample_rate=16000, total_samples=samples,
                  role_audio={r: {"pcm": b"\0\0"*samples} for r in "AB"}, context_frames=0)
    assets["plan"].update(totalSamples=samples, totalFrames=frames, sampleRate=16000)
    assets["clips"]["a"].update(frames=frames, coordinates=[[1, 2, 30, 40]]*frames)
    for segment in assets["plan"]["cameras"][0]["roles"].values():
        segment[0].update(outputEndFrame=frames, sourceFrames=list(range(frames)))
    turns = [{"id": "before", "speaker": "A", "startSample": 0, "endSample": 480000},
             {"id": "after", "speaker": "A", "startSample": 480000, "endSample": samples}]
    result = build_motion_segment_cache_plan(assets, turns, **kwargs)
    assert all([w["windowIndex"] for w in s["audioEncoderWindows"]] == [0, 1] for s in result["segments"])
    assert all(not s["resampleWholeTrackDependency"] for s in result["segments"])
