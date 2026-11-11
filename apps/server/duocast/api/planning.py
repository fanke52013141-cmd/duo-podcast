"""User-facing impact and estimates derived from actual project/media inputs."""
import shutil
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from ..services.voice_svc import build_units, _effective_bindings, effective_pronunciation

router = APIRouter(prefix="/api/projects/{project_id}/plan", tags=["planning"])


@router.get('/motion')
def motion_plan(project_id: str, request: Request):
    from ..domain.project import Project
    from ..services.motion_impact import motion_impact
    project = request.app.state.project_store.load(project_id)
    if project is None:
        raise HTTPException(404, '工程不存在')
    previous = None
    baseline_job_id = None
    previous_seed = 1
    for job in request.app.state.job_manager.list(project_id=project_id):
        if job.kind == 'renders' and job.status.value == 'succeeded' and not (job.result or {}).get('simulated'):
            snapshot = job.input_snapshot.get('projectSnapshot')
            if isinstance(snapshot, dict):
                try:
                    previous = Project.model_validate(snapshot)
                    baseline_job_id = job.id
                    previous_seed = job.input_snapshot.get('seed', 1)
                    break
                except ValueError:
                    continue
    dictionary = effective_pronunciation(project, request.app.state.machine.get_global_pronunciation())
    result = motion_impact(project, request.app.state.artifacts, request.app.state.motion_pack_store, previous,
                          previous_seed=previous_seed, pronunciation=dictionary)
    result['baselineJobId'] = baseline_job_id
    return result


@router.get("/render-segments")
def render_segment_plan(project_id: str, request: Request):
    """P0-C（42 号方案）：标准模式分段复用计划——真实数据，不再前端硬编码 0。

    以最近一次成功真实渲染为基线：当前依赖哈希与基线冻结输入一致，且对应
    shot 片段文件完整性（fileHash）通过，该段才标「可复用」。与渲染适配器
    的缓存判定同口径（5 秒段键 = round(start*rate)*1e8 + round(stop*rate)）。
    快速模式走 /plan/motion；过肩按话轮机位，不适用本计划。
    """
    import json
    from pathlib import Path
    from ..services.render_inputs import dependency_hash
    project = request.app.state.project_store.load(project_id)
    if project is None:
        raise HTTPException(404, "工程不存在")
    empty = {"available": False, "segments": [], "reusableCount": 0, "totalCount": 0}
    timeline = project.audio_timeline
    if not timeline or not timeline.units or timeline.sample_count <= 0:
        return {**empty, "reason": "尚无时间轨，合成配音后展示分段计划"}
    if project.generation_mode == "fast":
        return {**empty, "reason": "快速模式请使用动作复用计划"}
    artifacts = request.app.state.artifacts
    baseline = None
    for job in request.app.state.job_manager.list(project_id=project_id):
        meta = (job.result or {}).get("meta", {})
        if (job.kind == "renders" and job.status.value == "succeeded"
                and not (job.result or {}).get("simulated") and meta.get("motionDirectory")):
            baseline = job
            break
    if baseline is None:
        return {**empty, "reason": "尚无真实渲染基线，全部分段待生成"}
    meta = baseline.result["meta"]
    spec = project.render_spec
    inputs_match = (
        baseline.input_snapshot.get("dependencyHash") == dependency_hash(project)
        and baseline.input_snapshot.get("revisionId") == project.current_draft_revision
        and baseline.input_snapshot.get("resolution", "854x480") == spec.get("resolution", "854x480")
        and baseline.input_snapshot.get("fps", 25) == spec.get("fps", 25)
    )
    motion_dir = Path(meta["motionDirectory"])
    rate = timeline.sample_rate or 48000
    total = timeline.sample_count / rate
    segments = []
    start = 0.0
    while start < total - 0.001:
        stop = min(total, start + 5)
        shot_key = round(start * rate) * 100000000 + round(stop * rate)
        clip = motion_dir / f"shot_{shot_key}.mp4"
        marker = clip.with_suffix(".complete.json")
        file_ok = False
        if clip.is_file() and marker.is_file():
            try:
                saved = json.loads(marker.read_text(encoding="utf-8"))
                file_ok = saved.get("fileHash") == artifacts._hash_file(str(clip))
            except (ValueError, OSError):
                file_ok = False
        reusable = inputs_match and file_ok
        reason = ("输入与基线一致，片段完整性通过" if reusable
                  else "输入已变化，需重新生成" if not inputs_match
                  else "片段缺失或完整性改变")
        segments.append({"id": f"S{len(segments)+1:02d}", "startSec": start, "endSec": stop,
                         "reusable": reusable, "reason": reason})
        start = stop
    return {"available": True, "inputsMatch": inputs_match, "baselineJobId": baseline.id,
            "segments": segments, "reusableCount": sum(s["reusable"] for s in segments),
            "totalCount": len(segments),
            "notice": "复用以依赖哈希与片段文件完整性为准；实际渲染仍会逐段复核参考链。"}


