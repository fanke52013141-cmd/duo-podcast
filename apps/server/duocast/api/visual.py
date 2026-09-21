"""视觉接口（01 §12.1）：POST /api/projects/{id}/visual/generate → Job（api 队列）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager

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
