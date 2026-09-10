"""脚本生成接口（01 §12.1）：POST /api/projects/{id}/script/generate → Job。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager

router = APIRouter(prefix="/api/projects/{project_id}/script", tags=["script"])


@router.post("/generate")
def generate_script(project_id: str, payload: dict, request: Request) -> dict:
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    if store.load(project_id) is None:
        raise HTTPException(404, "project not found")
    client_token = payload.get("clientToken") or f"gen-{uuid.uuid4().hex[:8]}"
    snapshot = {
        "sourceInput": payload.get("sourceInput", {}),
        "brief": payload.get("brief", {}),
        "projectId": project_id,
    }
    job = manager.submit(
        kind="script.generate",
        project_id=project_id,
        queue_class="api",
        client_token=client_token,
        input_snapshot=snapshot,
    )
    return {"jobId": job.id, "clientToken": client_token}


@router.post("/rewrite")
def rewrite_script(project_id: str, payload: dict, request: Request) -> dict:
    """选区改写（01 §4.2）：返回候选改写与差异，接受后才替换（其他发言不自动重写）。"""
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    if store.load(project_id) is None:
        raise HTTPException(404, "project not found")
    client_token = payload.get("clientToken") or f"rw-{uuid.uuid4().hex[:8]}"
    snapshot = {
        "revisionId": payload.get("revisionId") or "",
        "turnIds": payload.get("turnIds", []),
        "mode": payload.get("mode", "rewrite"),
        "projectId": project_id,
    }
    job = manager.submit(
        kind="script.rewrite",
        project_id=project_id,
        queue_class="api",
        client_token=client_token,
        input_snapshot=snapshot,
    )
    return {"jobId": job.id, "clientToken": client_token}
