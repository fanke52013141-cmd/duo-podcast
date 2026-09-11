"""启动恢复（06 §6.3 / 02 §7.2 五步的最小落位）。

扫描 jobs/ 未结束任务：校验落盘输出 → 有 providerTaskId 的查询 → 结果不明标 unknown →
确认未提交或确定失败的重新派发。关口 B 起接入真实查询能力。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..core.logging import log
from ..domain.job import Job, JobStatus


def recover_on_startup(jobs_root: Path) -> list[Job]:
    recovered: list[Job] = []
    if not jobs_root.exists():
        return recovered
    for job_dir in jobs_root.iterdir():
        job_file = job_dir / "job.json"
        if not job_file.exists():
            continue
        try:
            data = json.loads(job_file.read_text(encoding="utf-8"))
            job = Job.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            log("recovery", "job manifest unreadable, marking unknown", job_dir=job_dir.name, error=str(exc))
            continue
        if job.status in (JobStatus.RUNNING, JobStatus.RECOVERING, JobStatus.PAUSE_REQUESTED,
                          JobStatus.CANCEL_REQUESTED):
            # 无定向查询能力的最小实现：标 unknown，避免误判重跑（02 §7.2 纪律）
            job.status = JobStatus.UNKNOWN
            temporary = job_file.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(job.model_dump(mode="json", by_alias=True), ensure_ascii=False),
                                 encoding="utf-8")
            os.replace(temporary, job_file)
        # 终态也装入管理器：历史任务与幂等键重启后仍然有效。
        recovered.append(job)
    log("recovery", "startup recovery scan done", recovered=len(recovered))
    return recovered
