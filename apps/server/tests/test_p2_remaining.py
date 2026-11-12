"""P2 余项（42 号方案）回归：机位锁定持久化校验 / 接受样片只校验视觉哈希（F4 收尾）。"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from duocast.domain.audio import AudioTimeline
from duocast.domain.job import JobStatus
from duocast.domain.project import Line, ScriptRevision, Turn
from duocast.domain.visual import VisualVariant
from duocast.main import app, settings
from duocast.services.render_inputs import dependency_hash, visual_dependency_hash
from duocast.services.voice_svc import build_units


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, text_provider='mock', image_provider='mock',
        tts_provider='mock', video_provider='mock'))
    with TestClient(app) as c:
        yield c


# ---------- 机位锁定持久化 ----------

def test_camera_locks_patch_persists(client):
    p = client.post("/api/projects", json={"title": "机位锁"}).json()
    r = client.patch(f"/api/projects/{p['id']}", json={
        "expectedRevision": p["revision"], "cameraLocks": {"A": True, "B": False}})
    assert r.status_code == 200, r.text
    assert r.json()["cameraLocks"] == {"A": True, "B": False}


@pytest.mark.parametrize("bad", [{"C": True}, {"A": "yes"}, {"A": 1}, "notdict"])
def test_camera_locks_patch_rejects_invalid(client, bad):
    p = client.post("/api/projects", json={"title": "坏锁"}).json()
    r = client.patch(f"/api/projects/{p['id']}", json={
        "expectedRevision": p["revision"], "cameraLocks": bad})
    assert r.status_code == 422


# ---------- 接受样片只校验视觉哈希 ----------

def _setup(client):
    store = client.app.state.project_store
    p = store.create("sample-hash")
    rev = ScriptRevision(id="R1", turns=[
        Turn(id="T01", speaker="A", lines=[Line(id="L1", display_text="你好", spoken_text="你好")]),
        Turn(id="T02", speaker="B", lines=[Line(id="L2", display_text="大家好", spoken_text="大家好")]),
    ])
    p = store.apply(p.id, {"script_revisions": [rev], "current_draft_revision": "R1"}, p.revision)
    units = build_units(p, rev, {}, [])
    offsets, offset = {}, 0
    for i, u in enumerate(units):
        u.adopted_audio_asset_id = f"AUD-{i}"
        u.sample_count = 48000
        offsets[u.id] = offset
        offset += 48000 + (320 * 48 if i < len(units) - 1 else 0)
    tl = AudioTimeline(revision_id="R1", sample_rate=48000, sample_count=offset,
                       units=units, unit_offsets=offsets, transition_gap_ms=[320])
    p = store.apply(p.id, {"audio_timeline": tl, "visual_variants": [VisualVariant(id="V1")]}, p.revision)
    return p


def _sample_job(client, p):
    manager = client.app.state.job_manager
    snap = {"visualDependencyHash": visual_dependency_hash(p), "dependencyHash": dependency_hash(p),
            "resolution": "854x480", "fps": 25, "fastQuality": "original", "performanceMode": "legacy"}
    job = manager.submit("sample.generate", p.id, "gpu", "smp-hash", snap)
    manager.transition(job, JobStatus.RUNNING)
    manager.transition(job, JobStatus.SUCCEEDED,
                       result={"artifactIds": [], "meta": {}, "simulated": True})
    return job


def _accept(client, p, job):
    proj = client.get(f"/api/projects/{p.id}").json()
    return client.patch(f"/api/projects/{p.id}", json={
        "expectedRevision": proj["revision"],
        "approvals": [*proj["approvals"], {"kind": "sample", "inputRevisionId": "R1",
                                           "spec": {"sampleJobId": job.id, "checks": []}}]})


def test_sample_accept_after_audio_change_still_ok(client):
    p = _setup(client)
    job = _sample_job(client, p)  # 快照视觉哈希在改配音前冻结
    store = client.app.state.project_store
    latest = store.load(p.id)
    tl = latest.audio_timeline.model_copy(deep=True)
    tl.transition_gap_ms = [500]
    tl.sample_count += 48000  # 改配音（间隔/总长）
    store.apply(p.id, {"audio_timeline": tl}, latest.revision)
    r = _accept(client, p, job)
    assert r.status_code == 200, r.text  # 画面依赖未变 → 仍可接受（F4 收尾）
    assert r.json()["stageProgress"]["visual"] == "done"


def test_sample_accept_after_visual_change_rejected(client):
    p = _setup(client)
    job = _sample_job(client, p)
    store = client.app.state.project_store
    latest = store.load(p.id)
    store.apply(p.id, {"visual_variants": [*latest.visual_variants, VisualVariant(id="V2")]}, latest.revision)
    r = _accept(client, p, job)
    assert r.status_code == 409  # 画面本身变了 → 必须重生成样片


def test_sample_accept_legacy_snapshot_falls_back_to_full_hash(client):
    """旧快照无 visualDependencyHash：回落全量哈希（改配音会拒绝，兼容旧行为）。"""
    p = _setup(client)
    manager = client.app.state.job_manager
    snap = {"dependencyHash": dependency_hash(p), "resolution": "854x480", "fps": 25,
            "fastQuality": "original", "performanceMode": "legacy"}
    job = manager.submit("sample.generate", p.id, "gpu", "smp-legacy", snap)
    manager.transition(job, JobStatus.RUNNING)
    manager.transition(job, JobStatus.SUCCEEDED, result={"artifactIds": [], "meta": {}, "simulated": True})
    store = client.app.state.project_store
    latest = store.load(p.id)
    tl = latest.audio_timeline.model_copy(deep=True)
    tl.sample_count += 48000
    store.apply(p.id, {"audio_timeline": tl}, latest.revision)
    r = _accept(client, p, job)
    assert r.status_code == 409  # 旧快照口径：全量哈希含音频，改配音即拒绝