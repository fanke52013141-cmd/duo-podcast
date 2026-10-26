"""Job 生命周期（06 §6.1）：提交幂等 → 冻结输入快照 → 落盘意图 → 入队；状态机唯一执行者。"""

from __future__ import annotations

import asyncio
import heapq
import json
import os
import re
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..core.eventbus import EventBus
from ..core.logging import log
from ..domain.job import Job, JobStatus
from .control import JobInterrupted

# 任务执行体：async (job) -> {artifactIds: [...]}；适配器回调经 bus 发事件
JobRunner = Callable[[Job], Awaitable[dict[str, Any]]]

# kind → 默认优先级（12 报告 C-1：整期渲染排后，交互链路任务插队先跑，数值小者先）
KIND_PRIORITY: dict[str, int] = {"renders": 10}


class _PriorityQueue:
    """类内 (priority, created_at, 入队序) 最小堆。取代 asyncio.Queue 的纯 FIFO，
    支持「样片/脚本先于整期渲染」的排队策略；取出后状态非 queued 的副本由 worker 丢弃。"""

    def __init__(self) -> None:
        self._heap: list[tuple[int, str, int, Job]] = []
        self._counter = 0
        self._event = asyncio.Event()

    def put(self, job: Job) -> None:
        heapq.heappush(self._heap, (job.priority, job.created_at, self._counter, job))
        self._counter += 1
        self._event.set()

    def __len__(self) -> int:
        return len(self._heap)

    def position_of(self, job_id: str) -> int | None:
        """堆内排位（1 起）。近似值：堆的弹出顺序受后续入队影响，仅用于「排第几」展示。"""
        ordered = sorted(self._heap)
        for i, entry in enumerate(ordered):
            if entry[3].id == job_id:
                return i + 1
        return None

    def discard(self, job_id: str) -> int:
        """从堆里摘掉指定任务（工程删除时用），返回摘除条数。

        只删 _jobs 不清队列的话，worker 仍会取出该 Job 照常执行并重建落盘目录。
        """
        kept = [entry for entry in self._heap if entry[3].id != job_id]
        removed = len(self._heap) - len(kept)
        if removed:
            self._heap = kept
            heapq.heapify(self._heap)
        return removed

    async def get(self) -> Job:
        while True:
            if self._heap:
                return heapq.heappop(self._heap)[3]
            self._event.clear()
            if self._heap:  # clear 前已有入队
                continue
            await self._event.wait()


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
        # 工程删除后进入此集合：队列摘除 + 收尾不再落盘，防止「任务复活」。
        self._discarded: set[str] = set()
        self._queues: dict[str, _PriorityQueue] = {k: _PriorityQueue() for k in self.queue_caps}
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

    def _resolve_class(self, queue_class: str) -> str:
        # 磁盘旧数据/未知类别回落 cpu 而不是启动崩溃（12 报告 C-2 兼容项）
        if queue_class not in self._queues:
            log("jobs", "unknown queue_class, fallback to cpu", queue_class=queue_class)
            return "cpu"
        return queue_class

    def restore(self, jobs: list[Job]) -> None:
        for job in jobs:
            if job.id in self._jobs:
                continue
            job.queue_class = self._resolve_class(job.queue_class)
            self._jobs[job.id] = job
            if job.status == JobStatus.QUEUED:
                self._queues[job.queue_class].put(job)

    def submit(self, kind: str, project_id: str, queue_class: str, client_token: str,
               input_snapshot: dict[str, Any], attempt: int = 0,
               owner_type: str = "project", owner_id: str = "") -> Job:
        if owner_type not in {"project", "motionPack"}:
            raise ValueError("Unknown job owner type")
        if owner_type == "motionPack":
            if project_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", owner_id):
                raise ValueError("Motion-pack jobs require a pack owner and no project ID")
        else:
            if owner_id and owner_id != project_id:
                raise ValueError("Project job owner must match project ID")
            owner_id = project_id
        # 幂等（01 §12.1：相同请求连续点击只创建一项任务）
        existing = self._find_by_token(client_token, project_id, kind, owner_type, owner_id)
        if existing is not None:
            return existing
        queue_class = self._resolve_class(queue_class)
        job = Job(
            id=f"J{uuid.uuid4().hex[:10].upper()}",
            project_id=project_id,
            owner_type=owner_type,
            owner_id=owner_id,
            kind=kind,
            queue_class=queue_class,
            client_token=client_token,
            input_snapshot=deepcopy(input_snapshot),
            attempt=attempt,
            priority=KIND_PRIORITY.get(kind, 0),
            created_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )
        self._jobs[job.id] = job
        self._persist(job)
        self.bus.publish("job.queued", jobId=job.id, kind=job.kind, **self._owner_fields(job))
        self._queues[queue_class].put(job)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self, project_id: str | None = None, *, owner_type: str | None = None,
             owner_id: str | None = None) -> list[Job]:
        jobs = list(self._jobs.values())
        if project_id:
            jobs = [j for j in jobs if j.owner_type == "project" and j.project_id == project_id]
        if owner_type is not None:
            jobs = [j for j in jobs if j.owner_type == owner_type]
        if owner_id is not None:
            jobs = [j for j in jobs if (j.owner_id or j.project_id) == owner_id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def purge_project(self, project_id: str, trash_dir) -> int:
        """工程删除时把其全部任务记录移入回收站（第五轮验证发现：工程 ID 复用后，
        新工程会看到被删工程的成功任务，阶段①被「幽灵任务」误标为已生成）。

        内存态剔除 + 磁盘任务目录整体搬移；返回搬移的任务数。trash_dir 由调用方给到
        本次删除对应的回收站目录，任务记录与工程数据放同一处，便于一起恢复。
        """
        moved = 0
        trash_path = Path(trash_dir)
        for job in [j for j in self._jobs.values() if j.owner_type == "project" and j.project_id == project_id]:
            # 先登记为丢弃：运行中的任务收尾时不再落盘，排队的任务出队后直接跳过。
            self._discarded.add(job.id)
            for queue in self._queues.values():
                queue.discard(job.id)
            if job.status in (JobStatus.RUNNING, JobStatus.PAUSE_REQUESTED, JobStatus.CANCEL_REQUESTED):
                # 让 worker 在 runner 返回后走 CANCEL_REQUESTED → CANCELLED 分支（不落盘）。
                job.status = JobStatus.CANCEL_REQUESTED
            src = self.root / job.id
            if src.exists():
                (trash_path / "jobs").mkdir(parents=True, exist_ok=True)
                dest = trash_path / "jobs" / job.id
                try:
                    os.replace(src, dest)
                except OSError:
                    import shutil
                    shutil.move(str(src), str(dest))
            self._jobs.pop(job.id, None)
            moved += 1
        return moved

    def _find_by_token(self, client_token: str, project_id: str, kind: str,
                       owner_type: str = "project", owner_id: str | None = None) -> Optional[Job]:
        if not client_token:
            return None
        expected_owner = project_id if owner_id is None else owner_id
        return next((j for j in self._jobs.values() if j.client_token == client_token
                     and j.project_id == project_id and j.kind == kind
                     and j.owner_type == owner_type
                     and (j.owner_id or j.project_id) == expected_owner), None)

    @staticmethod
    def _owner_fields(job: Job) -> dict[str, str]:
        return {"projectId": job.project_id, "ownerType": job.owner_type,
                "ownerId": job.owner_id or job.project_id}

    # ---- 状态机（唯一执行者） ----
    _TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})

    def transition(self, job: Job, target: JobStatus, **extra) -> bool:
        if not job.can_transition(target):
            # 非法转移只记日志，不再广播 job.failed（11 报告 P1-1：矛盾事件会让任务中心误判失败）
            log("jobs", "illegal transition rejected", jobId=job.id,
                current=job.status.value, target=target.value)
            return False
        job.status = target
        if target == JobStatus.RUNNING:
            job.started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        # 终态记录完成时刻；非终态（如恢复重跑回 RUNNING）视为进行中，清空旧值。
        if target in self._TERMINAL_STATUSES:
            job.finished_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        else:
            job.finished_at = None
        for k, v in extra.items():
            setattr(job, k, v)
        self._persist(job)
        self.bus.publish("job.stage", jobId=job.id, stage=job.stage, status=job.status.value,
                         **self._owner_fields(job))
        if target == JobStatus.SUCCEEDED:
            self.bus.publish("job.done", jobId=job.id, status=job.status.value,
                             artifactIds=(job.result or {}).get("artifactIds", []), **self._owner_fields(job))
        elif target in (JobStatus.FAILED,):
            self.bus.publish("job.failed", jobId=job.id, status=job.status.value,
                             error=job.error or "", retryable=job.retryable, **self._owner_fields(job))
        return True

    def set_stage(self, job: Job, stage: str, message: str | None = None) -> None:
        self.checkpoint(job)
        job.stage = stage
        self._persist(job)
        self.bus.publish("job.stage", jobId=job.id, stage=stage, message=message,
                         **self._owner_fields(job))

    def checkpoint(self, job: Job) -> None:
        if job.id in self._discarded or job.status in (JobStatus.CANCEL_REQUESTED, JobStatus.PAUSE_REQUESTED):
            raise JobInterrupted()

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
                self._queues[self._resolve_class(job.queue_class)].put(job)
        return job

    def abandon(self, job_id: str) -> Job:
        """UNKNOWN 出路（12 报告 C-4）：外部执行状态无法核实时，允许用户放弃记账，
        转入 FAILED（retryable=False）而不是永久滞留「进行中」。"""
        job = self._require(job_id)
        if job.status != JobStatus.UNKNOWN:
            return job  # 幂等：非 UNKNOWN 状态不伪称放弃
        self.transition(job, JobStatus.FAILED,
                        error=job.error or "已放弃：外部任务状态无法核实", retryable=False)
        return job

    def queue_position(self, job: Job) -> int | None:
        """queued 任务的队内排位（1 起）；不在队中返回 None。"""
        if job.status != JobStatus.QUEUED:
            return None
        return self._queues[self._resolve_class(job.queue_class)].position_of(job.id)

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

    def resume_external(self, job_id: str) -> Job:
        """Resume querying one known motion I2V task; never submit a new task.

        This explicit recovery operation is restricted to server-frozen motion
        jobs. It does not relax ordinary terminal-state retry semantics.
        """
        from ..domain.motion_pack import content_hash
        job = self._require(job_id)
        record = job.completed_calls.get("motionPreparation", {})
        workflow = job.input_snapshot.get("workflow")
        if (job.kind != "motion.prepare" or job.owner_type != "motionPack"
                or not job.input_snapshot.get("resumableMotionPreparation")
                or not isinstance(workflow, dict) or not record.get("promptId")
                or record.get("promptId") != job.provider_task_id
                or record.get("workflowHash") != content_hash(workflow)
                or record.get("externalTerminalFailure")):
            raise ValueError("No verified resumable external motion task")
        if job.status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RECOVERING, JobStatus.SUCCEEDED):
            return job
        if job.status not in (JobStatus.FAILED, JobStatus.UNKNOWN, JobStatus.PAUSED):
            raise ValueError("Motion task cannot be resumed from current status")
        # A completed local wait may be FAILED while its paid external task is
        # still running. Reopen that same query, preserving its external ID.
        job.status = JobStatus.QUEUED
        job.stage = "recovering known external motion task"
        job.error, job.finished_at, job.retryable = None, None, False
        self._persist(job)
        self.bus.publish("job.queued", jobId=job.id, kind=job.kind, **self._owner_fields(job))
        self._queues[job.queue_class].put(job)
        return job

    # ---- 调度（三队列，06 §6.1） ----
    async def _worker(self, queue_class: str) -> None:
        queue = self._queues[queue_class]
        while True:
            job = await queue.get()
            try:
                # 取出瞬间工程可能已被删除：直接丢弃，不执行也不落盘。
                if job.id in self._discarded:
                    continue
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
            except JobInterrupted:
                self.transition(job, JobStatus.PAUSED if job.status == JobStatus.PAUSE_REQUESTED else JobStatus.CANCELLED)
            except asyncio.CancelledError:
                # 停止本地等待并不证明外部推理停止。留给恢复流程核实。
                job.status = JobStatus.UNKNOWN
                self._persist(job)
                raise
            except Exception as exc:  # noqa: BLE001
                job.error = str(exc)
                job.retryable = False
                self.transition(job, JobStatus.FAILED)

    def _require(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        return job

    def _persist(self, job: Job) -> None:
        # 工程已删除、任务已移入回收站：worker 若还在跑，收尾不得把 job.json 写回来，
        # 否则被删工程的任务会「复活」（第五轮幽灵任务的第二层根因）。
        if job.id in self._discarded:
            return
        p = self.root / job.id / "job.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        # 唯一临时文件名：不跟随预置的 project.json.tmp 之类的固定名链接，避免链接攻击。
        fd = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".job-", suffix=".json.tmp",
            dir=str(p.parent), delete=False,
        )
        tmp_path = Path(fd.name)
        try:
            with fd:
                fd.write(json.dumps(job.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2))
            os.replace(tmp_path, p)
        finally:
            tmp_path.unlink(missing_ok=True)


class _null_ctx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False
