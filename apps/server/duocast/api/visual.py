"""视觉接口（01 §12.1）：POST /api/projects/{id}/visual/generate → Job（api 队列）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager

router = APIRouter(prefix="/api/projects/{project_id}/visual", tags=["visual"])


@router.post("/generate")
def generate(project_id: str, payload: dict, request: Request) -> dict:
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    if store.load(project_id) is None:
        raise HTTPException(404, "project not found")
    client_token = payload.get("clientToken") or f"img-{uuid.uuid4().hex[:8]}"
    snapshot = {
        "aspect": payload.get("aspect", "landscape"),
        "prompt": payload.get("prompt", ""),
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
