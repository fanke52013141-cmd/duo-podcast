"""配音接口（01 §12.1）：POST /api/projects/{id}/voice/synthesize → Job（gpu 队列）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager
from ..services.voice_svc import find_revision

router = APIRouter(prefix="/api/projects/{project_id}/voice", tags=["voice"])


@router.post("/synthesize")
async def synthesize(project_id: str, payload: dict, request: Request) -> dict:
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        revision = find_revision(project, payload.get("revisionId"))
    except KeyError as exc:
        raise HTTPException(422, "没有可用脚本版本") from exc
    client_token = payload.get("clientToken") or f"tts-{uuid.uuid4().hex[:8]}"
    snapshot = {
        "revisionId": revision.id,
        "projectSnapshot": project.model_dump(mode="json", by_alias=True),
        "voiceBindings": payload.get("voiceBindings", {}),
        "transitionGapMs": payload.get("transitionGapMs"),
        "leadInMs": payload.get("leadInMs", 0),
        "tailOutMs": payload.get("tailOutMs", 0),
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
