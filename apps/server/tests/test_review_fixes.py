"""11 报告修复项的回归测试（第二轮审查）。全部使用临时目录，不依赖运行中的服务。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from duocast.adapters.mock import MockImageProvider, MockTTSProvider, MockTextProvider
from duocast.api.events import is_contiguous, _sse
from duocast.core.eventbus import EventBus, RESYNC_EVENT
from duocast.domain.job import Job, JobStatus
from duocast.jobs.manager import JobManager
from duocast.jobs.recovery import recover_on_startup
from duocast.main import _job_runner
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.project_store import ProjectStore


# ---------- 状态机（P1-1） ----------

def manager_fixture(tmp_path: Path) -> tuple[JobManager, EventBus]:
    bus = EventBus(tmp_path / "events.jsonl")
    return JobManager(tmp_path / "jobs", bus, {"gpu": 1, "api": 2, "cpu": 1}), bus


def test_double_cancel_is_idempotent_and_silent(tmp_path):
    """重复取消运行中任务：幂等返回，不广播矛盾的 job.failed（11 P1-1）。"""
    async def scenario():
        manager, bus = manager_fixture(tmp_path)

        async def runner(job):
            await asyncio.sleep(30)
            return {}

        manager.set_runner(runner)
        manager.start()
        job = manager.submit("kind", "p", "gpu", "t", {})
        while job.status != JobStatus.RUNNING:
            await asyncio.sleep(0.01)
        assert manager.cancel(job.id).status == JobStatus.CANCEL_REQUESTED
        assert manager.cancel(job.id).status == JobStatus.CANCEL_REQUESTED
        await manager.stop()
        assert not [e for e in bus._log_buffer if e["type"] == "job.failed"]
    asyncio.run(scenario())


def test_illegal_transition_is_rejected_silently(tmp_path):
    """非法转移返回 False 且不产生任何事件（11 P1-1 后半句）。"""
    async def scenario():
        manager, bus = manager_fixture(tmp_path)
        job = manager.submit("kind", "p", "gpu", "t", {})
        queued_events = len(bus._log_buffer)
        assert manager.transition(job, JobStatus.SUCCEEDED) is False  # queued -> succeeded 非法
        assert job.status == JobStatus.QUEUED
        assert len(bus._log_buffer) == queued_events  # 非法转移不再产生任何事件（含 job.failed）
    asyncio.run(scenario())


# ---------- 恢复（P1-5） ----------

def test_broken_manifest_becomes_unknown_placeholder(tmp_path):
    jobs_dir = tmp_path / "jobs" / "JBROKEN"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "job.json").write_text("{not-json", encoding="utf-8")
    recovered = recover_on_startup(tmp_path / "jobs")
    assert len(recovered) == 1
    assert recovered[0].id == "JBROKEN"
    assert recovered[0].status == JobStatus.UNKNOWN
    assert "manifest unreadable" in (recovered[0].error or "")


# ---------- 事件总线 / SSE（P1-3、P2-3、P2-12） ----------

def test_slow_consumer_queue_is_drained_before_resync(tmp_path):
    async def scenario():
        bus = EventBus(tmp_path / "events.jsonl", cap=10)
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        bus._subscribers["slow"] = q
        bus.publish("a")
        bus.publish("b")
        got = []
        while not q.empty():
            got.append(q.get_nowait())
        assert got == [RESYNC_EVENT], got  # 旧事件全部清空，只剩 resync
    asyncio.run(scenario())


def test_sse_resync_carries_no_id():
    assert "id" not in _sse(dict(RESYNC_EVENT))
    assert _sse({"seq": 5, "type": "x", "payload": {}})["id"] == "5"


def test_replay_gap_detection():
    assert is_contiguous([{"seq": 3}, {"seq": 4}], 2) is True
    assert is_contiguous([{"seq": 8}], 2) is False  # 缺口 → 必须 resync
    assert is_contiguous([], 2) is False


# ---------- runner 接线（P2-2、P2-4、P2-6） ----------

def test_script_generate_returns_no_fake_artifacts(tmp_path):
    async def scenario():
        store = ProjectStore(tmp_path / "projects", debounce_ms=0)
        store.create("EP-SC")
        runner = _job_runner(None, store, ArtifactStore(tmp_path / "artifacts"),
                             MockTextProvider(delay_ms=0), MockImageProvider(delay_ms=0),
                             MockTTSProvider(delay_ms=0))
        job = Job(id="J1", project_id="EP-SC", kind="script.generate",
                  input_snapshot={"sourceInput": {"kind": "topic", "content": "第一段\n第二段"}})
        result = await runner(job)
        assert result["artifactIds"] == []
        assert result["meta"]["revisionId"]
        project = store.load("EP-SC")
        assert project.script_revisions[-1].source_input["kind"] == "topic"  # P2-6 kind 透传
    asyncio.run(scenario())


def test_visual_generate_invokes_provider_and_registers_variant(tmp_path):
    class RecordingImage(MockImageProvider):
        def __init__(self):
            super().__init__(delay_ms=0)
            self.calls = []

        async def generate(self, req):
            self.calls.append(req)
            return await super().generate(req)

    async def scenario():
        store = ProjectStore(tmp_path / "projects", debounce_ms=0)
        store.create("EP-V")
        image = RecordingImage()
        runner = _job_runner(None, store, ArtifactStore(tmp_path / "artifacts"),
                             MockTextProvider(delay_ms=0), image, MockTTSProvider(delay_ms=0))
        job = Job(id="J2", project_id="EP-V", kind="visual.generate",
                  input_snapshot={"aspect": "portrait", "prompt": "p"})
        result = await runner(job)
        assert image.calls and image.calls[0]["aspect"] == "portrait"  # 提供方真实被调用
        assert result["meta"]["variantId"] == store.load("EP-V").visual_variants[-1].id
    asyncio.run(scenario())


# ---------- HTTP 层（P1-4、P1-6、P2-5） ----------

@pytest.fixture
def client(tmp_path):
    import duocast.main as main_mod

    orig = main_mod.settings
    main_mod.settings = type(orig)(
        host=orig.host, port=orig.port, storage_root=tmp_path / "storage",
        queue_cap_gpu=1, queue_cap_api=4, queue_cap_cpu=2,
        project_debounce_ms=orig.project_debounce_ms, event_log_cap=orig.event_log_cap,
    )
    try:
        with TestClient(main_mod.app) as c:
            yield c
    finally:
        main_mod.settings = orig


def test_project_id_rejects_traversal(client):
    r = client.post("/api/projects", json={"id": "../evil", "title": "t"})
    assert r.status_code == 422, r.text
    assert not (client.app.state.project_store.root.parent / "evil").exists()


def test_project_id_still_accepts_chinese_and_spaces(client):
    r = client.post("/api/projects", json={"id": "EP 中文01", "title": "中文"})
    assert r.status_code == 200, r.text


def test_duplicate_project_id_conflicts(client):
    assert client.post("/api/projects", json={"id": "EP-DUP", "title": "a"}).status_code == 200
    assert client.post("/api/projects", json={"id": "EP-DUP", "title": "b"}).status_code == 409


def test_patch_whitelist_rejects_unknown_fields(client):
    pid = client.post("/api/projects", json={"id": "EP-WL", "title": "t"}).json()["id"]
    r = client.patch(f"/api/projects/{pid}", json={"revision": 99, "expectedRevision": 0})
    assert r.status_code == 422, r.text
    r2 = client.patch(f"/api/projects/{pid}", json={"title": "x", "expectedRevision": 0})
    assert r2.status_code == 200, r2.text


def test_confirm_must_bind_current_draft(client):
    pid = client.post("/api/projects", json={"id": "EP-CF", "title": "t"}).json()["id"]
    approval = {"kind": "script", "inputRevisionId": "R-STALE", "spec": {},
                "decision": "accepted", "reasonTarget": None, "note": "", "at": ""}
    r = client.patch(f"/api/projects/{pid}", json={"approvals": [approval], "expectedRevision": 0})
    assert r.status_code == 422, r.text
