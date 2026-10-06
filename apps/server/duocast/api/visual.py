"""视觉接口（01 §12.1）：POST /api/projects/{id}/visual/generate → Job（api 队列）；
POST /api/projects/{id}/visual/sample → 样片任务 sample.generate（gpu 队列，01 §6.4）。"""

from __future__ import annotations

import uuid
import math

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager
from ..services.render_inputs import dependency_hash, validate_render_options, resolve_visual_id, resolve_camera_mode, validate_motion_source
from .voice import _merged_pronunciation

router = APIRouter(prefix="/api/projects/{project_id}/visual", tags=["visual"])


@router.post("/generate")
async def generate(project_id: str, payload: dict, request: Request) -> dict:
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    if store.load(project_id) is None:
        raise HTTPException(404, "project not found")
    client_token = payload.get("clientToken") or f"img-{uuid.uuid4().hex[:8]}"
    mode = payload.get("mode", "twoShot")
    if mode not in {"twoShot", "overShoulder"}:
        raise HTTPException(422, "mode 必须是 twoShot 或 overShoulder")
    subject = payload.get("subjectSpeaker")
    foreground = payload.get("foregroundSpeaker")
    if mode == "overShoulder":
        if subject not in {"A", "B"} or foreground not in {"A", "B"} or subject == foreground:
            raise HTTPException(422, "过肩机位必须指定不同的主体与前景角色（A/B）")
    snapshot = {
        "aspect": payload.get("aspect", "landscape"),
        "prompt": payload.get("prompt", ""),
        "mode": mode,
        "cameraGroupId": payload.get("cameraGroupId"),
        "cameraAssetId": payload.get("cameraAssetId"),
        "subjectSpeaker": subject,
        "foregroundSpeaker": foreground,
        "projectId": project_id,
    }
    job = manager.submit(
        kind="visual.generate",
        project_id=project_id,
        queue_class="api",
        client_token=client_token,
        input_snapshot=snapshot,
    )
    return {"jobId": job.id, "clientToken": client_token}


@router.post("/sample")
async def sample(project_id: str, payload: dict, request: Request) -> dict:
    """样片生成（01 §6.4：区间自动选取 A→B→A；确认点 3 之前的人工检查对象）。

    区间来自前端按时间轨计算的结果（秒），服务端只校验时间轨存在且区间合法。
    """
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    validate_render_options(project, payload, _merged_pronunciation(request, project),
                            real_video=not request.app.state.capabilities.video.get("simulated", True))
    validate_motion_source(project, payload, request.app.state.artifacts)
    timeline = project.audio_timeline
    if timeline is None or timeline.sample_count <= 0 or timeline.sample_rate <= 0:
        raise HTTPException(422, "请先生成配音时间轨，样片区间依赖音轨")
    start = payload.get("rangeStartSec")
    end = payload.get("rangeEndSec")
    total = timeline.sample_count / (timeline.sample_rate or 48000)
    if type(start) not in (int, float) or type(end) not in (int, float) or not math.isfinite(start) or not math.isfinite(end) \
            or not (0 <= start < end <= total + 0.001):
        raise HTTPException(422, f"非法样片区间 ({start}, {end})，音轨总长 {total:.1f}s")
    client_token = payload.get("clientToken") or f"smp-{uuid.uuid4().hex[:8]}"
    revision_id = payload.get("revisionId") or project.current_draft_revision
    if timeline.revision_id != revision_id or revision_id != project.current_draft_revision:
        raise HTTPException(409, "配音已过期，请先采用当前脚本对应的配音")
    snapshot = {
        "revisionId": revision_id,
        "cacheOnly": bool(payload.get("reuseMotionArtifactId") and payload.get("redoFromSec") is None),
        "projectSnapshot": project.model_dump(mode="json", by_alias=True),
        "dependencyHash": dependency_hash(project),
        "visualVariantId": resolve_visual_id(project, payload),
        "performanceMode": payload.get("performanceMode", project.performance_mode),
        "reuseMotionArtifactId": payload.get("reuseMotionArtifactId"),
        "redoFromSec": payload.get("redoFromSec"),
        "resumableVideo": True,
        "cameraMode": resolve_camera_mode(project, payload),
        "seed": payload.get("seed", 1),
        "rangeStartSec": float(start),
        "rangeEndSec": float(end),
        "aspect": payload.get("aspect", project.aspect),
        "projectId": project_id,
    }
    job = manager.submit(
        kind="sample.generate",
        project_id=project_id,
        queue_class="cpu" if snapshot["cacheOnly"] else "gpu",
        client_token=client_token,
        input_snapshot=snapshot,
    )
    return {"jobId": job.id, "clientToken": client_token}
