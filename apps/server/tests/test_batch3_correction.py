"""批 3（42 号方案）纠偏解锁回归测试：P1-E 主动撤销确认 / P1-D 时间轨回退 / P1-B 自定义试听。"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from duocast.domain.audio import AudioTimeline
from duocast.domain.project import Line, ScriptRevision, Turn
from duocast.main import app, settings
from duocast.services.voice_svc import build_units

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, text_provider='mock', image_provider='mock',
        tts_provider='mock', video_provider='mock'))
    with TestClient(app) as c:
        yield c


def _revision(rid="R1") -> ScriptRevision:
    return ScriptRevision(id=rid, turns=[
        Turn(id="T01", speaker="A", lines=[Line(id="L001", display_text="你好", spoken_text="你好")]),
        Turn(id="T02", speaker="B", lines=[Line(id="L002", display_text="大家好", spoken_text="大家好")]),
    ])


def _make_wav(path: Path, seconds: float = 0.5) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000", "-t", str(seconds),
                    "-ac", "1", "-c:a", "pcm_s16le", str(path)], check=True)
    return path


# ---------- P1-E：主动撤销确认 ----------

def test_revoke_script_approval_marks_revoked_not_deleted(client):
    p = client.post("/api/projects", json={"title": "撤销"}).json()
    client.post(f"/api/projects/{p['id']}/script/import",
                json={"turns": [{"speaker": "A", "lines": [{"displayText": "x"}]}]})
    proj = client.get(f"/api/projects/{p['id']}").json()
    rev = proj["currentDraftRevision"]
    # 确认脚本
    r = client.patch(f"/api/projects/{p['id']}", json={
        "expectedRevision": proj["revision"],
        "approvals": [*proj["approvals"], {"kind": "script", "inputRevisionId": rev}]})
    assert r.status_code == 200, r.text
    assert r.json()["stageProgress"]["script"] == "done"
    # 主动撤销
    r = client.post(f"/api/projects/{p['id']}/approvals/script/revoke",
                    json={"reason": "想再改改", "expectedRevision": r.json()["revision"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stageProgress"]["script"] != "done"
    # 审计保留：记录仍在，带 revokedAt/revokedReason
    record = next(a for a in body["approvals"] if a["kind"] == "script")
    assert record["revokedAt"]
    assert record["revokedReason"] == "想再改改"
    # 再撤销 → 409（无有效确认）
    assert client.post(f"/api/projects/{p['id']}/approvals/script/revoke", json={}).status_code == 409


def test_revoke_rejects_unknown_kind_and_missing_project(client):
    p = client.post("/api/projects", json={"title": "坏撤销"}).json()
    assert client.post(f"/api/projects/{p['id']}/approvals/nope/revoke", json={}).status_code == 422
    assert client.post("/api/projects/NOPE/approvals/script/revoke", json={}).status_code == 404


# ---------- P1-D：时间轨回退 ----------

def _setup_with_history(client, tmp_path):
    import wave
    store = client.app.state.project_store
    assets = client.app.state.artifacts
    p = store.create(f"restore-{tmp_path.name[:8]}")
    p = store.apply(p.id, {"script_revisions": [_revision()], "current_draft_revision": "R1"}, p.revision)
    units = build_units(p, p.script_revisions[0], {}, [])
    offsets, offset = {}, 0
    for i, unit in enumerate(units):
        wav = _make_wav(tmp_path / f"u{i}.wav")
        with wave.open(str(wav)) as reader:
            count = reader.getnframes()
        entry = assets.register(f"AUD-R-{i}-{tmp_path.name[:6]}", "audio", str(wav),
                                assets._hash_file(str(wav)), {"source": "fixture"})
        unit.adopted_audio_asset_id = entry["artifactId"]
        unit.sample_count = count
        offsets[unit.id] = offset
        offset += count + (320 * 48 if i < len(units) - 1 else 0)
    current = AudioTimeline(revision_id="R1", sample_rate=48000, sample_count=offset,
                            units=units, unit_offsets=offsets, transition_gap_ms=[320])
    master = _make_wav(tmp_path / "master.wav", offset / 48000)
    mentry = assets.register(f"AUD-M-R-{tmp_path.name[:6]}", "mixed", str(master),
                             assets._hash_file(str(master)), {"source": "fixture"})
    current.master_audio_asset_id = mentry["artifactId"]
    old = current.model_copy(deep=True)
    old.sample_count = offset // 2  # 历史版本与当前不同，便于断言
    p = store.apply(p.id, {"audio_timeline": current, "audio_history": [old]}, p.revision)
    return p, old, current


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg unavailable")
def test_restore_timeline_rolls_back_and_revokes_voice(client, tmp_path):
    p, old, current = _setup_with_history(client, tmp_path)
    # 先确认配音
    proj = client.get(f"/api/projects/{p.id}").json()
    r = client.patch(f"/api/projects/{p.id}", json={
        "expectedRevision": proj["revision"],
        "approvals": [*proj["approvals"], {"kind": "voice", "inputRevisionId": "R1"}]})
    assert r.status_code == 200, r.text
    # 回退到历史 #0
    r = client.post(f"/api/projects/{p.id}/voice/restore-timeline", json={"historyIndex": 0})
    assert r.status_code == 200, r.text
    timeline = r.json()["audioTimeline"]
    assert timeline["sampleCount"] == old.sample_count
    proj = client.get(f"/api/projects/{p.id}").json()
    assert proj["audioTimeline"]["sampleCount"] == old.sample_count
    assert proj["stageProgress"]["voice"] != "done"  # 确认被撤销
    assert any(a["kind"] == "voice" and a["revokedAt"] for a in proj["approvals"])


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg unavailable")
def test_restore_rejects_cross_revision_and_bad_index(client, tmp_path):
    p, old, current = _setup_with_history(client, tmp_path)
    assert client.post(f"/api/projects/{p.id}/voice/restore-timeline", json={"historyIndex": 99}).status_code == 422
    store = client.app.state.project_store
    latest = store.load(p.id)
    foreign = old.model_copy(deep=True)
    foreign.revision_id = "R9"
    store.apply(p.id, {"audio_history": [foreign]}, latest.revision)
    r = client.post(f"/api/projects/{p.id}/voice/restore-timeline", json={"historyIndex": 0})
    assert r.status_code == 422
    assert "回退脚本" in r.text


# ---------- P1-B：自定义试听文本 ----------

def test_audition_accepts_custom_text(client):
    p = client.post("/api/projects", json={"title": "试听"}).json()
    r = client.post(f"/api/projects/{p['id']}/voice/audition",
                    json={"speaker": "A", "text": "用真实台词验音色"})
    assert r.status_code == 200
    job = client.get(f"/api/jobs/{r.json()['jobId']}").json()
    assert job["inputSnapshot"]["text"] == "用真实台词验音色"