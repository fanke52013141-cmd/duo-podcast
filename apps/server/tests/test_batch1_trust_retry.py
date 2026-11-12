"""批 1（42 号方案）信任与重试回归测试：P0-A / P0-B / P0-C。

覆盖：
- P0-A：script.generate 结果带 applied 标记；生成期间输入被改动 → applied=False 且草稿不切换
- P0-B：manager.retry 继承快照与 completedCalls 账本；外部付费任务护栏；
        retryable 按错误类别分类；TTS 中断重启带账本续跑（不再一律 UNKNOWN）
- P0-C：/plan/render-segments 以依赖哈希 + 片段文件完整性给出真实复用计划
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from duocast.adapters.mock import MockImageProvider, MockTextProvider, MockTTSProvider, MockVideoProvider
from duocast.core.eventbus import EventBus
from duocast.domain.audio import AudioTimeline, SynthesisUnit
from duocast.domain.job import Job, JobStatus
from duocast.domain.project import Line, ScriptRevision, Turn
from duocast.jobs.manager import JobManager, classify_retryable
from duocast.jobs.recovery import recover_on_startup
from duocast.main import _job_runner, app, settings
from duocast.services.render_inputs import dependency_hash
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.project_store import ProjectStore


def manager_fixture(tmp_path: Path) -> JobManager:
    bus = EventBus(tmp_path / "events.jsonl")
    return JobManager(tmp_path / "jobs", bus, {"gpu": 1, "api": 2, "cpu": 1})


def _revision(rid: str = "R1") -> ScriptRevision:
    return ScriptRevision(id=rid, turns=[
        Turn(id=f"T{i}", speaker=s, lines=[Line(id=f"L{i}", display_text="你好", spoken_text="你好")])
        for i, s in enumerate(("A", "B"), 1)
    ])


# ---------- P0-A：脚本生成 applied 语义 ----------

def test_script_generate_reports_applied_true(tmp_path):
    async def scenario():
        store = ProjectStore(tmp_path / "projects", debounce_ms=0)
        p = store.create("pa")
        artifacts = ArtifactStore(tmp_path / "artifacts")
        manager = manager_fixture(tmp_path)
        runner = _job_runner(manager, store, artifacts, MockTextProvider(delay_ms=0),
                             MockImageProvider(), MockTTSProvider(), MockVideoProvider())
        job = manager.submit("script.generate", p.id, "api", "tok-a", {
            "sourceInput": {"kind": "topic", "content": "测试话题"},
            "brief": {}, "expectedProjectRevision": p.revision,
        })
        result = await runner(job)
        assert result["meta"]["applied"] is True
        saved = store.load(p.id)
        assert saved.current_draft_revision == result["meta"]["revisionId"]
    asyncio.run(scenario())


def test_script_generate_reports_applied_false_when_input_changed(tmp_path):
    """生成期间用户改动输入（工程 revision 变化）→ 新版本只追加不切换，applied=False。"""
    async def scenario():
        store = ProjectStore(tmp_path / "projects", debounce_ms=0)
        p = store.create("pb")
        artifacts = ArtifactStore(tmp_path / "artifacts")
        manager = manager_fixture(tmp_path)

        class EditingProvider(MockTextProvider):
            async def generate(self, req):
                # 模拟生成期间用户编辑：工程 revision 前进
                latest = store.load(p.id)
                store.apply(p.id, {"title": "生成期间被改动"}, latest.revision)
                return await super().generate(req)

        runner = _job_runner(manager, store, artifacts, EditingProvider(delay_ms=0),
                             MockImageProvider(), MockTTSProvider(), MockVideoProvider())
        job = manager.submit("script.generate", p.id, "api", "tok-b", {
            "sourceInput": {"kind": "topic", "content": "测试话题"},
            "brief": {}, "expectedProjectRevision": p.revision,
        })
        result = await runner(job)
        assert result["meta"]["applied"] is False
        saved = store.load(p.id)
        # 新版本已登记（可一键采用），但当前草稿未被静默替换
        assert any(r.id == result["meta"]["revisionId"] for r in saved.script_revisions)
        assert saved.current_draft_revision != result["meta"]["revisionId"]
    asyncio.run(scenario())


# ---------- P0-B：原地重试 ----------

def test_retry_inherits_snapshot_and_completed_calls(tmp_path):
    manager = manager_fixture(tmp_path)
    job = manager.submit("tts.synthesize", "p", "gpu", "t1", {"revisionId": "R1"})
    manager.transition(job, JobStatus.RUNNING)
    job.completed_calls = {"unit-key": {"audioAssetId": "AUD-1", "sampleCount": 48000}}
    manager.transition(job, JobStatus.FAILED, error="connect timeout")
    new = manager.retry(job.id)
    assert new.id != job.id
    assert new.status == JobStatus.QUEUED
    assert new.input_snapshot == {"revisionId": "R1"}
    assert new.completed_calls == {"unit-key": {"audioAssetId": "AUD-1", "sampleCount": 48000}}
    assert new.retried_from == job.id
    assert new.attempt == job.attempt + 1
    assert new.client_token == "t1-retry-1"
    # 原终态任务保留（审计链）
    assert manager.get(job.id).status == JobStatus.FAILED


def test_retry_token_increments_and_is_idempotent_per_attempt(tmp_path):
    manager = manager_fixture(tmp_path)
    job = manager.submit("tts.synthesize", "p", "gpu", "t1", {})
    manager.transition(job, JobStatus.RUNNING)
    manager.transition(job, JobStatus.FAILED)
    first = manager.retry(job.id)
    manager.transition(first, JobStatus.RUNNING)
    manager.transition(first, JobStatus.FAILED)
    second = manager.retry(first.id)
    assert second.client_token == "t1-retry-1-retry-1"
    assert second.retried_from == first.id


def test_retry_rejects_non_terminal(tmp_path):
    manager = manager_fixture(tmp_path)
    job = manager.submit("tts.synthesize", "p", "gpu", "t1", {})
    with pytest.raises(ValueError):
        manager.retry(job.id)  # queued
    manager.transition(job, JobStatus.RUNNING)
    with pytest.raises(ValueError):
        manager.retry(job.id)  # running
    manager.transition(job, JobStatus.SUCCEEDED)
    with pytest.raises(ValueError):
        manager.retry(job.id)  # succeeded


def test_retry_motion_prepare_guarded(tmp_path):
    manager = manager_fixture(tmp_path)
    job = manager.submit("motion.prepare", "", "gpu", "t1", {}, owner_type="motionPack", owner_id="pack-1")
    manager.transition(job, JobStatus.RUNNING)
    manager.transition(job, JobStatus.FAILED)
    with pytest.raises(ValueError, match="继续查询原外部任务"):
        manager.retry(job.id)


def test_retry_unknown_render_requires_inspection_first(tmp_path):
    manager = manager_fixture(tmp_path)
    job = manager.submit("renders", "p", "gpu", "t1", {})
    manager.transition(job, JobStatus.RUNNING)
    manager.transition(job, JobStatus.UNKNOWN)
    with pytest.raises(ValueError, match="核实"):
        manager.retry(job.id)
    # FAILED 渲染可重试（段级磁盘缓存会复用）
    manager.transition(job, JobStatus.FAILED)
    assert manager.retry(job.id).status == JobStatus.QUEUED


def test_retry_unknown_tts_allowed_with_ledger(tmp_path):
    manager = manager_fixture(tmp_path)
    job = manager.submit("tts.synthesize", "p", "gpu", "t1", {})
    manager.transition(job, JobStatus.RUNNING)
    job.completed_calls = {"k": {"sampleCount": 1}}
    manager.transition(job, JobStatus.UNKNOWN)
    new = manager.retry(job.id)
    assert new.status == JobStatus.QUEUED
    assert new.completed_calls == {"k": {"sampleCount": 1}}


def test_classify_retryable_by_error_category():
    assert classify_retryable("HTTP connect timeout") is True
    assert classify_retryable("连接被重置 connection reset by peer") is True
    assert classify_retryable("I2V_ENGINE_FAILED: 引擎失败") is True
    assert classify_retryable("503 Service Unavailable") is True
    assert classify_retryable("INVALID_GAPS: 间隔数量不符") is False
    assert classify_retryable("EMPTY_SCRIPT") is False
    assert classify_retryable("I2V_SUBMISSION_UNKNOWN") is False


# ---------- P0-B：TTS 中断恢复 ----------

def _write_job(jobs_root: Path, job: Job) -> None:
    (jobs_root / job.id).mkdir(parents=True, exist_ok=True)
    (jobs_root / job.id / "job.json").write_text(
        json.dumps(job.model_dump(mode="json", by_alias=True)), encoding="utf-8")


def test_recovery_requeues_interrupted_tts_with_ledger(tmp_path):
    jobs_root = tmp_path / "jobs"
    job = Job(id="JT1", project_id="p", kind="tts.synthesize", status=JobStatus.RUNNING,
              completed_calls={"unit-1": {"audioAssetId": "AUD-1"}})
    _write_job(jobs_root, job)
    recovered = recover_on_startup(jobs_root)
    assert recovered[0].status == JobStatus.QUEUED
    assert recovered[0].completed_calls == {"unit-1": {"audioAssetId": "AUD-1"}}
    persisted = json.loads((jobs_root / "JT1" / "job.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "queued"
    assert persisted["completedCalls"] == {"unit-1": {"audioAssetId": "AUD-1"}}


def test_recovery_respects_tts_pause_and_cancel_intent(tmp_path):
    jobs_root = tmp_path / "jobs"
    paused = Job(id="JT2", project_id="p", kind="tts.synthesize", status=JobStatus.PAUSE_REQUESTED)
    cancelled = Job(id="JT3", project_id="p", kind="tts.synthesize", status=JobStatus.CANCEL_REQUESTED)
    _write_job(jobs_root, paused)
    _write_job(jobs_root, cancelled)
    by_id = {j.id: j for j in recover_on_startup(jobs_root)}
    assert by_id["JT2"].status == JobStatus.PAUSED
    assert by_id["JT3"].status == JobStatus.CANCELLED


def test_recovery_other_kinds_still_unknown(tmp_path):
    """非 TTS/视频任务保持原有 UNKNOWN 纪律，不扩大自动重跑面。"""
    jobs_root = tmp_path / "jobs"
    job = Job(id="JX", project_id="p", kind="script.rewrite", status=JobStatus.RUNNING)
    _write_job(jobs_root, job)
    assert recover_on_startup(jobs_root)[0].status == JobStatus.UNKNOWN


# ---------- P0-B / P0-C：API 层 ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, text_provider='mock', image_provider='mock',
        tts_provider='mock', video_provider='mock'))
    with TestClient(app) as c:
        yield c


def test_retry_api_returns_new_job_and_guards(client):
    manager = client.app.state.job_manager
    job = manager.submit("tts.synthesize", "p-api", "gpu", "api-t1", {"revisionId": "R1"})
    manager.transition(job, JobStatus.RUNNING)
    manager.transition(job, JobStatus.FAILED, error="connect timeout")
    res = client.post(f"/api/jobs/{job.id}/retry")
    assert res.status_code == 200
    body = res.json()
    assert body["retriedFrom"] == job.id
    assert body["status"] == "queued"
    # 护栏：motion.prepare 409
    motion = manager.submit("motion.prepare", "", "gpu", "api-t2", {}, owner_type="motionPack", owner_id="pack-x")
    manager.transition(motion, JobStatus.RUNNING)
    manager.transition(motion, JobStatus.FAILED)
    assert client.post(f"/api/jobs/{motion.id}/retry").status_code == 409
    # 不存在的任务 404
    assert client.post("/api/jobs/JNOPE/retry").status_code == 404


def _setup_render_baseline(client, tmp_path):
    store = client.app.state.project_store
    p = store.create("seg-plan")
    p = store.apply(p.id, {"script_revisions": [_revision()], "current_draft_revision": "R1"}, p.revision)
    timeline = AudioTimeline(revision_id="R1", sample_rate=48000, sample_count=480000,
                             units=[SynthesisUnit(id="U-T01", turn_id="T01", sample_count=480000)],
                             unit_offsets={"U-T01": 0})
    p = store.apply(p.id, {"audio_timeline": timeline}, p.revision)
    motion_dir = tmp_path / "artifacts" / "video_motion" / "baseline"
    motion_dir.mkdir(parents=True)
    rate = 48000
    for start, stop in ((0, 5), (5, 10)):
        key = round(start * rate) * 100000000 + round(stop * rate)
        clip = motion_dir / f"shot_{key}.mp4"
        clip.write_bytes(f"clip-{start}".encode())
        clip.with_suffix(".complete.json").write_text(json.dumps({
            "referenceHash": "ref", "fileHash": hashlib.sha256(clip.read_bytes()).hexdigest(),
        }), encoding="utf-8")
    manager = client.app.state.job_manager
    job = manager.submit("renders", p.id, "gpu", "seg-tok", {
        "dependencyHash": dependency_hash(p), "revisionId": "R1",
        "resolution": "854x480", "fps": 25,
    })
    manager.transition(job, JobStatus.RUNNING)
    manager.transition(job, JobStatus.SUCCEEDED, result={
        "artifactIds": ["VID-baseline"], "meta": {"motionDirectory": str(motion_dir)},
    })
    return p, motion_dir


def test_render_segment_plan_real_reuse(client, tmp_path):
    p, motion_dir = _setup_render_baseline(client, tmp_path)
    plan = client.get(f"/api/projects/{p.id}/plan/render-segments").json()
    assert plan["available"] is True
    assert plan["totalCount"] == 2
    assert plan["reusableCount"] == 2
    assert all(s["reusable"] for s in plan["segments"])


def test_render_segment_plan_detects_corrupt_clip(client, tmp_path):
    p, motion_dir = _setup_render_baseline(client, tmp_path)
    rate = 48000
    first = motion_dir / f"shot_{round(0*rate)*100000000 + round(5*rate)}.mp4"
    first.write_bytes(b"corrupted")
    plan = client.get(f"/api/projects/{p.id}/plan/render-segments").json()
    assert plan["reusableCount"] == 1
    assert plan["segments"][0]["reusable"] is False
    assert plan["segments"][1]["reusable"] is True


def test_render_segment_plan_input_change_invalidates(client, tmp_path):
    p, _ = _setup_render_baseline(client, tmp_path)
    store = client.app.state.project_store
    latest = store.load(p.id)
    store.apply(p.id, {"camera_mode": "twoShot"}, latest.revision)
    plan = client.get(f"/api/projects/{p.id}/plan/render-segments").json()
    assert plan["inputsMatch"] is False
    assert plan["reusableCount"] == 0


def test_render_segment_plan_no_baseline(client):
    store = client.app.state.project_store
    p = store.create("seg-empty")
    plan = client.get(f"/api/projects/{p.id}/plan/render-segments").json()
    assert plan["available"] is False
    assert plan["reusableCount"] == 0