"""阶段⑤ 演示渲染推导：走完演示流程即视为有产物，输入变化后回到未准备。

第五轮 E2E 实测矛盾：正文写「流程演示完成」，阶段轨却是「未准备」。
"""

from duocast.domain.audio import AudioTimeline
from duocast.domain.job import Job, JobStatus
from duocast.domain.project import Line, Project, ScriptRevision, StageState, Turn
from duocast.domain.visual import VisualVariant
from duocast.services.render_inputs import dependency_hash
from duocast.services.stage_svc import derive_stage_states
from duocast.services.voice_svc import binding_models, build_units


def _project() -> Project:
    project = Project(id="stage5", current_draft_revision="R1")
    revision = ScriptRevision(id="R1", turns=[
        Turn(id="T1", speaker="A", lines=[Line(id="L1", display_text="你好", spoken_text="你好")]),
        Turn(id="T2", speaker="B", lines=[Line(id="L2", display_text="你好", spoken_text="你好")]),
    ])
    project.script_revisions = [revision]
    project.visual_variants = [VisualVariant(id="V1")]
    bindings = {s: {"id": f"VB-{s}", "characterId": s, "providerProfileId": "comfyui-indextts"} for s in ("A", "B")}
    project.voice_bindings = binding_models(bindings)
    units = build_units(project, revision, bindings)
    for unit in units:
        unit.sample_count = 48000
    project.audio_timeline = AudioTimeline(revision_id="R1", sample_count=112000, units=units,
                                           unit_offsets={"U-T1": 0, "U-T2": 64000})
    return project


def _demo_job(project: Project, *, hash_matches: bool = True) -> Job:
    return Job(
        id="JDemo", project_id=project.id, kind="renders", status=JobStatus.SUCCEEDED,
        input_snapshot={"revisionId": "R1", "dependencyHash": dependency_hash(project) if hash_matches else "stale"},
        result={"artifactIds": [], "simulated": True, "meta": {"outputVersion": "DEMO-R1-J1"}},
    )


def test_demo_render_marks_stage5_done():
    project = _project()
    states = derive_stage_states(project, [_demo_job(project)])
    assert states["render"] == StageState.DONE


def test_demo_render_with_stale_inputs_shows_stale():
    project = _project()
    states = derive_stage_states(project, [_demo_job(project, hash_matches=False)])
    assert states["render"] == StageState.STALE


def test_simulated_sample_does_not_count_as_render():
    project = _project()
    job = _demo_job(project)
    job.id = "JSample"
    job.kind = "sample.generate"
    assert derive_stage_states(project, [job])["render"] == StageState.NOT_READY
