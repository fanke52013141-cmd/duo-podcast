"""任务接口（01 §12.1）：GET /api/jobs、GET /api/jobs/{id}、pause/cancel 返回真实状态。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..jobs.manager import JobManager

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _manager(request: Request) -> JobManager:
    return request.app.state.job_manager


@router.get("")
async def list_jobs(request: Request, project_id: str | None = Query(None, alias="projectId")) -> list[dict]:
    manager = _manager(request)
    jobs = manager.list(project_id)
    # 工程已删除的任务不再返回（第五轮验证：工程 ID 复用后，残留任务会让
    # 新工程在阶段①看到「幽灵任务已生成」的假状态；删除时已随工程入回收站，
    # 此处兜底过滤历史遗留数据）。
    store = request.app.state.project_store
    out = []
    for j in jobs:
        if store.load(j.project_id) is None:
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
