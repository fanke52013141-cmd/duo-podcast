"""User-facing impact and estimates derived from actual project/media inputs."""
import shutil
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from ..services.voice_svc import build_units, _effective_bindings, effective_pronunciation

router = APIRouter(prefix="/api/projects/{project_id}/plan", tags=["planning"])


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
    return {"reusableTurnIds": reusable, "generateTurnIds": [u.turn_id for u in expected if u.turn_id not in reusable],
            "durationSec": duration, "estimateSeconds": estimated, "measurementCount": len(ratios),
            "estimatedTemporaryBytes": reserve, "freeBytes": shutil.disk_usage(assets.root).free,
            "impact": "修改脚本需重确认脚本与配音；调间隔复用声音重建时间轨；改变画面需重查样片。复用以实际媒体完整性为准。"}
