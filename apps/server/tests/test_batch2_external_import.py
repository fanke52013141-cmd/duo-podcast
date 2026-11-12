"""批 2（42 号方案）外部上传通道回归测试。

覆盖：
- 脚本 JSON 导入：结构校验 / 版本语义 / clientToken 幂等 / 乐观锁 / Ingest 信封
- 音频单元导入：转码 48kHz PCM16 单声道 → 注入候选 → 现有 adopt 采纳（不自动采用）
- 图片带区域导入：magic 校验 / 区域校验 / 变体登记
- 整片视频导入：ffprobe 严格校验 / 时长一致性 / audioMode=master 重混 / 候选采用
- Ingest 门禁：DUOCAST_API_TOKEN 设置后必须 Bearer 匹配；schema 自描述
"""
from __future__ import annotations

import base64
import json
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

# 1x1 透明 PNG（magic 校验夹具）
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, text_provider='mock', image_provider='mock',
        tts_provider='mock', video_provider='mock'))
    with TestClient(app) as c:
        yield c


def _make_wav(path: Path, seconds: float = 1.0) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate=48000", "-t", str(seconds),
                    "-ac", "1", "-c:a", "pcm_s16le", str(path)], check=True)
    return path


def _make_mp4(path: Path, seconds: float = 1.0, size: str = "160x96") -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=blue:s={size}:r=25:d={seconds}", "-f", "lavfi", "-i",
                    f"anullsrc=r=48000:cl=mono", "-t", str(seconds),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(path)], check=True)
    return path


def _revision() -> ScriptRevision:
    return ScriptRevision(id="R1", turns=[
        Turn(id="T01", speaker="A", lines=[Line(id="L001", display_text="你好", spoken_text="你好")]),
        Turn(id="T02", speaker="B", lines=[Line(id="L002", display_text="大家好", spoken_text="大家好")]),
    ])


def _setup_project_with_timeline(client, tmp_path, seconds: float = 1.0, with_master: bool = False):
    """建工程 + 脚本 + 真实 WAV 单元构成的权威时间轨（指纹与 build_units 一致）。"""
    import wave
    store = client.app.state.project_store
    assets = client.app.state.artifacts
    p = store.create(f"imp-{tmp_path.name[:8]}")
    p = store.apply(p.id, {"script_revisions": [_revision()], "current_draft_revision": "R1"}, p.revision)
    revision = p.script_revisions[0]
    units = build_units(p, revision, {}, [])
    offsets = {}
    offset = 0
    for i, unit in enumerate(units):
        wav = _make_wav(tmp_path / f"unit-{i}.wav", seconds)
        with wave.open(str(wav)) as reader:
            count = reader.getnframes()
        entry = assets.register(f"AUD-FIX-{i}-{tmp_path.name[:6]}", "audio", str(wav),
                                assets._hash_file(str(wav)), {"source": "fixture"})
        unit.adopted_audio_asset_id = entry["artifactId"]
        unit.sample_count = count
        offsets[unit.id] = offset
        offset += count + (320 * 48 if i < len(units) - 1 else 0)
    timeline = AudioTimeline(revision_id="R1", sample_rate=48000, sample_count=offset,
                             units=units, unit_offsets=offsets,
                             transition_gap_ms=[320] * (len(units) - 1))
    if with_master:
        master = _make_wav(tmp_path / "master.wav", offset / 48000)
        entry = assets.register(f"AUD-M-FIX-{tmp_path.name[:6]}", "mixed", str(master),
                                assets._hash_file(str(master)), {"source": "fixture"})
        timeline.master_audio_asset_id = entry["artifactId"]
    p = store.apply(p.id, {"audio_timeline": timeline}, p.revision)
    return p


# ---------- 脚本 JSON 导入 ----------

