"""工程接口（01 §12.1）：GET/PATCH /api/projects/{id}，携带 expectedRevision 冲突保护。

阶段七态由 stage_svc 权威推导（前端不自算），在响应中覆盖 stageProgress 派生值（不落盘）。
PATCH 白名单与确认绑定校验为 11 报告 P1-4/P1-6/P2-5 的落点。
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from ..domain.project import Project
from ..services.stage_svc import derive_stage_states
from ..storage.project_store import ProjectAlreadyExists, ProjectRevisionConflict, ProjectStore

router = APIRouter(prefix="/api/projects", tags=["projects"])

# 客户端可修改的字段（11 报告 P2-5：拒绝注入 revision/id/audioTimeline 等内部字段）
ALLOWED_PATCH_FIELDS = {"title", "aspect", "scriptRevisions", "currentDraftRevision",
                        "approvals", "voiceBindings"}
# 三个默认确认点必须绑定当前草稿（11 报告 P1-6；stage_svc 的推导口径与此一致）
CONFIRM_KINDS = {"script", "voice", "sample"}
# 工程 id 白名单：字母/数字/下划线/连字符/空格/中文，不含路径分隔符与点号（P1-4 路径穿越）
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff][A-Za-z0-9 _\-\u4e00-\u9fff]{0,62}$")


def _init_store(request: Request) -> ProjectStore:
    return request.app.state.project_store


def _inject_states(request: Request, proj: Project) -> dict:
    jobs = request.app.state.job_manager.list(proj.id) if proj.id else []
    states = derive_stage_states(proj, jobs)
    data = proj.model_dump(mode="json", by_alias=True)
    data["stageProgress"] = {k: v.value for k, v in states.items()}
    return data


@router.get("")
async def list_projects(request: Request) -> list[dict]:
    store: ProjectStore = _init_store(request)
    ids = store.list_ids()
    return [_inject_states(request, store.load(i)) for i in ids if store.load(i)]


@router.post("")
async def create_project(request: Request, payload: dict) -> dict:
    store: ProjectStore = _init_store(request)
    title = payload.get("title", "未命名节目")
    # ID 白名单（P1-4 路径穿越）：显式 ID 先过正则，非法直接 422。
    project_id = payload.get("id")
    if project_id is not None:
        if not isinstance(project_id, str) or not PROJECT_ID_RE.match(project_id):
            raise HTTPException(
                422,
                f"非法工程 id: {project_id!r}（允许字母/数字/空格/连字符/下划线/中文，不含路径分隔符与点号）",
            )
    try:
        if project_id is not None:
            proj = store.create(project_id, title)
        else:
            # 未落盘工程、空目录及链接占位均占用编号，不能覆盖或复用。
            occupied = set(store.list_ids()) | {d.name for d in store.root.iterdir()}
            number = max(
                (int(i[2:]) for i in occupied if re.fullmatch(r"EP[0-9]+", i)),
                default=0,
            ) + 1
            while True:
                try:
                    proj = store.create(f"EP{number:03d}", title)
                    break
                except ProjectAlreadyExists:
                    number += 1
    except ProjectAlreadyExists as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors(
            include_url=False, include_context=False, include_input=False,
        )) from exc
    return _inject_states(request, proj)


@router.get("/{project_id}")
async def get_project(project_id: str, request: Request) -> dict:
    store: ProjectStore = _init_store(request)
    proj = store.load(project_id)
    if proj is None:
        raise HTTPException(404, "project not found")
    return _inject_states(request, proj)


def _validate_confirm_bindings(store: ProjectStore, project_id: str, approvals: list) -> None:
    """新增的默认确认点必须绑定当前草稿；历史确认原样透传（只追加不改写，01 §13.2）。"""
    current = store.load(project_id)
    if current is None:
        return
    existing = {
        json.dumps(a.model_dump(mode="json", by_alias=True), sort_keys=True, ensure_ascii=False)
        for a in current.approvals
    }
    for entry in approvals:
        if not isinstance(entry, dict) or entry.get("kind") not in CONFIRM_KINDS:
            continue
        key = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        if key in existing:
            continue
        if entry.get("inputRevisionId") != current.current_draft_revision:
            raise HTTPException(
                422,
                f"确认记录必须绑定当前草稿 {current.current_draft_revision}，"
                f"收到的是 {entry.get('inputRevisionId')!r}（kind={entry.get('kind')}，可能已过期）",
            )


@router.patch("/{project_id}")
async def patch_project(project_id: str, payload: dict, request: Request) -> dict:
    store: ProjectStore = _init_store(request)
    expected = payload.get("expectedRevision")
    patch = {k: v for k, v in payload.items() if k != "expectedRevision"}
    unknown = set(patch) - ALLOWED_PATCH_FIELDS
    if unknown:
        raise HTTPException(422, f"字段不允许修改: {sorted(unknown)}")
    if "approvals" in patch and isinstance(patch["approvals"], list):
        _validate_confirm_bindings(store, project_id, patch["approvals"])
    try:
        updated = store.apply(project_id, patch, expected_revision=expected)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ProjectRevisionConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors(
            include_url=False, include_context=False, include_input=False,
        )) from exc
    return _inject_states(request, updated)
