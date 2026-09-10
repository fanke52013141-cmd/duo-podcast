"""任务接口（01 §12.1）：GET /api/jobs、GET /api/jobs/{id}、pause/cancel 返回真实状态。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _manager(request: Request) -> JobManager:
    return request.app.state.job_manager


@router.get("")
def list_jobs(request: Request, project_id: str | None = None) -> list[dict]:
    jobs = _manager(request).list(project_id)
    return [j.model_dump(mode="json", by_alias=True) for j in jobs]


@router.get("/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    job = _manager(request).get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.model_dump(mode="json", by_alias=True)


@router.post("/{job_id}/pause")
def pause_job(job_id: str, request: Request) -> dict:
    try:
        job = _manager(request).pause(job_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return job.model_dump(mode="json", by_alias=True)


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict:
    try:
        job = _manager(request).cancel(job_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return job.model_dump(mode="json", by_alias=True)
