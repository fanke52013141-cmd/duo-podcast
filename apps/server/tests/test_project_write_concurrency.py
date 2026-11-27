"""Project writes, confirmation appends and output versions stay consistent."""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from duocast.api.projects import append_approval
from duocast.domain.audio import AudioTimeline, SynthesisUnit
from duocast.domain.project import Project
from duocast.services.render_svc import apply_output_version
from duocast.storage.project_store import ProjectRevisionConflict, ProjectStore


def test_concurrent_apply_same_revision_has_one_winner(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "projects", debounce_ms=60000)
    store.create("P")
    original = Project.model_dump

    def slow_dump(self, *args, **kwargs):
        time.sleep(0.02)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Project, "model_dump", slow_dump)
    barrier = threading.Barrier(2)

    def write(title):
        barrier.wait()
        try:
            store.apply("P", {"title": title}, 0)
            return "ok"
        except ProjectRevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["A", "B"]))
    assert sorted(results) == ["conflict", "ok"]
    assert store.load("P").revision == 1


def test_atomic_approval_endpoint_appends_without_client_snapshot(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    store.create("P")
    store.apply("P", {"current_draft_revision": "R1"}, 0)
    state = SimpleNamespace(
        project_store=store,
        job_manager=SimpleNamespace(list=lambda _id: []),
        machine=SimpleNamespace(get_global_pronunciation=lambda: []),
        capabilities=None,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    first = asyncio.run(append_approval("P", {
        "kind": "script", "inputRevisionId": "R1", "note": "first",
    }, request))
    # 本用例验证原子追加；声音确认必须有完整时间轨是独立的前置契约。
    current = store.load("P")
    store.apply("P", {"audio_timeline": AudioTimeline(
        revision_id="R1", sample_count=48000, unit_offsets={"U-T01": 0},
        units=[SynthesisUnit(id="U-T01", turn_id="T01", sample_count=48000)],
    )}, current.revision)
    second = asyncio.run(append_approval("P", {
        "kind": "voice", "inputRevisionId": "R1", "note": "second",
    }, request))
    assert [item["note"] for item in second["approvals"]] == ["first", "second"]
    assert current.revision + 2 == second["revision"]  # 先补时间轨，再原子追加确认


def test_output_version_allocator_never_reuses_historical_id(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    store.create("P")
    apply_output_version(store, store.load("P"), "R1", {"artifactIds": ["A1"], "meta": {}})
    apply_output_version(store, store.load("P"), "R1", {"artifactIds": ["A2"], "meta": {}})
    project = store.load("P")
    old = project.output_history[0]
    store.apply("P", {"output_version": old["version"], "selected_output_version": old["version"],
                      "output_manifest": old}, project.revision)
    version = apply_output_version(store, store.load("P"), "R1", {"artifactIds": ["A3"], "meta": {}})
    assert version == "OUT-R1-v3"
    versions = [item["version"] for item in store.load("P").output_history]
    assert len(versions) == len(set(versions))
