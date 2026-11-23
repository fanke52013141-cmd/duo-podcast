"""Idempotency and storage-failure guards for the job lifecycle."""
import asyncio

import pytest

from duocast.core.eventbus import EventBus
from duocast.domain.job import JobStatus
from duocast.jobs.manager import JobInputConflict, JobManager, JobPersistenceError


def _manager(tmp_path):
    return JobManager(tmp_path / "jobs", EventBus(tmp_path / "events.jsonl"), {"gpu": 1, "api": 1, "cpu": 1})


def test_submit_reuses_only_identical_input(tmp_path):
    manager = _manager(tmp_path)
    first = manager.submit("tts.synthesize", "p", "gpu", "token", {"revisionId": "R1"})
    assert manager.submit("tts.synthesize", "p", "gpu", "token", {"revisionId": "R1"}).id == first.id
    with pytest.raises(JobInputConflict):
        manager.submit("tts.synthesize", "p", "gpu", "token", {"revisionId": "R2"})


def test_submit_persist_failure_leaves_no_idempotency_ghost(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    original = manager._persist

    def fail(_job):
        raise JobPersistenceError("disk full")

    monkeypatch.setattr(manager, "_persist", fail)
    with pytest.raises(JobPersistenceError):
        manager.submit("test", "p", "cpu", "token", {"value": 1})
    assert manager.find_by_token("token", "p", "test") is None
    monkeypatch.setattr(manager, "_persist", original)
    job = manager.submit("test", "p", "cpu", "token", {"value": 1})
    assert manager.find_by_token("token", "p", "test").id == job.id


def test_transition_persist_failure_keeps_previous_state(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    job = manager.submit("test", "p", "cpu", "token", {})
    monkeypatch.setattr(manager, "_persist", lambda _job: (_ for _ in ()).throw(JobPersistenceError("disk full")))
    with pytest.raises(JobPersistenceError):
        manager.transition(job, JobStatus.RUNNING)
    assert job.status == JobStatus.QUEUED


def test_retry_is_idempotent_per_intent(tmp_path):
    manager = _manager(tmp_path)
    job = manager.submit("tts.synthesize", "p", "gpu", "t1", {"revisionId": "R1"})
    manager.transition(job, JobStatus.RUNNING)
    manager.transition(job, JobStatus.FAILED)
    first = manager.retry(job.id, "retry-intent")
    assert manager.retry(job.id, "retry-intent").id == first.id


def test_queued_cancel_removes_queue_entry(tmp_path):
    manager = _manager(tmp_path)
    job = manager.submit("test", "p", "cpu", "token", {})
    assert len(manager._queues["cpu"]) == 1
    manager.cancel(job.id)
    assert len(manager._queues["cpu"]) == 0
    assert job.status == JobStatus.CANCELLED


def test_worker_survives_one_persistence_failure(tmp_path, monkeypatch):
    manager = _manager(tmp_path)

    async def runner(_job):
        return {"artifactIds": []}

    manager.set_runner(runner)
    job = manager.submit("test", "p", "api", "token", {})
    original = manager._persist
    failed = False

    def flaky(candidate):
        nonlocal failed
        if candidate.status == JobStatus.RUNNING and not failed:
            failed = True
            raise JobPersistenceError("temporary")
        original(candidate)

    monkeypatch.setattr(manager, "_persist", flaky)

    async def scenario():
        manager.start()
        try:
            for _ in range(100):
                if failed and job.status == JobStatus.SUCCEEDED:
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("worker did not recover")
        finally:
            await manager.stop()

    asyncio.run(scenario())
    assert job.status == JobStatus.SUCCEEDED
    assert failed is True


def test_cancel_requested_failure_is_recorded_as_cancelled(tmp_path):
    manager = _manager(tmp_path)

    async def runner(job):
        manager.cancel(job.id)
        raise RuntimeError("connect timeout after cancel")

    manager.set_runner(runner)
    job = manager.submit("test", "p", "api", "token", {})

    async def scenario():
        manager.start()
        try:
            for _ in range(100):
                if job.status == JobStatus.CANCELLED:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("job was not cancelled")
        finally:
            await manager.stop()

    asyncio.run(scenario())
    assert job.status == JobStatus.CANCELLED
    assert job.cancel_reason == "connect timeout after cancel"
