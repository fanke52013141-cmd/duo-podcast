"""渲染接口（01 §12.1）：POST /api/projects/{id}/render/generate → Job（gpu 队列）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager

router = APIRouter(prefix="/api/projects/{project_id}/render", tags=["render"])


@router.post("/generate")
async def generate(project_id: str, payload: dict, request: Request) -> dict:
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    timeline = project.audio_timeline
    if timeline is None or timeline.sample_rate <= 0 or timeline.sample_count <= 0:
        raise HTTPException(422, "请先生成有效配音时间轨")
    if timeline.sample_count > 300 * timeline.sample_rate:
        raise HTTPException(422, "完整音轨超过 5 分钟，请先精简内容；不会自动截断")
    revision_id = payload.get("revisionId") or project.current_draft_revision
    if timeline.revision_id != revision_id or revision_id != project.current_draft_revision:
        raise HTTPException(409, "配音已过期，请先采用当前脚本对应的配音")
    client_token = payload.get("clientToken") or f"vid-{uuid.uuid4().hex[:8]}"
    snapshot = {
        "revisionId": revision_id,
        "projectSnapshot": project.model_dump(mode="json", by_alias=True),
        "resolution": payload.get("resolution", "1920x1080"),
        "fps": payload.get("fps", 30),
        "projectId": project_id,
    }
    job = manager.submit(
        kind="renders",
        project_id=project_id,
        queue_class="gpu",
        client_token=client_token,
        input_snapshot=snapshot,
    )
    return {"jobId": job.id, "clientToken": client_token}
