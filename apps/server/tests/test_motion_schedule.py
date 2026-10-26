import pytest

from duocast.domain.motion_pack import MotionPack, clip_fingerprint
from duocast.services.motion_schedule import build_motion_schedule


@pytest.fixture
def pack():
    ref = {"artifactId": "image", "fileHash": "a" * 64}
    cameras = [{"id": "dual", "mode": "twoShot", "masterImage": ref}] + [
        {"id": "ots" + role, "mode": "overShoulder", "masterImage": ref,
         "subjectSpeaker": role, "foregroundSpeaker": "B" if role == "A" else "A"} for role in "AB"]
    clips = []
    for camera in cameras:
        for role in ("AB" if camera["mode"] == "twoShot" else camera["subjectSpeaker"]):
            for state in ("speaking", "listening", "responding"):
                for variant in range(2):
                    name = f"{camera['id']}-{role}-{state}-{variant}"
                    clips.append({"id": name, "role": role, "cameraId": camera["id"], "state": state,
                                  "media": {"artifactId": name, "fileHash": "b" * 64},
                                  "nativeWidth": 96, "nativeHeight": 54, "fps": 25,
                                  "frameCount": 100, "usableStartFrame": 10, "usableEndFrame": 85})
    value = MotionPack.model_validate({"id": "pack", "version": "pinned-v1", "revision": 1,
        "createdAt": "now", "updatedAt": "now", "name": "fixture", "cameras": cameras, "clips": clips,
        "characters": {s: {"characterId": s, "referenceImage": ref} for s in "AB"}})
    checks, reviews = {}, {}
    for clip in value.clips:
        fingerprint = clip_fingerprint(value, clip)
        checks[clip.id] = {"contentHash": fingerprint, "checkedAt": "now", "report": {
            "inputFileHash": clip.media.file_hash, "checksVersion": "test", "status": "needs_review",
            "native": {"width": 96, "height": 54, "fps": 25, "frames": 100, "durationSec": 4},
            "evidence": {"completeSourceFramesDecoded": 100, "analyzedStartFrame": 10, "analyzedEndFrame": 85}}}
        reviews[clip.id] = {"contentHash": fingerprint, "accepted": True, "reviewedAt": "now", "reason": "inspected"}
    return MotionPack.model_validate({**value.model_dump(by_alias=True), "checks": checks, "reviews": reviews})


def plan(pack, turns=None, **kwargs):
    return build_motion_schedule(pack, "mixed", turns or [], sample_rate=48000,
                                 total_samples=kwargs.pop("total_samples", 30 * 48000), **kwargs)


def test_thirty_seconds_three_cameras_all_source_frames_in_bounds(pack):
    result = plan(pack, [{"speaker": "A", "startSample": 0, "endSample": 240000},
                         {"speaker": "B", "startSample": 240000, "endSample": 480000}])
    assert result["totalFrames"] == 750
    assert len(result["cameras"]) == 3
    assert result["packVersion"] == "pinned-v1"
    for camera in result["cameras"]:
        for segments in camera["roles"].values():
            assert segments[0]["outputStartFrame"] == 0
            assert segments[-1]["outputEndFrame"] == 750
            for previous, current in zip(segments, segments[1:]):
                assert previous["outputEndFrame"] == current["outputStartFrame"]
                assert previous["clipId"] != current["clipId"]
            for segment in segments:
                assert segment["direction"] == 1
                assert len(segment["sourceFrames"]) == segment["outputEndFrame"] - segment["outputStartFrame"]
                assert 10 <= segment["sourceStartFrame"] < segment["sourceEndFrame"] <= 85
                assert all(10 <= frame < 85 for frame in segment["sourceFrames"])
    assert any(i["code"] == "SEAM_UNVERIFIED" for i in result["issues"])


def test_sample_boundary_state_and_replan_no_drift(pack):
    turns = [{"speaker": "A", "startSample": 48001, "endSample": 96001, "sampleCount": 48000}]
    first = plan(pack, turns)
    assert first == plan(pack, turns)
    segments = first["cameras"][0]["roles"]["A"]
    active = [s for s in segments if s["state"] == "speaking"]
    assert active[0]["outputStartFrame"] == 26
    assert active[-1]["outputEndFrame"] == 51
    for samples in (1440001, 90001, 48000):
        result = plan(pack, total_samples=samples)
        assert result["totalFrames"] == (samples * 25 + 47999) // 48000
    assert first == plan(pack, turns)


def test_responding_only_when_explicit(pack):
    assert all(s["state"] == "listening" for c in plan(pack)["cameras"] for ss in c["roles"].values() for s in ss)
    result = plan(pack, [{"speaker": "A", "startSample": 0, "endSample": 48000, "state": "responding"}])
    assert result["cameras"][0]["roles"]["A"][0]["state"] == "responding"


def test_metadata_measured_never_claims_naturalness(pack):
    metadata = {c.id: {"fileHash": c.media.file_hash, "frames": {
        str(frame): {"pose": [0, 1], "velocity": [1, 0]} for frame in range(10, 85)}} for c in pack.clips}
    result = plan(pack, seam_metadata=metadata)
    seams = [s["seam"] for c in result["cameras"] for ss in c["roles"].values() for s in ss][1:]
    measured = [s for s in seams if s["status"] == "measured"]
    assert measured and all(s["naturalnessVerified"] is False for s in measured)
    for value in metadata.values():
        value["fileHash"] = "c" * 64
    assert any(i["code"] == "SEAM_UNVERIFIED" for i in plan(pack, seam_metadata=metadata)["issues"])


def test_no_adoption_cannot_plan(pack):
    pack.reviews = {}
    with pytest.raises(ValueError, match="not ready"):
        plan(pack)


@pytest.mark.parametrize("turns", [[{"speaker": "A", "startSample": False, "endSample": 10}],
    [{"speaker": "A", "startSample": 10, "endSample": 20, "sampleCount": 11}],
    [{"speaker": "A", "startSample": 0, "endSample": 20}, {"speaker": "B", "startSample": 19, "endSample": 30}]])
def test_invalid_turns_rejected(pack, turns):
    with pytest.raises(ValueError):
        plan(pack, turns)


def test_output_rate_conversion_explicit_source_frames(pack):
    result = plan(pack, output_fps=30)
    assert result["totalFrames"] == 900
    for camera in result["cameras"]:
        for segments in camera["roles"].values():
            assert sum(len(s["sourceFrames"]) for s in segments) == 900
            assert all(max(s["sourceFrames"]) < 85 for s in segments)


def test_single_clip_repeat_is_explicit_and_never_out_of_bounds(pack):
    pack.clips = [c for c in pack.clips if c.id.endswith("-0")]
    result = plan(pack)
    assert any(i["code"] == "SINGLE_CLIP_REPEAT" for i in result["issues"])
    assert all(10 <= f < 85 for c in result["cameras"] for ss in c["roles"].values()
               for s in ss for f in s["sourceFrames"])


def test_explicit_response_requires_adopted_response_asset(pack):
    pack.clips = [c for c in pack.clips if c.state != "responding"]
    with pytest.raises(ValueError, match="No adopted responding"):
        plan(pack, [{"speaker": "A", "startSample": 0, "endSample": 48000, "state": "responding"}])
