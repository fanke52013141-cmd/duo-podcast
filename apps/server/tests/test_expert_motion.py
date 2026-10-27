"""Static expert graph/source contracts; no simulated model quality claims."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from duocast.adapters.expert_motion_video import build_expert_motion_workflow
from duocast.services.expert_motion import build_expert_motion_plan


def assets_and_edits(frames=300, rate=48000):
    clips, cameras, plans = {}, {}, []
    for camera_id, roles in (("dual", "AB"), ("otsA", "A"), ("otsB", "B")):
        cameras[camera_id] = {"mode": "twoShot" if camera_id == "dual" else "overShoulder",
                              "subjectSpeaker": None if len(roles) == 2 else roles,
                              "foregroundSpeaker": None if len(roles) == 2 else "B" if roles == "A" else "A"}
        role_plans = {}
        for role in roles:
            segments = []
            for start in range(0, frames, 100):
                end = min(frames, start + 100)
                state = "speaking" if (start // 100) % 2 == (0 if role == "A" else 1) else "listening"
                id = camera_id + role + state
                x = 2 if role == "A" else 62
                digest = hashlib.sha256(id.encode()).hexdigest()
                clips[id] = {"path": "not-used-static-test.mp4", "fileHash": digest,
                    "width": 100, "height": 60, "fps": 25, "frames": 100,
                    "role": role, "cameraId": camera_id, "state": state,
                    "usableStartFrame": 0, "usableEndFrame": 100,
                    "coordinates": [[x, 2, x + 20, 24] for _ in range(100)],
                    "bodyRegion": {"x": 0 if role == "A" else .5, "y": 0, "width": .5, "height": 1}}
                segments.append({"outputStartFrame": start, "outputEndFrame": end, "clipId": id,
                    "state": state, "fileHash": digest, "direction": 1,
                    "sourceFrames": list(range(end - start)), "sourceStartFrame": 0, "sourceEndFrame": end - start})
            role_plans[role] = segments
        plans.append({"cameraId": camera_id, "roles": role_plans})
    samples = frames * rate // 25
    assets = {"formatVersion": 1, "packId": "pack", "packVersion": "v1", "clips": clips,
        "cameras": cameras, "plan": {"fps": 25, "totalFrames": frames, "sampleRate": rate,
                                     "totalSamples": samples, "cameras": plans}}
    edits = [{"cameraId": "dual", "startFrame": 0, "endFrame": frames}]
    return assets, edits, samples


def proposed(assets=None, edits=None, **extra):
    default_assets, default_edits, samples = assets_and_edits()
    return build_expert_motion_plan(assets or default_assets, edits or default_edits,
                                   sample_rate=48000, total_samples=samples, **extra)


def template():
    path = Path(__file__).parents[1] / "duocast/adapters/multitalk_workflow.json"
    return json.loads(path.read_text(encoding="utf-8"))


def names(roles="AB"):
    return {"anchorImage": "anchor.png", "referenceVideo": "reference.mp4", "masterAudio": "mix.wav",
            "roleAudio": {role: role + ".wav" for role in roles},
            "roleMasks": {role: "mask_" + role + ".png" for role in roles}}


def test_twelve_seconds_a_b_a_three_actual_cameras_complete_integer_clock():
    assets, _, samples = assets_and_edits()
    edits = [{"cameraId": "dual", "startFrame": 0, "endFrame": 100},
             {"cameraId": "otsB", "startFrame": 100, "endFrame": 200},
             {"cameraId": "otsA", "startFrame": 200, "endFrame": 300}]
    plan = proposed(assets, edits)
    assert plan["totalFrames"] == 300 and plan["totalSamples"] == samples
    assert plan["finalAudio"] == "unchanged_authoritative_master"
    assert plan["segments"][0]["outputStartFrame"] == 0
    assert plan["segments"][-1]["outputEndFrame"] == 300
    assert plan["segments"][-1]["coreEndSample"] == samples
    for left, right in zip(plan["segments"], plan["segments"][1:]):
        assert left["outputEndFrame"] == right["outputStartFrame"]
    for segment in plan["segments"]:
        assert segment["modelFrames"] <= 81 and (segment["modelFrames"] - 1) % 4 == 0
        assert segment["trimEndFrame"] - segment["trimStartFrame"] == segment["outputEndFrame"] - segment["outputStartFrame"]
        assert segment["audioSlice"]["prefixSilentSamples"] + segment["audioSlice"]["suffixSilentSamples"] + segment["audioSlice"]["endSample"] - segment["audioSlice"]["startSample"] == segment["audioSlice"]["modelSampleCount"]
        roles = set(segment["sourceByRole"])
        assert roles == ({"A", "B"} if segment["cameraId"] == "dual" else {segment["cameraId"][-1]})
        for source in segment["sourceByRole"].values():
            assert len(source) == segment["modelFrames"]
            assert all(0 <= item["sourceFrame"] < 100 for item in source)
        assert segment["controlTrace"]["referenceMotionRetentionVerified"] is False
    assert plan["segments"][0]["padding"]["prefixFrames"] == 8
    assert plan["segments"][-1]["padding"]["suffixFrames"] == 8


def test_plan_is_frozen_from_input_and_seed_and_version_change_dependencies():
    assets, edits, _ = assets_and_edits()
    first = proposed(assets, edits)
    assert first == proposed(assets, edits)
    assert first != proposed(assets, edits, seed=2)
    assets["packVersion"] = "v2"
    assert first["segments"][0]["dependencyHash"] != proposed(assets, edits)["segments"][0]["dependencyHash"]
    assets["clips"]["dualAspeaking"]["coordinates"][0][0] = 8
    assert first["segments"][0]["sourceByRole"]["A"][8]["faceBox"][0] == 2


@pytest.mark.parametrize("change", ["role", "camera", "range", "hash", "mask", "face", "gap", "reverse"])
def test_invalid_or_wrong_subject_sources_are_rejected(change):
    assets, edits, _ = assets_and_edits()
    clip = assets["clips"]["dualAspeaking"]
    segment = assets["plan"]["cameras"][0]["roles"]["A"][0]
    if change == "role": clip["role"] = "B"
    elif change == "camera": clip["cameraId"] = "otsA"
    elif change == "range": clip["usableStartFrame"] = 1
    elif change == "hash": clip["fileHash"] = "b" * 64
    elif change == "mask": clip["bodyRegion"]["width"] = 1
    elif change == "face": clip["coordinates"][0] = None
    elif change == "gap": segment["outputStartFrame"] = 1
    elif change == "reverse": segment["direction"] = -1
    with pytest.raises(ValueError):
        proposed(assets, edits)


def test_model_and_sample_limits_and_missing_reverse_camera_rejected():
    assets, edits, _ = assets_and_edits()
    edits[0]["cameraId"] = "missingReverse"
    with pytest.raises(ValueError): proposed(assets, edits)
    with pytest.raises(ValueError): proposed(core_frames=65)
    with pytest.raises(ValueError): proposed(context_frames=9)
    with pytest.raises(ValueError): proposed(seed=True)


def test_partial_final_frame_preserves_exact_master_samples_and_version_mismatch_rejected():
    assets, edits, samples = assets_and_edits()
    assets["plan"]["totalSamples"] = samples - 1
    result = build_expert_motion_plan(assets, edits, sample_rate=48000, total_samples=samples - 1)
    assert result["totalFrames"] == 300
    assert result["finalTrimSamples"] == result["segments"][-1]["coreEndSample"] == samples - 1
    assert all(s["audioSlice"]["endSample"] <= samples - 1 for s in result["segments"])
    assets["plan"]["packVersion"] = "other-version"
    with pytest.raises(ValueError, match="identity"):
        build_expert_motion_plan(assets, edits, sample_rate=48000, total_samples=samples - 1)


@pytest.mark.parametrize("camera,roles", [("dual", "AB"), ("otsA", "A"), ("otsB", "B")])
def test_workflow_replaces_long_loop_and_connects_true_v2v_plus_visible_audio(camera, roles):
    assets, _, _ = assets_and_edits()
    segment = proposed(assets, [{"cameraId": camera, "startFrame": 0, "endFrame": 300}])["segments"][0]
    original = template()
    before = copy.deepcopy(original)
    result = build_expert_motion_workflow(original, segment, names(roles), output_prefix="test-output")
    wf, trace = result["workflow"], result["controlTrace"]
    assert original == before
    assert wf["192"]["class_type"] == "WanVideoImageToVideoEncode"
    assert "mode" not in wf["192"]["inputs"] and "output_path" not in wf["192"]["inputs"]
    assert wf["199"]["inputs"]["samples"] == ["expert_reference_latents", 0]
    assert wf["199"]["inputs"]["multitalk_embeds"] == ["198", 0]
    assert wf["199"]["inputs"]["add_noise_to_samples"] is True
    assert wf["201"]["inputs"]["samples"] == ["199", 0]
    assert wf["229"]["inputs"]["images"] == ["201", 0]
    assert trace["visibleAudioRoles"] == list(roles)
    assert trace["runtimeCompatibilityVerified"] is False
    assert trace["referenceMotionRetentionVerified"] is False
    if len(roles) == 1:
        assert "audio_2" not in wf["198"]["inputs"]
        assert ("241" if roles == "A" else "218") not in wf
        assert wf["198"]["inputs"]["audio_1"] == ["218" if roles == "A" else "241", 0]
    else:
        assert wf["198"]["inputs"]["audio_2"] == ["241", 0]


@pytest.mark.parametrize("value", ["../reference.mp4", "C:\\private\\ref.mp4", "/tmp/ref.mp4", "https://host/ref.mp4", "ref.wav"])
def test_client_paths_or_wrong_reference_types_never_enter_workflow(value):
    segment = proposed()["segments"][0]
    bad = names()
    bad["referenceVideo"] = value
    with pytest.raises(ValueError):
        build_expert_motion_workflow(template(), segment, bad, output_prefix="safe")


def test_foreground_audio_is_not_silently_accepted_and_partial_denoise_must_be_explicit():
    assets, _, _ = assets_and_edits()
    segment = proposed(assets, [{"cameraId": "otsA", "startFrame": 0, "endFrame": 300}])["segments"][0]
    with pytest.raises(ValueError, match="visible"):
        build_expert_motion_workflow(template(), segment, names("AB"), output_prefix="safe")
    with pytest.raises(ValueError, match="partial"):
        build_expert_motion_workflow(template(), segment, names("A"), output_prefix="safe", start_step=0)
