"""阶段七态推导（06 §3.2 stage_svc / 01 §13.1 权威：由产物版本与任务状态推导，前端不自算）。

简化实现：以工程内产物存在性与确认记录近似推导；完整规则见 01 §13.1 表格。
"""

from __future__ import annotations

from ..domain.job import JobStatus
from ..domain.project import Project, StageId, StageState

# 阶段 → 所需产物/确认（01 §13.1 精简）
REQUIREMENTS: dict[StageId, dict] = {
    "script": {"field": "script_revisions", "confirm_kind": "script"},
    "voice": {"field": "audio_timeline", "confirm_kind": "voice"},
    "visual": {"field": "visual_variants", "confirm_kind": "sample"},
    "render": {"field": "output_version", "confirm_kind": None},
}


def derive_stage_states(project: Project, jobs: list | None = None) -> dict[StageId, StageState]:
    states: dict[StageId, StageState] = {}
    running_kinds = {
        job.kind for job in (jobs or []) if job.status in (JobStatus.QUEUED, JobStatus.RUNNING,
                                                           JobStatus.PAUSE_REQUESTED, JobStatus.RECOVERING)
    }
    latest = {a.kind: a for a in project.approvals
              if a.input_revision_id == project.current_draft_revision}
    approved = {kind for kind, a in latest.items() if a.decision == "accepted"}
    has_script = bool(project.current_draft_revision)
    has_voice = bool(getattr(project, "audio_timeline", None))
    has_visual = bool(getattr(project, "visual_variants", None))
    has_render = bool(project.selected_output_version)

    states["create"] = StageState.DONE if has_script else StageState.READY
    states["script"] = _stage(has_script, "script" in approved, "script.generate" in running_kinds)
    states["voice"] = _stage(has_voice, "voice" in approved, "tts.synthesize" in running_kinds)
    states["visual"] = _stage(has_visual, "sample" in approved, "visual.generate" in running_kinds)
    states["render"] = _stage(has_render, True, "renders" in running_kinds)
    timeline = project.audio_timeline
    if timeline and "tts.synthesize" not in running_kinds:
        if timeline.revision_id != project.current_draft_revision:
            states["voice"] = StageState.STALE
        elif timeline.sample_rate <= 0 or timeline.sample_count <= 0 or timeline.sample_count > 300 * timeline.sample_rate:
            states["voice"] = StageState.NOT_READY
    if has_visual and timeline and timeline.revision_id != project.current_draft_revision:
        states["visual"] = StageState.STALE
    return states


def _stage(has_product: bool, confirmed: bool, running: bool) -> StageState:
    if running:
        return StageState.RUNNING
    if has_product and confirmed:
        return StageState.DONE
    if has_product and not confirmed:
        return StageState.PENDING_CONFIRM
    if has_product:
        return StageState.READY
    return StageState.NOT_READY
