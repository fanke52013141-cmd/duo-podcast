"""任务接口（01 §12.1）：GET /api/jobs、GET /api/jobs/{id}、pause/cancel 返回真实状态。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from ..jobs.manager import JobManager
from ..storage.project_store import InvalidProjectInput

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _manager(request: Request) -> JobManager:
    return request.app.state.job_manager


def _project_exists(store, project_id: str) -> bool | None:
    """任务所属工程是否存在；None = 无法判定（损坏清单的占位任务）。

    占位任务的 project_id 可能是空串或非法 id，直接 store.load 会抛
    InvalidProjectInput，让整个 /api/jobs 变成 422。这类任务必须保留在列表里
    （否则它在 UI 上凭空消失），只是不参与工程过滤。
    """
    if not project_id:
        return None
    try:
        return store.load(project_id) is not None
    except InvalidProjectInput:
        return None


@router.get("")
async def list_jobs(request: Request, project_id: str | None = Query(None, alias="projectId"),
                    owner_type: Literal["project", "motionPack"] | None = Query(None, alias="ownerType"),
                    owner_id: str | None = Query(None, alias="ownerId")) -> list[dict]:
    manager = _manager(request)
    jobs = manager.list(project_id, owner_type=owner_type, owner_id=owner_id)
    # 工程已删除的任务不再返回（第五轮验证：工程 ID 复用后，残留任务会让
    # 新工程在阶段①看到「幽灵任务已生成」的假状态；删除时已随工程入回收站，
    # 此处兜底过滤历史遗留数据）。
    store = request.app.state.project_store
    out = []
    for j in jobs:
        # None = 占位任务（工程 id 损坏/为空）：保留在列表里，否则会在 UI 上凭空消失。
        if j.owner_type == "project" and _project_exists(store, j.project_id) is False:
            continue
        d = j.model_dump(mode="json", by_alias=True)
        # 排队位次（12 报告 C-1）：前端任务卡显示「队列第 N 位」
        d["queuePosition"] = manager.queue_position(j)
        out.append(d)
    return out


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> dict:
    manager = _manager(request)
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    d = job.model_dump(mode="json", by_alias=True)
    d["queuePosition"] = manager.queue_position(job)
    return d


@router.post("/{job_id}/pause")
async def pause_job(job_id: str, request: Request) -> dict:
    try:
        job = _manager(request).pause(job_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return job.model_dump(mode="json", by_alias=True)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> dict:
    try:
        job = _manager(request).cancel(job_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return job.model_dump(mode="json", by_alias=True)


@router.post("/{job_id}/resume")
async def resume_job(job_id: str, request: Request) -> dict:
    try:
        job = _manager(request).resume(job_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return job.model_dump(mode="json", by_alias=True)


@router.post("/{job_id}/abandon")
async def abandon_job(job_id: str, request: Request) -> dict:
    """UNKNOWN 任务的出路（12 报告 C-4）：放弃记账，转 FAILED。其他状态幂等返回。"""
    try:
        job = _manager(request).abandon(job_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return job.model_dump(mode="json", by_alias=True)
