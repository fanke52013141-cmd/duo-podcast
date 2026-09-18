"""12 报告（第三轮审查）修复项回归测试。全部使用临时目录，不依赖运行中的服务。

覆盖：
- C-1 排队策略：kind 默认优先级、堆弹出顺序（优先级小者先，同优先级 FIFO）、队内位次
- C-2 queue_class 兼容：未知类别回落 cpu（submit / restore / _resolve_class）
- C-3 队列上限环境变量化（DUOCAST_QUEUE_* / DUOCAST_PROJECT_DEBOUNCE_MS）
- C-4 UNKNOWN 出路：manager.abandon → FAILED；非 UNKNOWN 幂等
- C-5 事件序号跨重启续接（EventBus.resume_from_log）
- C-6 recovery 原子写：唯一临时名、结束后无 .tmp 残留、内容可读回
- U-3 render 阶段过期传播（stage_svc：OUT-{rev}-vN 与当前草稿不一致 → stale）
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from duocast.core.config import Settings
from duocast.core.eventbus import EventBus
from duocast.domain.job import Job, JobStatus
from duocast.jobs.manager import KIND_PRIORITY, JobManager
from duocast.jobs.recovery import recover_on_startup


def manager_fixture(tmp_path: Path) -> JobManager:
    bus = EventBus(tmp_path / "events.jsonl")
    return JobManager(tmp_path / "jobs", bus, {"gpu": 1, "api": 2, "cpu": 1})


# ---------- C-1 排队策略 ----------

def test_submit_assigns_kind_priority(tmp_path):
    """renders 默认低优先级（数值大），交互任务默认 0。"""
    manager = manager_fixture(tmp_path)
    render_job = manager.submit("renders", "p1", "gpu", "t-r", {})
    script_job = manager.submit("script.generate", "p1", "api", "t-s", {})
    assert render_job.priority == KIND_PRIORITY["renders"] > 0
    assert script_job.priority == 0


def test_heap_pops_priority_first_then_fifo(tmp_path):
    """同队列内：优先级小的先弹出；同优先级按提交时间 FIFO。"""
    async def scenario():
        manager = manager_fixture(tmp_path)
        # 不起 worker，手工构造受控入队顺序与时间戳
        q = manager._queues["api"]
        jobs = [
            Job(id="Jlow-old", project_id="p", kind="script.generate", priority=0,
                created_at="2026-01-01T00:00:00.000+00:00"),
            Job(id="Jlow-new", project_id="p", kind="script.generate", priority=0,
                created_at="2026-01-01T00:00:01.000+00:00"),
            Job(id="Jhigh", project_id="p", kind="renders", priority=10,
                created_at="2025-12-31T00:00:00.000+00:00"),
        ]
        for j in jobs:
            q.put(j)
        order = [(await q.get()).id for _ in jobs]
        assert order == ["Jlow-old", "Jlow-new", "Jhigh"]
    asyncio.run(scenario())


def test_queue_position_reports_heap_rank(tmp_path):
    """queued 任务返回队内位次；非 queued 返回 None。"""
    manager = manager_fixture(tmp_path)
    a = manager.submit("script.generate", "p", "api", "ta", {})
    b = manager.submit("renders", "p", "api", "tb", {})
    assert manager.queue_position(a) == 1
    assert manager.queue_position(b) == 2  # 低优先级排后
    manager.pause(a.id)
    assert manager.queue_position(a) is None  # paused 不再占位展示


# ---------- C-2 queue_class 兼容 ----------

def test_unknown_queue_class_falls_back_to_cpu(tmp_path):
    """磁盘旧数据带未知队列类别不再让启动崩溃（12 报告 C-2）。"""
    manager = manager_fixture(tmp_path)
    assert manager._resolve_class("tpu") == "cpu"
    job = Job(id="JX1", project_id="p", kind="legacy", status=JobStatus.QUEUED)
    object.__setattr__(job, "queue_class", "tpu")  # 绕过 Literal，模拟磁盘脏数据
    manager.restore([job])
    assert job.queue_class == "cpu"
    assert len(manager._queues["cpu"]) == 1
    # submit 路径同样兜底
    submitted = manager.submit("script.generate", "p", "quantum", "tz", {})
    assert submitted.queue_class == "cpu"


# ---------- C-3 队列上限环境变量 ----------

def test_settings_env_override_queue_caps(monkeypatch):
    monkeypatch.setenv("DUOCAST_QUEUE_GPU", "2")
    monkeypatch.setenv("DUOCAST_QUEUE_API", "8")
    monkeypatch.setenv("DUOCAST_QUEUE_CPU", "abc")   # 非法值回默认
    monkeypatch.setenv("DUOCAST_PROJECT_DEBOUNCE_MS", "0")  # 非正数回默认
    s = Settings.from_env()
    assert s.queue_cap_gpu == 2
    assert s.queue_cap_api == 8
    assert s.queue_cap_cpu == 4
    assert s.project_debounce_ms == 500


# ---------- C-4 UNKNOWN 出路 ----------

def test_abandon_moves_unknown_to_failed(tmp_path):
    manager = manager_fixture(tmp_path)
    job = manager.submit("renders", "p", "gpu", "t1", {})
    manager.transition(job, JobStatus.RUNNING)
    manager.transition(job, JobStatus.UNKNOWN)
    done = manager.abandon(job.id)
    assert done.status == JobStatus.FAILED
    assert done.retryable is False
    assert done.error
    # 幂等：终态再调用不改变状态
    assert manager.abandon(job.id).status == JobStatus.FAILED


def test_abandon_is_noop_for_running(tmp_path):
    """非 UNKNOWN 状态放弃无效果，不伪称处理。"""
    manager = manager_fixture(tmp_path)
    job = manager.submit("tts.synthesize", "p", "gpu", "t1", {})
    manager.transition(job, JobStatus.RUNNING)
    assert manager.abandon(job.id).status == JobStatus.RUNNING


# ---------- C-5 事件序号跨重启续接 ----------

def test_event_seq_resumes_after_restart(tmp_path):
    log_path = tmp_path / "events.jsonl"
    bus1 = EventBus(log_path)
    for i in range(5):
        bus1.publish("job.stage", jobId=f"J{i}")
    assert bus1.latest_seq == 5

    bus2 = EventBus(log_path)  # 模拟重启：内存缓冲清空
    assert bus2.latest_seq == 0
    bus2.resume_from_log()
    assert bus2.latest_seq == 5
    ev = bus2.publish("job.stage", jobId="J-after")
    assert ev["seq"] == 6
    # 续接后旧 Last-Event-ID 不再被误判为连续
    assert bus2.replay(3)[0]["seq"] == 6


def test_event_resume_survives_garbage_tail(tmp_path):
    log_path = tmp_path / "events.jsonl"
    bus1 = EventBus(log_path)
    bus1.publish("job.stage", jobId="J1")
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("truncated-line-without-newline")
    bus2 = EventBus(log_path)
    bus2.resume_from_log()
    assert bus2.latest_seq == 1


# ---------- C-6 recovery 原子写 ----------

def test_recovery_atomic_write_leaves_no_temp(tmp_path):
    jobs_root = tmp_path / "jobs"
    (jobs_root / "J1").mkdir(parents=True)
    job = Job(id="J1", project_id="p", kind="renders", status=JobStatus.RUNNING)
    (jobs_root / "J1" / "job.json").write_text(
        json.dumps(job.model_dump(mode="json", by_alias=True)), encoding="utf-8")
    recovered = recover_on_startup(jobs_root)
    assert recovered[0].status == JobStatus.UNKNOWN
    persisted = json.loads((jobs_root / "J1" / "job.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "unknown"
    leftovers = [p.name for p in (jobs_root / "J1").iterdir() if p.name != "job.json"]
    assert leftovers == []


# ---------- U-3 render 过期传播 ----------

def _project_with_output(draft: str, output: str | None):
    from duocast.domain.project import Project
    p = Project(id="EP9", title="t")
    p.current_draft_revision = draft
    p.output_version = output
    p.selected_output_version = output
    return p


def _render_state(project):
    from duocast.services.stage_svc import derive_stage_states
    return derive_stage_states(project, [])[  "render"]


def test_render_stale_after_draft_change():
    from duocast.domain.project import StageState
    p = _project_with_output("R2", "OUT-R1-v1")
    assert _render_state(p) == StageState.STALE


def test_render_done_when_revision_matches():
    from duocast.domain.project import StageState
    p = _project_with_output("R1", "OUT-R1-v2")
    # 产物齐备但未确认：render 的 confirmed 恒 True → done
    assert _render_state(p) == StageState.DONE


def test_render_unaffected_by_legacy_or_demo_version():
    from duocast.domain.project import StageState
    p = _project_with_output("R3", "DEMO-R2-J1")  # 非 OUT- 前缀：不解析、不误报过期
    assert _render_state(p) != StageState.STALE
