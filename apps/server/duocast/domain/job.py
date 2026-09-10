"""任务域模型（01 §11.1 + 02 §7.1 状态机）：Job / JobStatus / 转换表集中定义。"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .base import dc_config

JobStage = Literal["prepare", "infer", "download", "verify", "post"]
QueueClass = Literal["gpu", "api", "cpu"]


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RECOVERING = "recovering"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# 状态转换表（02 §7.1 权威；manager.py 为唯一执行者）
TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED, JobStatus.FAILED, JobStatus.UNKNOWN}),
    JobStatus.RUNNING: frozenset(
        {JobStatus.PAUSE_REQUESTED, JobStatus.CANCEL_REQUESTED, JobStatus.SUCCEEDED, JobStatus.FAILED,
         JobStatus.WAITING_CONFIRMATION, JobStatus.UNKNOWN, JobStatus.RECOVERING}
    ),
    JobStatus.PAUSE_REQUESTED: frozenset({JobStatus.PAUSED, JobStatus.FAILED, JobStatus.CANCEL_REQUESTED}),
    JobStatus.PAUSED: frozenset({JobStatus.QUEUED, JobStatus.CANCEL_REQUESTED, JobStatus.FAILED}),
    JobStatus.WAITING_CONFIRMATION: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.RECOVERING: frozenset({JobStatus.RUNNING, JobStatus.UNKNOWN, JobStatus.FAILED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCEL_REQUESTED: frozenset({JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.UNKNOWN}),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.UNKNOWN: frozenset({JobStatus.RUNNING, JobStatus.RECOVERING, JobStatus.FAILED, JobStatus.CANCELLED}),
}


class Job(BaseModel):
    model_config = dc_config()
    id: str
    project_id: str = ""
    kind: str = "generic"  # script.generate / tts.synthesize / renders / ...
    queue_class: QueueClass = "cpu"
    client_token: str = ""
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    stage: JobStage = "prepare"
    status: JobStatus = JobStatus.QUEUED
    provider_task_id: Optional[str] = None
    attempt: int = 0
    error: Optional[str] = None
    retryable: bool = False
    result: Optional[dict[str, Any]] = None  # {artifactIds: [...]}
    created_at: str = ""

    def can_transition(self, target: JobStatus) -> bool:
        return target in TRANSITIONS.get(self.status, frozenset())
