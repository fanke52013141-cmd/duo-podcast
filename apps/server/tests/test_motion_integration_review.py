"""Cross-layer checks for pinned plans and legacy-independent render gating."""
import pytest

from duocast.domain.project import Project
from duocast.services.render_inputs import dependency_hash, visual_dependency_hash, validate_render_options
from duocast.services.motion_render_inputs import camera_edit_plan


def test_motion_binding_and_style_invalidate_both_dependency_hashes():
    project = Project(id="episode", generation_mode="fast",
                      motion_pack={"id": "pack", "version": "v1"})
    before = dependency_hash(project), visual_dependency_hash(project)
    project.shot_style = "mixed"
    assert before[0] != dependency_hash(project)
    assert before[1] != visual_dependency_hash(project)
    changed = dependency_hash(project), visual_dependency_hash(project)
    project.motion_pack.version = "v2"
    assert changed[0] != dependency_hash(project)
    assert changed[1] != visual_dependency_hash(project)


def test_actual_camera_edits_follow_sample_boundaries():
    assets = {"style": "mixed", "plan": {"totalFrames": 200, "fps": 25}, "cameras": {
        "dual": {"mode": "twoShot"},
        "otsA": {"mode": "overShoulder", "subjectSpeaker": "A"},
        "otsB": {"mode": "overShoulder", "subjectSpeaker": "B"}}}
    turns = [{"speaker": "A", "startSample": 0, "endSample": 96000},
             {"speaker": "B", "startSample": 96001, "endSample": 192000}]
    edits = camera_edit_plan(assets, turns, 48000)
    assert edits == [{"cameraId": "dual", "startFrame": 0, "endFrame": 50},
                     {"cameraId": "otsA", "startFrame": 50, "endFrame": 51},
                     {"cameraId": "otsB", "startFrame": 51, "endFrame": 200}]


def test_motion_pack_does_not_require_legacy_visual_variant(monkeypatch):
    monkeypatch.setattr("duocast.adapters.musetalk_video.validate_fast_options", lambda *args: None)
    project = Project(id="episode", generation_mode="fast",
                      motion_pack={"id": "pack", "version": "v1"})
    validate_render_options(project, {}, real_video=True)
