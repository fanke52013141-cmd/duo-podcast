"""阶段七态推导（06 §3.2 stage_svc / 01 §13.1 权威：由产物版本与任务状态推导，前端不自算）。

简化实现：以工程内产物存在性与确认记录近似推导；完整规则见 01 §13.1 表格。
"""

from __future__ import annotations

from ..domain.job import JobStatus
from .render_inputs import dependency_hash, audio_inputs_match
from ..domain.project import Project, StageId, StageState

# 阶段 → 所需产物/确认（01 §13.1 精简）
REQUIREMENTS: dict[StageId, dict] = {
    "script": {"field": "script_revisions", "confirm_kind": "script"},
    "voice": {"field": "audio_timeline", "confirm_kind": "voice"},
    "visual": {"field": "visual_variants", "confirm_kind": "sample"},
    "render": {"field": "output_version", "confirm_kind": None},
}


def derive_stage_states(project: Project, jobs: list | None = None, pronunciation=None) -> dict[StageId, StageState]:
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
    current_hash = dependency_hash(project)
    demo_jobs = [job for job in (jobs or [])
                 if job.kind == "renders" and job.status == JobStatus.SUCCEEDED
                 and (job.result or {}).get("simulated")]
    # 演示渲染（simulated）不产出真实文件、也不写 output 指针，但它确实把流程走完了。
    # 阶段⑤ 若只认 selected_output_version，就会在「正文写演示完成」的同时显示「未准备」
    # （第五轮 E2E 实测矛盾）。这里按「演示任务 + 输入指纹」推导：一致=完成，变了=已过期。
    demo_render = any(
        (not (job.input_snapshot or {}).get("dependencyHash")
         or job.input_snapshot["dependencyHash"] == current_hash)
        for job in demo_jobs
    )
    demo_render_stale = bool(demo_jobs) and not demo_render
    has_render = bool(project.selected_output_version) or demo_render

    states["create"] = StageState.DONE if has_script else StageState.READY
    states["script"] = _stage(has_script, "script" in approved, "script.generate" in running_kinds)
    states["voice"] = _stage(has_voice, "voice" in approved, "tts.synthesize" in running_kinds)
    states["visual"] = _stage(has_visual, "sample" in approved, "visual.generate" in running_kinds)
    states["render"] = _stage(has_render, True, "renders" in running_kinds)
    if demo_render_stale and "renders" not in running_kinds:
        # 演示产物跑过、但脚本/配音/画面又改过：给出「已过期」，而不是退回「未准备」。
        states["render"] = StageState.STALE
    timeline = project.audio_timeline
    if timeline and "tts.synthesize" not in running_kinds:
        if timeline.revision_id != project.current_draft_revision:
            states["voice"] = StageState.STALE
        elif timeline.sample_rate <= 0 or timeline.sample_count <= 0 or timeline.sample_count > 300 * timeline.sample_rate:
            states["voice"] = StageState.NOT_READY
    if has_visual and timeline and timeline.revision_id != project.current_draft_revision:
        states["visual"] = StageState.STALE
    # render 过期传播（12 报告 U-3）：输出版本 OUT-{rev}-vN 内嵌脚本版本，
    # 草稿改动后旧成片必须显示「已过期」，不能保持「已就绪」。
    if has_render and project.output_version and project.current_draft_revision:
        rev_in_output = _output_revision(project.output_version)
        if rev_in_output and rev_in_output != project.current_draft_revision:
            states["render"] = StageState.STALE
    if timeline and timeline.sample_count > 0 and "tts.synthesize" not in running_kinds:
        revision = next((r for r in project.script_revisions if r.id == project.current_draft_revision), None)
        if revision:
            if not audio_inputs_match(project, pronunciation):
                states["voice"] = StageState.STALE
                states["visual"] = StageState.STALE
                if has_render:
                    states["render"] = StageState.STALE
    sample_approval = latest.get("sample")
    if sample_approval and sample_approval.spec.get("dependencyHash") and sample_approval.spec["dependencyHash"] != current_hash:
        states["visual"] = StageState.STALE
    output_hash = ((project.output_manifest or {}).get("meta") or {}).get("dependencyHash")
    if output_hash and output_hash != current_hash and "renders" not in running_kinds:
        states["render"] = StageState.STALE
    return states


def _output_revision(output_version: str) -> str:
    if not output_version.startswith("OUT-") or not output_version.rsplit("-v", 1)[-1].isdigit():
        return ""
    return output_version[4:].rsplit("-v", 1)[0]


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
