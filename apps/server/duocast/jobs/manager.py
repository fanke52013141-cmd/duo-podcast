"""Job 生命周期（06 §6.1）：提交幂等 → 冻结输入快照 → 落盘意图 → 入队；状态机唯一执行者。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..core.eventbus import EventBus
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
            k: asyncio.Queue(maxsize=cap) for k, cap in self.queue_caps.items()
        }
        self._gpu_lock = asyncio.Lock()
        self._runner: Optional[JobRunner] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._dispatch_started = False

    # ---- 生命周期 ----
    def set_runner(self, runner: JobRunner) -> None:
        """由 main.py 装配：runner 内部按 job.kind 分派到对应 service。"""
        self._runner = runner

    def start(self) -> None:
        if self._dispatch_started:
            return
        self._dispatch_started = True
        self._scheduler_task = asyncio.get_event_loop().create_task(self._dispatch_loop())

    def submit(self, kind: str, project_id: str, queue_class: str, client_token: str,
               input_snapshot: dict[str, Any], attempt: int = 0) -> Job:
        # 幂等（01 §12.1：相同请求连续点击只创建一项任务）
        existing = self._find_by_token(client_token)
        if existing is not None:
            return existing
        job = Job(
            id=f"J{uuid.uuid4().hex[:10].upper()}",
            project_id=project_id,
            kind=kind,
            queue_class=queue_class,
            client_token=client_token,
            input_snapshot=input_snapshot,
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

    def _find_by_token(self, client_token: str) -> Optional[Job]:
        if not client_token:
            return None
        return next((j for j in self._jobs.values() if j.client_token == client_token), None)

    # ---- 状态机（唯一执行者） ----
    def transition(self, job: Job, target: JobStatus, **extra) -> bool:
        if not job.can_transition(target):
            self.bus.publish("job.failed", jobId=job.id, error=f"illegal transition {job.status}->{target}",
                             retryable=False)
            return False
        job.status = target
        for k, v in extra.items():
            setattr(job, k, v)
        self._persist(job)
        if target == JobStatus.SUCCEEDED:
            self.bus.publish("job.done", jobId=job.id, artifactIds=(job.result or {}).get("artifactIds", []))
        elif target in (JobStatus.FAILED,):
            self.bus.publish("job.failed", jobId=job.id, error=job.error or "", retryable=job.retryable)
        return True

    def set_stage(self, job: Job, stage: str, message: str | None = None) -> None:
        job.stage = stage
        self._persist(job)
        self.bus.publish("job.stage", jobId=job.id, stage=stage, message=message)

    def pause(self, job_id: str) -> Job:
        job = self._require(job_id)
        self.transition(job, JobStatus.PAUSE_REQUESTED)
        return job

    def cancel(self, job_id: str) -> Job:
        job = self._require(job_id)
        if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
            return job
        self.transition(job, JobStatus.CANCEL_REQUESTED)
        return job

    # ---- 调度（三队列，06 §6.1） ----
    async def _dispatch_loop(self) -> None:
        # 简化轮询：逐个消费三类队列；GPU 队列以锁排他
        while True:
            item = await self._wait_any()
            if item is None:
                await asyncio.sleep(0.05)
                continue
            job, queue_class = item
            if job.status == JobStatus.CANCEL_REQUESTED:
                self.transition(job, JobStatus.CANCELLED)
                continue
            if job.status != JobStatus.QUEUED:
                continue
            self.transition(job, JobStatus.RUNNING)
            try:
                async with self._gpu_lock if queue_class == "gpu" else _null_ctx():
                    if self._runner is None:
                        raise RuntimeError("JobManager.runner not set")
                    self.set_stage(job, "infer")
                    result = await self._runner(job)
                    job.result = result
                    self.transition(job, JobStatus.SUCCEEDED)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                job.error = str(exc)
                job.retryable = False
                self.transition(job, JobStatus.FAILED)

    async def _wait_any(self):
        # 简单公平轮询（minimal）：优先 GPU/API/CPU 顺序各 peek 一次
        for qc in ("gpu", "api", "cpu"):
            q = self._queues[qc]
            try:
                job = q.get_nowait()
                return job, qc
            except asyncio.QueueEmpty:
                continue
        await asyncio.sleep(0.05)
        return None

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
