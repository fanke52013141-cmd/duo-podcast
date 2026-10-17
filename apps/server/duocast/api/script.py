"""脚本生成接口（01 §12.1）：POST /api/projects/{id}/script/generate → Job。"""

from __future__ import annotations

import uuid
from copy import deepcopy

from fastapi import APIRouter, HTTPException, Request

from ..domain.project import ScriptRevision
from ..jobs.manager import JobManager
from ..services.script_svc import apply_script_revision, next_revision_id
from ..services.voice_svc import find_revision

router = APIRouter(prefix="/api/projects/{project_id}/script", tags=["script"])


@router.post("/generate")
async def generate_script(project_id: str, payload: dict, request: Request) -> dict:
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    client_token = payload.get("clientToken") or f"gen-{uuid.uuid4().hex[:8]}"
    snapshot = {
        "sourceInput": payload.get("sourceInput", {}),
        "brief": payload.get("brief", {}),
        "projectId": project_id,
        "expectedProjectRevision": project.revision,
    }
    job = manager.submit(
        kind="script.generate",
        project_id=project_id,
        queue_class="api",
        client_token=client_token,
        input_snapshot=snapshot,
    )
    return {"jobId": job.id, "clientToken": client_token}


@router.post("/branch")
async def branch_script(project_id: str, payload: dict, request: Request) -> dict:
    """从任一版本派生新草稿（F5 分支）。

    不引入独立的分支表：脚本版本本就不可变，父版本 + 分支名足以表达 DAG。
    新版本立刻成为当前草稿；原版本与它的确认记录原样保留。
    """
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        base = find_revision(project, payload.get("fromRevisionId"))
    except KeyError as exc:
        raise HTTPException(422, "源版本不存在") from exc
    revision = ScriptRevision(
        id=next_revision_id(project),
        source_input=deepcopy(base.source_input),
        content_brief=deepcopy(base.content_brief),
        turns=deepcopy(base.turns),
        text_api_config_ref=base.text_api_config_ref,
        parent_revision_id=base.id,
        label=str(payload.get("label") or "").strip()[:40],
    )
    saved = apply_script_revision(store, project_id, revision,
                                  expected_project_revision=project.revision)
    return {"revisionId": revision.id, "parentRevisionId": base.id,
            "label": revision.label, "currentDraftRevision": saved.current_draft_revision}


@router.post("/rewrite")
async def rewrite_script(project_id: str, payload: dict, request: Request) -> dict:
    """选区改写（01 §4.2）：返回候选改写与差异，接受后才替换（其他发言不自动重写）。"""
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        revision = find_revision(project, payload.get("revisionId"))
    except KeyError as exc:
        raise HTTPException(422, "没有可用脚本版本") from exc
    client_token = payload.get("clientToken") or f"rw-{uuid.uuid4().hex[:8]}"
    snapshot = {
        "revisionId": revision.id,
        "scriptSnapshot": revision.model_dump(mode="json", by_alias=True),
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
