"""配音接口（01 §12.1）：POST /api/projects/{id}/voice/synthesize → Job（gpu 队列）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager

router = APIRouter(prefix="/api/projects/{project_id}/voice", tags=["voice"])


@router.post("/synthesize")
def synthesize(project_id: str, payload: dict, request: Request) -> dict:
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    if store.load(project_id) is None:
        raise HTTPException(404, "project not found")
    client_token = payload.get("clientToken") or f"tts-{uuid.uuid4().hex[:8]}"
    snapshot = {
        "revisionId": payload.get("revisionId") or "",
        "voiceBindings": payload.get("voiceBindings", {}),
        "transitionGapMs": payload.get("transitionGapMs", []),
        "projectId": project_id,
    }
    job = manager.submit(
        kind="tts.synthesize",
        project_id=project_id,
        queue_class="gpu",
        client_token=client_token,
        input_snapshot=snapshot,
    )
    return {"jobId": job.id, "clientToken": client_token}
