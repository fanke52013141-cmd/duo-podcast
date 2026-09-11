"""工程接口（01 §12.1）：GET/PATCH /api/projects/{id}，携带 expectedRevision 冲突保护。

阶段七态由 stage_svc 权威推导（前端不自算），在响应中覆盖 stageProgress 派生值（不落盘）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..domain.project import Project
from ..services.stage_svc import derive_stage_states
from ..storage.project_store import ProjectRevisionConflict, ProjectStore

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _init_store(request: Request) -> ProjectStore:
    return request.app.state.project_store


def _inject_states(request: Request, proj: Project) -> dict:
    jobs = request.app.state.job_manager.list(proj.id) if proj.id else []
    states = derive_stage_states(proj, jobs)
    data = proj.model_dump(mode="json", by_alias=True)
    data["stageProgress"] = {k: v.value for k, v in states.items()}
    return data


@router.get("")
def list_projects(request: Request) -> list[dict]:
    store: ProjectStore = _init_store(request)
    ids = store.list_ids()
    return [_inject_states(request, store.load(i)) for i in ids if store.load(i)]


@router.post("")
async def create_project(request: Request, payload: dict) -> dict:
    store: ProjectStore = _init_store(request)
    project_id = payload.get("id") or f"EP{len(store.list_ids()) + 1:03d}"
    proj = store.create(project_id, payload.get("title", "未命名节目"))
    return _inject_states(request, proj)


@router.get("/{project_id}")
def get_project(project_id: str, request: Request) -> dict:
    store: ProjectStore = _init_store(request)
    proj = store.load(project_id)
    if proj is None:
        raise HTTPException(404, "project not found")
    return _inject_states(request, proj)


@router.patch("/{project_id}")
async def patch_project(project_id: str, payload: dict, request: Request) -> dict:
    store: ProjectStore = _init_store(request)
    expected = payload.get("expectedRevision")
    patch = {k: v for k, v in payload.items() if k != "expectedRevision"}
    try:
        updated = store.apply(project_id, patch, expected_revision=expected)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ProjectRevisionConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return _inject_states(request, updated)
