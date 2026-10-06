import copy
import pytest
from fastapi import HTTPException
from duocast.domain.project import Project, ScriptRevision, Turn, Line
from duocast.domain.audio import AudioTimeline
from duocast.domain.visual import VisualVariant
from duocast.services.voice_svc import build_units, binding_models
from duocast.services.render_inputs import dependency_hash, validate_render_options
from duocast.adapters.comfyui_video import ComfyUIVideoProvider

def project():
    p = Project(id="test", current_draft_revision="R1")
    rev = ScriptRevision(id="R1", turns=[Turn(id="T1", speaker="A", lines=[Line(id="L1", display_text="hello", spoken_text="hello")]), Turn(id="T2", speaker="B", lines=[Line(id="L2", display_text="yes", spoken_text="yes")])])
    p.script_revisions = [rev]
    p.visual_variants = [VisualVariant(id="V1")]
    bindings = {s:{"id":f"VB-{s}-default", "characterId":s, "providerProfileId":"comfyui-indextts"} for s in ("A","B")}
    p.voice_bindings = binding_models(bindings)
    units = build_units(p, rev, bindings)
    for u in units: u.sample_count = 48000
    p.audio_timeline = AudioTimeline(revision_id="R1", sample_count=112000, units=units, unit_offsets={"U-T1":0,"U-T2":64000})
    return p

def test_edit_same_revision_invalidates_audio_and_output():
    p = project()
    validate_render_options(p, {})
    old = dependency_hash(p)
    p.script_revisions[0].turns[0].lines[0].spoken_text = "changed"
    assert dependency_hash(p) != old
    with pytest.raises(HTTPException) as exc: validate_render_options(p, {})
    assert exc.value.status_code == 409

def test_approvals_do_not_invalidate_generated_motion():
    p = project(); before = dependency_hash(p)
    p.title = "rename"; p.revision += 1
    assert dependency_hash(p) == before
    p.camera_mode = "twoShot"
    assert dependency_hash(p) != before

def test_plan_assigns_closed_mouth_listener_from_actual_samples():
    p = project()
    plan = ComfyUIVideoProvider.performance_plan(p.model_dump(by_alias=True), "R1")
    assert [(x["speaker"],x["listener"]) for x in plan] == [("A","B"),("B","A")]
    assert plan[1]["start"] == 64000/48000
    assert all(x["mouth"] == "closed" for x in plan)

@pytest.mark.parametrize("options", [{"seed":-1},{"seed":True},{"cameraMode":"bad"},{"visualVariantId":"missing"},{"burnSubtitle":True},{"resolution":"3840x2160"},{"fps":24}])
def test_invalid_render_options_rejected_before_queue(options):
    with pytest.raises(HTTPException) as exc: validate_render_options(project(), options)
    assert exc.value.status_code == 422


def test_output_manifest_binds_version_to_actual_artifact(tmp_path):
    from duocast.storage.project_store import ProjectStore
    from duocast.services.render_svc import apply_output_version
    store = ProjectStore(tmp_path)
    p = store.create("out", "output")
    version = apply_output_version(store, p, "R1", {"artifactIds":["VID-real"],"meta":{"path":"real.mp4"}})
    saved = store.load("out")
    assert saved.output_manifest["version"] == version
    assert saved.output_manifest["artifactIds"] == ["VID-real"]


def test_same_revision_edit_marks_downstream_stages_stale():
    from duocast.services.stage_svc import derive_stage_states
    from duocast.domain.project import StageState
    p = project()
    p.selected_output_version = p.output_version = "OUT-R1-v1"
    p.script_revisions[0].turns[0].lines[0].spoken_text = "changed"
    states = derive_stage_states(p)
    assert states["voice"] == states["visual"] == states["render"] == StageState.STALE

def test_subtitle_turn_timestamps_handle_hours_and_milliseconds():
    from duocast.api.export import srt_time
    assert srt_time(14.880375) == "00:00:14,880"
    assert srt_time(3661.002) == "01:01:01,002"

def test_detailed_provider_stage_survives_job_reload():
    from duocast.domain.job import Job
    job = Job(id="stage", projectId="p", kind="renders", queueClass="gpu", clientToken="token", stage="motion 5.0–10.0s: cached")
    assert Job.model_validate(job.model_dump(by_alias=True)).stage == job.stage
