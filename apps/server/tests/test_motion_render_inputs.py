import json

import pytest

from duocast.domain.motion_pack import MotionClip
from duocast.services.motion_render_inputs import coordinates_for_clip
from duocast.storage.artifacts import ArtifactStore


def setup_clip(tmp_path, data):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "coordinates.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    entry = artifacts.register("coords", "mixed", str(source), "test")
    clip = MotionClip(id="a", role="A", camera_id="dual", state="speaking",
                      media={"artifactId": "video", "fileHash": "a" * 64},
                      face_coordinates={"artifactId": "coords", "fileHash": entry["fileHash"]},
                      native_width=100, native_height=80, frame_count=3,
                      usable_start_frame=1, usable_end_frame=3)
    return artifacts, clip, source


def test_coords_only_require_boxes_in_selected_range(tmp_path):
    data = {"videoFileHash": "a" * 64, "role": "A", "boxes": [None, [1, 2, 20, 30], [2, 3, 21, 31]]}
    artifacts, clip, _ = setup_clip(tmp_path, data)
    assert coordinates_for_clip(artifacts, clip) == data["boxes"]


@pytest.mark.parametrize("change", [
    {"role": "B"}, {"videoFileHash": "b" * 64}, {"boxes": [[1, 2, 20, 30]]},
    {"boxes": [None, None, [2, 3, 21, 31]]},
    {"boxes": [None, [True, 2, 20, 30], [2, 3, 21, 31]]},
    {"boxes": [None, [1, 2, 101, 30], [2, 3, 21, 31]]},
])
def test_wrong_role_source_missing_or_invalid_boxes_rejected(tmp_path, change):
    data = {"videoFileHash": "a" * 64, "role": "A", "boxes": [None, [1, 2, 20, 30], [2, 3, 21, 31]]}
    artifacts, clip, _ = setup_clip(tmp_path, {**data, **change})
    with pytest.raises(ValueError):
        coordinates_for_clip(artifacts, clip)


def test_coordinates_tamper_and_missing_ref_rejected(tmp_path):
    artifacts, clip, source = setup_clip(tmp_path, {})
    source.write_text("changed")
    with pytest.raises(ValueError, match="内容已改变"):
        coordinates_for_clip(artifacts, clip)
    clip.face_coordinates = None
    with pytest.raises(ValueError, match="缺少"):
        coordinates_for_clip(artifacts, clip)


def test_camera_plan_uses_actual_reverse_views_on_integer_clock():
    from duocast.services.motion_render_inputs import camera_edit_plan
    assets = {"style": "overShoulder", "plan": {"fps": 25, "totalFrames": 250},
              "cameras": {"dual": {"mode": "twoShot", "subjectSpeaker": None},
                          "view-A": {"mode": "overShoulder", "subjectSpeaker": "A"},
                          "view-B": {"mode": "overShoulder", "subjectSpeaker": "B"}}}
    turns = [{"speaker": "A", "startSample": 0, "endSample": 48000},
             {"speaker": "B", "startSample": 48001, "endSample": 96000}]
    assert camera_edit_plan(assets, turns, 48000) == [
        {"cameraId": "view-A", "startFrame": 0, "endFrame": 26},
        {"cameraId": "view-B", "startFrame": 26, "endFrame": 250}]
    assets["style"] = "mixed"
    assert camera_edit_plan(assets, turns, 48000)[0] == {"cameraId": "dual", "startFrame": 0, "endFrame": 50}
    del assets["cameras"]["view-B"]
    with pytest.raises(ValueError, match="不完整"):
        camera_edit_plan(assets, turns, 48000)


def test_motion_pin_and_style_invalidate_visual_but_not_audio_dependency():
    from duocast.domain.project import Project, MotionPackPin
    from duocast.services.render_inputs import dependency_hash, visual_dependency_hash, audio_dependency_hash
    original = Project(id="episode", name="test")
    pinned = original.model_copy(update={"motion_pack": MotionPackPin(id="pack", version="v1")})
    changed = pinned.model_copy(update={"shot_style": "overShoulder"})
    assert len({dependency_hash(p) for p in (original, pinned, changed)}) == 3
    assert len({visual_dependency_hash(p) for p in (original, pinned, changed)}) == 3
    assert len({audio_dependency_hash(p) for p in (original, pinned, changed)}) == 1