@router.get("/delivery")
def delivery(project_id: str, request: Request):
    from ..services.delivery_check import delivery_check
    project = request.app.state.project_store.load(project_id)
    if project is None:
        raise HTTPException(404, "工程不存在")
    dictionary = effective_pronunciation(project, request.app.state.machine.get_global_pronunciation())
    return delivery_check(project, request.app.state.artifacts, dictionary)


@router.get("")
def plan(project_id: str, request: Request):
    project = request.app.state.project_store.load(project_id)
    if project is None:
        raise HTTPException(404, "工程不存在")
    revision = next((r for r in project.script_revisions if r.id == project.current_draft_revision), None)
    dictionary = effective_pronunciation(project, request.app.state.machine.get_global_pronunciation())
    expected = build_units(project, revision, _effective_bindings(project, {}), dictionary) if revision else []
    candidates = [u for t in [*project.audio_history, *([project.audio_timeline] if project.audio_timeline else [])] for u in t.units]
    assets = request.app.state.artifacts
    reusable = []
    for unit in expected:
        for old in candidates:
            if old.id != unit.id or old.pronunciation_revision != unit.pronunciation_revision or not old.adopted_audio_asset_id:
                continue
            entry = assets.by_id(old.adopted_audio_asset_id)
            if entry and entry.get("fileHash") and entry["fileHash"] == assets._hash_file(entry["path"]):
                reusable.append(unit.turn_id)
                break
    timeline = project.audio_timeline
    duration = timeline.sample_count / timeline.sample_rate if timeline and timeline.sample_rate else 0
    ratios = []
    spec = project.render_spec
    for job in request.app.state.job_manager.list():
        snapshot = job.input_snapshot
        meta = (job.result or {}).get("meta", {})
        if job.kind != "renders" or job.status.value != "succeeded" or (job.result or {}).get("simulated") or not job.started_at or not job.finished_at:
            continue
        if snapshot.get("projectSnapshot", {}).get("generationMode", "standard") != project.generation_mode or snapshot.get("resolution", "854x480") != spec.get("resolution", "854x480") or snapshot.get("fps", 25) != spec.get("fps", 25):
            continue
        seconds = meta.get("durationSec", 0)
        if seconds > 0:
            wall = (datetime.fromisoformat(job.finished_at)-datetime.fromisoformat(job.started_at)).total_seconds()
            if wall > 0:
                ratios.append(wall / seconds)
    estimated = [min(ratios)*duration, max(ratios)*duration] if ratios and duration else None
    reserve = max(256*1024**2, int(duration*12*1024**2))
    # P2-A（42 号方案）：影响文案按真实复用/待生成数量动态生成，不再是固定通用句
    gen_n = len(expected) - len(reusable)
    impact = (f"当前草稿共 {len(expected)} 条配音单元：{len(reusable)} 条可复用、{gen_n} 条待生成。"
              "修改脚本需重确认脚本与配音；调间隔复用声音重建时间轨；改变画面需重查样片。复用以实际媒体完整性为准。")
    return {"reusableTurnIds": reusable, "generateTurnIds": [u.turn_id for u in expected if u.turn_id not in reusable],
            "durationSec": duration, "estimateSeconds": estimated, "measurementCount": len(ratios),
            "estimatedTemporaryBytes": reserve, "freeBytes": shutil.disk_usage(assets.root).free,
            "impact": impact}
