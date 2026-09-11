"""Job 生命周期（06 §6.1）：提交幂等 → 冻结输入快照 → 落盘意图 → 入队；状态机唯一执行者。"""

from __future__ import annotations

import asyncio
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..core.eventbus import EventBus
from ..core.logging import log
from ..domain.job import Job, JobStatus

# 任务执行体：async (job) -> {artifactIds: [...]}；适配器回调经 bus 发事件
JobRunner = Callable[[Job], Awaitable[dict[str, Any]]]


class JobManager:
    def __init__(
        self,
        root: Path,
        event_bus: EventBus,
        queue_caps: dict[str, int] | None = None,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.bus = event_bus
        self.queue_caps = queue_caps or {"gpu": 1, "api": 16, "cpu": 4}
        self._jobs: dict[str, Job] = {}
        self._queues: dict[str, asyncio.Queue] = {
            k: asyncio.Queue() for k in self.queue_caps
        }
        self._gpu_lock = asyncio.Lock()
        self._runner: Optional[JobRunner] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._dispatch_started = False
        self._workers: list[asyncio.Task] = []

    # ---- 生命周期 ----
    def set_runner(self, runner: JobRunner) -> None:
        """由 main.py 装配：runner 内部按 job.kind 分派到对应 service。"""
        self._runner = runner

    def start(self) -> None:
        if self._dispatch_started:
            return
        self._dispatch_started = True
        for queue_class, cap in self.queue_caps.items():
            # GPU 始终排他；API/CPU 上限是执行并发，不是等待队列长度。
            count = 1 if queue_class == "gpu" else max(1, cap)
            for _ in range(count):
                self._workers.append(asyncio.get_running_loop().create_task(self._worker(queue_class)))

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._dispatch_started = False

    def restore(self, jobs: list[Job]) -> None:
        for job in jobs:
            if job.id in self._jobs:
                continue
            self._jobs[job.id] = job
            if job.status == JobStatus.QUEUED:
                self._queues[job.queue_class].put_nowait(job)

    def submit(self, kind: str, project_id: str, queue_class: str, client_token: str,
               input_snapshot: dict[str, Any], attempt: int = 0) -> Job:
        # 幂等（01 §12.1：相同请求连续点击只创建一项任务）
        existing = self._find_by_token(client_token, project_id, kind)
        if existing is not None:
            return existing
        job = Job(
            id=f"J{uuid.uuid4().hex[:10].upper()}",
            project_id=project_id,
            kind=kind,
            queue_class=queue_class,
            client_token=client_token,
            input_snapshot=deepcopy(input_snapshot),
            attempt=attempt,
            created_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )
        self._jobs[job.id] = job
        self._persist(job)
        self.bus.publish("job.queued", jobId=job.id, kind=job.kind)
        self._queues[queue_class].put_nowait(job)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self, project_id: str | None = None) -> list[Job]:
        jobs = list(self._jobs.values())
        if project_id:
            jobs = [j for j in jobs if j.project_id == project_id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def _find_by_token(self, client_token: str, project_id: str, kind: str) -> Optional[Job]:
        if not client_token:
            return None
        return next((j for j in self._jobs.values() if j.client_token == client_token
                     and j.project_id == project_id and j.kind == kind), None)

    # ---- 状态机（唯一执行者） ----
    def transition(self, job: Job, target: JobStatus, **extra) -> bool:
        if not job.can_transition(target):
            # 非法转移只记日志，不再广播 job.failed（11 报告 P1-1：矛盾事件会让任务中心误判失败）
            log("jobs", "illegal transition rejected", jobId=job.id,
                current=job.status.value, target=target.value)
            return False
        job.status = target
        for k, v in extra.items():
            setattr(job, k, v)
        self._persist(job)
        self.bus.publish("job.stage", jobId=job.id, stage=job.stage, status=job.status.value)
        if target == JobStatus.SUCCEEDED:
            self.bus.publish("job.done", jobId=job.id, status=job.status.value,
                             artifactIds=(job.result or {}).get("artifactIds", []))
        elif target in (JobStatus.FAILED,):
            self.bus.publish("job.failed", jobId=job.id, status=job.status.value,
                             error=job.error or "", retryable=job.retryable)
        return True

    def set_stage(self, job: Job, stage: str, message: str | None = None) -> None:
        job.stage = stage
        self._persist(job)
        self.bus.publish("job.stage", jobId=job.id, stage=stage, message=message)

    def pause(self, job_id: str) -> Job:
        job = self._require(job_id)
        if job.status == JobStatus.QUEUED:
            self.transition(job, JobStatus.PAUSED)
        elif job.status == JobStatus.RUNNING:
            self.transition(job, JobStatus.PAUSE_REQUESTED)
        return job

    def resume(self, job_id: str) -> Job:
        job = self._require(job_id)
        if job.status == JobStatus.PAUSED:
            if job.result is not None:
                self.transition(job, JobStatus.SUCCEEDED)
            else:
                self.transition(job, JobStatus.QUEUED)
                self._queues[job.queue_class].put_nowait(job)
        return job

    def cancel(self, job_id: str) -> Job:
        job = self._require(job_id)
        if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED,
                          JobStatus.CANCEL_REQUESTED):
            return job  # 已在取消流程/终态：幂等返回，不重复转移（11 报告 P1-1）
        if job.status in (JobStatus.QUEUED, JobStatus.PAUSED, JobStatus.WAITING_CONFIRMATION):
            self.transition(job, JobStatus.CANCELLED)
            return job
        if job.status in (JobStatus.UNKNOWN, JobStatus.RECOVERING):
            return job  # 外部执行情况未核实时，不伪称已取消。
        self.transition(job, JobStatus.CANCEL_REQUESTED)
        return job

    # ---- 调度（三队列，06 §6.1） ----
    async def _worker(self, queue_class: str) -> None:
        queue = self._queues[queue_class]
        while True:
            job = await queue.get()
            try:
                if job.status != JobStatus.QUEUED:
                    continue
                self.transition(job, JobStatus.RUNNING)
                async with self._gpu_lock if queue_class == "gpu" else _null_ctx():
                    if self._runner is None:
                        raise RuntimeError("JobManager.runner not set")
                    self.set_stage(job, "infer")
                    result = await self._runner(job)
                    job.result = result
                    if job.status == JobStatus.CANCEL_REQUESTED:
                        self.transition(job, JobStatus.CANCELLED)
                    elif job.status == JobStatus.PAUSE_REQUESTED:
                        self.transition(job, JobStatus.PAUSED)
                    else:
                        self.transition(job, JobStatus.SUCCEEDED)
            except asyncio.CancelledError:
                # 停止本地等待并不证明外部推理停止。留给恢复流程核实。
                job.status = JobStatus.UNKNOWN
                self._persist(job)
                raise
            except Exception as exc:  # noqa: BLE001
                job.error = str(exc)
                job.retryable = False
                self.transition(job, JobStatus.FAILED)
            finally:
                queue.task_done()

    def _require(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        return job

    def _persist(self, job: Job) -> None:
        p = self.root / job.id / "job.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        import os

        os.replace(tmp, p)


class _null_ctx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False