def test_script_import_creates_new_draft(client):
    p = client.post("/api/projects", json={"title": "导入"}).json()
    payload = {"turns": [
        {"speaker": "A", "lines": [{"displayText": "开场白"}]},
        {"speaker": "B", "intent": "probe", "speedRatio": 1.1,
         "lines": [{"displayText": "追问", "spokenText": "追问读音"}]},
    ], "label": "外部稿"}
    res = client.post(f"/api/projects/{p['id']}/script/import", json=payload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["turnCount"] == 2
    proj = client.get(f"/api/projects/{p['id']}").json()
    assert proj["currentDraftRevision"] == body["revisionId"]
    rev = next(r for r in proj["scriptRevisions"] if r["id"] == body["revisionId"])
    assert rev["turns"][0]["id"] == "T01"
    assert rev["turns"][0]["lines"][0]["spokenText"] == "开场白"  # 缺省互为兜底
    assert rev["turns"][1]["speedRatio"] == 1.1
    assert rev["textApiConfigRef"] == "script-import-json"
    assert body["estimatedSeconds"]["max"] > 0


def test_script_import_envelope_and_idempotency(client):
    p = client.post("/api/projects", json={"title": "信封"}).json()
    envelope = {"schemaVersion": "1.0", "kind": "script", "clientToken": "tok-import-1",
                "projectId": p["id"], "provenance": {"source": "external-upload", "tool": "外部工具"},
                "payload": {"turns": [{"speaker": "A", "lines": [{"displayText": "你好"}]}]}}
    first = client.post(f"/api/projects/{p['id']}/script/import", json=envelope)
    assert first.status_code == 200, first.text
    again = client.post(f"/api/projects/{p['id']}/script/import", json=envelope)
    assert again.status_code == 200
    assert again.json()["revisionId"] == first.json()["revisionId"]
    assert again.json()["idempotent"] is True
    proj = client.get(f"/api/projects/{p['id']}").json()
    assert len(proj["scriptRevisions"]) == 1  # 幂等不重复建版本


@pytest.mark.parametrize("bad", [
    {"turns": []},
    {"turns": [{"speaker": "C", "lines": [{"displayText": "x"}]}]},
    {"turns": [{"speaker": "A", "speedRatio": 2.0, "lines": [{"displayText": "x"}]}]},
    {"turns": [{"speaker": "A", "intent": "unknown", "lines": [{"displayText": "x"}]}]},
    {"turns": [{"speaker": "A", "lines": []}]},
    {"turns": [{"id": "T01", "speaker": "A", "lines": [{"displayText": "x"}]},
               {"id": "T01", "speaker": "B", "lines": [{"displayText": "y"}]}]},
    {"turns": [{"speaker": "A", "lines": [{"displayText": "   "}]}]},
])
def test_script_import_rejects_bad_schema(client, bad):
    p = client.post("/api/projects", json={"title": "坏结构"}).json()
    res = client.post(f"/api/projects/{p['id']}/script/import", json=bad)
    assert res.status_code == 422
    assert "UPLOAD_SCHEMA" in res.text


def test_script_import_revision_conflict(client):
    p = client.post("/api/projects", json={"title": "冲突"}).json()
    res = client.post(f"/api/projects/{p['id']}/script/import",
                      json={"expectedRevision": p["revision"] + 5,
                            "turns": [{"speaker": "A", "lines": [{"displayText": "x"}]}]})
    assert res.status_code == 409


# ---------- 音频单元导入 ----------

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg unavailable")
def test_audio_import_injects_candidate_then_adopt(client, tmp_path):
    p = _setup_project_with_timeline(client, tmp_path)
    upload = _make_wav(tmp_path / "upload.wav", 1.5)
    res = client.post(f"/api/projects/{p.id}/voice/units/U-T01/import-audio",
                      files={"file": ("upload.wav", upload.read_bytes(), "audio/wav")},
                      data={"meta": json.dumps({"provenance": {"source": "external-upload", "tool": "真人录音"}})})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "pending-adoption"
    assert body["sampleCount"] == 72000  # 1.5s * 48000，实测为准
    proj = client.get(f"/api/projects/{p.id}").json()
    unit = next(u for u in proj["audioTimeline"]["units"] if u["id"] == "U-T01")
    assert body["candidateAudioAssetId"] in unit["candidateAudioAssetIds"]
    # 未采用：母轨与已采用音频不变
    assert unit["adoptedAudioAssetId"] != body["candidateAudioAssetId"]
    # 走现有 adopt 采纳 → 重建母轨
    adopt = client.post(f"/api/projects/{p.id}/voice/units/U-T01/adopt",
                        json={"candidateAudioAssetId": body["candidateAudioAssetId"]})
    assert adopt.status_code == 200, adopt.text
    timeline = adopt.json()["audioTimeline"]
    adopted = next(u for u in timeline["units"] if u["id"] == "U-T01")
    assert adopted["adoptedAudioAssetId"] == body["candidateAudioAssetId"]
    assert timeline["masterAudioAssetId"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg unavailable")
def test_audio_import_rejects_garbage_and_missing_timeline(client, tmp_path):
    p = _setup_project_with_timeline(client, tmp_path)
    res = client.post(f"/api/projects/{p.id}/voice/units/U-T01/import-audio",
                      files={"file": ("bad.wav", b"not-audio", "audio/wav")})
    assert res.status_code == 422
    res = client.post(f"/api/projects/{p.id}/voice/units/U-NOPE/import-audio",
                      files={"file": ("ok.wav", _make_wav(tmp_path / "ok.wav").read_bytes(), "audio/wav")})
    assert res.status_code == 422
    # 无时间轨的工程
    empty = client.post("/api/projects", json={"title": "无配音"}).json()
    res = client.post(f"/api/projects/{empty['id']}/voice/units/U-T01/import-audio",
                      files={"file": ("ok.wav", _make_wav(tmp_path / "ok2.wav").read_bytes(), "audio/wav")})
    assert res.status_code == 422
    assert "先合成" in res.text


# ---------- 图片带区域导入 ----------

def test_image_import_with_regions(client):
    p = client.post("/api/projects", json={"title": "图片导入"}).json()
    regions = {"A": {"x": 0.05, "y": 0.1, "width": 0.4, "height": 0.8},
               "B": {"x": 0.55, "y": 0.1, "width": 0.4, "height": 0.8}}
    res = client.post(f"/api/projects/{p['id']}/visual/import-variant",
                      files={"file": ("scene.png", PNG_1PX, "image/png")},
                      data={"meta": json.dumps({"mode": "twoShot", "personRegions": regions})})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["regionsApplied"] is True
    proj = client.get(f"/api/projects/{p['id']}").json()
    variant = next(v for v in proj["visualVariants"] if v["id"] == body["variantId"])
    assert variant["personRegions"] == regions
    assert variant["imageApiConfigRef"] == "upload"


def test_image_import_rejects_bad_magic_and_regions(client):
    p = client.post("/api/projects", json={"title": "坏图片"}).json()
    res = client.post(f"/api/projects/{p['id']}/visual/import-variant",
                      files={"file": ("fake.png", b"not-an-image", "image/png")})
    assert res.status_code == 422
    res = client.post(f"/api/projects/{p['id']}/visual/import-variant",
                      files={"file": ("scene.png", PNG_1PX, "image/png")},
                      data={"meta": json.dumps({"personRegions": {"A": {"x": 0.9, "y": 0, "width": 0.5, "height": 1}}})})
    assert res.status_code == 422


# ---------- 整片视频导入 ----------

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg unavailable")
def test_video_import_candidate_and_adopt(client, tmp_path):
    p = _setup_project_with_timeline(client, tmp_path, seconds=0.5)
    total_sec = p.audio_timeline.sample_count / 48000
    video = _make_mp4(tmp_path / "external.mp4", seconds=total_sec)
    res = client.post(f"/api/projects/{p.id}/render/import-output",
                      files={"file": ("external.mp4", video.read_bytes(), "video/mp4")},
                      data={"meta": json.dumps({"provenance": {"source": "external-upload", "tool": "剪映"}})})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "pending-adoption"
    # 候选记录：未采用前正式输出指针不变
    proj = client.get(f"/api/projects/{p.id}").json()
    assert proj["outputManifest"] is None
    job = client.get(f"/api/jobs/{body['jobId']}").json()
    assert job["status"] == "succeeded"
    assert job["result"]["meta"]["candidate"] is True
    assert job["result"]["meta"]["imported"] is True
    # 走现有 adopt 采用
    adopt = client.post(f"/api/projects/{p.id}/render/adopt", json={"jobId": body["jobId"]})
    assert adopt.status_code == 200, adopt.text
    proj = client.get(f"/api/projects/{p.id}").json()
    assert proj["outputManifest"] is not None
    assert body["artifactId"] in proj["outputManifest"]["artifactIds"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg unavailable")
def test_video_import_duration_mismatch_rejected(client, tmp_path):
    p = _setup_project_with_timeline(client, tmp_path, seconds=0.5)
    video = _make_mp4(tmp_path / "toolong.mp4", seconds=3.0)
    res = client.post(f"/api/projects/{p.id}/render/import-output",
                      files={"file": ("toolong.mp4", video.read_bytes(), "video/mp4")})
    assert res.status_code == 422
    assert "不符" in res.text


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg unavailable")
def test_video_import_master_remux(client, tmp_path):
    p = _setup_project_with_timeline(client, tmp_path, seconds=0.5, with_master=True)
    total_sec = p.audio_timeline.sample_count / 48000
    video = _make_mp4(tmp_path / "external.mp4", seconds=total_sec)
    res = client.post(f"/api/projects/{p.id}/render/import-output",
                      files={"file": ("external.mp4", video.read_bytes(), "video/mp4")},
                      data={"meta": json.dumps({"audioMode": "master"})})
    assert res.status_code == 200, res.text
    body = res.json()
    artifact = client.get(f"/api/artifacts/{body['artifactId']}/file")
    assert artifact.status_code == 200
    assert len(artifact.content) > 0


# ---------- Ingest 统一入口与门禁 ----------

def test_ingest_schema_self_describing(client):
    res = client.get("/api/ingest/schema")
    assert res.status_code == 200
    assert set(res.json()["kinds"]) == {"script", "audio", "image", "video"}
    for kind in ("script", "audio", "image", "video"):
        detail = client.get(f"/api/ingest/schema?kind={kind}")
        assert detail.status_code == 200
        assert detail.json()["payload"]
    assert client.get("/api/ingest/schema?kind=nope").status_code == 422


def test_ingest_script_route(client):
    p = client.post("/api/projects", json={"title": "ingest"}).json()
    envelope = {"schemaVersion": "1.0", "kind": "script", "projectId": p["id"],
                "payload": {"turns": [{"speaker": "B", "lines": [{"displayText": "来自外部工具"}]}]}}
    res = client.post("/api/ingest/script", json=envelope)
    assert res.status_code == 200, res.text
    assert res.json()["turnCount"] == 1
    # kind 不符 → 422
    bad = {**envelope, "kind": "audio"}
    assert client.post("/api/ingest/script", json=bad).status_code == 422


def test_ingest_token_gate(client, monkeypatch):
    monkeypatch.setattr(client.app.state, "runtime_settings",
                        replace(client.app.state.runtime_settings, api_token="secret-token"))
    p = client.post("/api/projects", json={"title": "门禁"}).json()
    envelope = {"schemaVersion": "1.0", "kind": "script", "projectId": p["id"],
                "payload": {"turns": [{"speaker": "A", "lines": [{"displayText": "x"}]}]}}
    assert client.post("/api/ingest/script", json=envelope).status_code == 401
    assert client.post("/api/ingest/script", json=envelope,
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/ingest/script", json=envelope,
                       headers={"Authorization": "Bearer secret-token"}).status_code == 200