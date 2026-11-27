"""Vidu 路由/装配/候选采用集成测试；使用本地假提供方，不访问真实服务。"""
from __future__ import annotations

import shutil
import subprocess
import time
import wave
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from duocast.main import app, settings
from duocast.domain.audio import AudioTimeline
from duocast.domain.project import Line, ScriptRevision, Turn
from duocast.services.voice_svc import build_units

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
CAPTURED = []


class FakeViduProvider:
    def __init__(self, *, artifact_store, **_kwargs):
        self.artifacts = artifact_store
        self.capabilities = {"simulated": False, "provider": "vidu"}

    async def check(self):
        return {"ok": True, "provider": "vidu", "version": "0.2.33"}

    async def render(self, req, progress=None):
        CAPTURED.append(req)
        root = Path(self.artifacts.root) / "video" / "vidu-fixture"
        root.mkdir(parents=True, exist_ok=True)
        output = root / f"{req['requestKey']}.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            "color=c=blue:s=160x96:r=25:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-frames:v", "25", str(output),
        ], check=True)
        artifact_id = "VIDU-FIXTURE-001"
        self.artifacts.register(artifact_id, "video", str(output), req["dependencyHash"], req, "vidu-fixture")
        return {"artifactIds": [artifact_id], "meta": {
            "candidate": True, "mode": "api", "provider": "vidu", "durationSec": 1.0,
            "resolution": "160x96", "fps": 25, "path": str(output), "viduTaskId": "remote-fixture",
        }}


@pytest.fixture
def client(tmp_path, monkeypatch):
    CAPTURED.clear()
    monkeypatch.setattr("duocast.main.settings", replace(
        settings, storage_root=tmp_path, video_provider="vidu", vidu_key="dummy", vidu_model="3.2",
        text_provider="mock", image_provider="mock", tts_provider="mock"))
    monkeypatch.setattr("duocast.main.ViduVideoProvider", FakeViduProvider)
    with TestClient(app) as test_client:
        yield test_client


def _wait(client, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            assert job["status"] == "succeeded", job
            return job
        time.sleep(0.02)
    raise AssertionError("job timed out")


def _submit(client, project_id, **overrides):
    payload = {
        "provider": "vidu", "clientToken": "vidu-route-fixture", "prompt": "A calm studio scene",
        "viduModel": "3.2", "viduMode": "text", "viduDuration": 1,
        "viduResolution": "1080p", "viduRatio": "16:9", "imageArtifactIds": [],
        "candidate": True,
    }
    payload.update(overrides)
    return client.post(f"/api/projects/{project_id}/render/generate", json=payload)


def test_vidu_validation_requires_active_provider_and_valid_parameters(client):
    project = client.post("/api/projects", json={"title": "Vidu 参数"}).json()
    assert _submit(client, project["id"], prompt="").status_code == 422
    response = _submit(client, project["id"], viduModel="3.4", viduMode="first-frame", imageArtifactIds=[])
    assert response.status_code == 422
    assert "Q4" in response.text
    response = _submit(client, project["id"], viduDuration=17)
    assert response.status_code == 422
    response = _submit(client, project["id"], viduMode="reference", imageArtifactIds=[])
    assert response.status_code == 422
    response = _submit(client, project["id"], withMasterAudio=True)
    assert response.status_code == 422
    assert "母轨" in response.text
    assert CAPTURED == []


def test_vidu_cli_check_is_local_and_does_not_submit(client):
    response = client.post("/api/providers/video/test", json={})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "0.2.33" in response.json()["message"]
    assert CAPTURED == []


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg unavailable")
def test_vidu_generates_candidate_without_script_or_timeline_and_adopts(client):
    project = client.post("/api/projects", json={"title": "Vidu 独立候选"}).json()
    accepted = _submit(client, project["id"])
    assert accepted.status_code == 200, accepted.text
    job = _wait(client, accepted.json()["jobId"])
    assert job["queueClass"] == "api"
    assert job["result"]["simulated"] is False
    assert job["result"]["meta"]["candidate"] is True
    assert job["result"]["meta"]["mode"] == "api"
    assert CAPTURED[0]["prompt"] == "A calm studio scene"
    assert CAPTURED[0]["viduDuration"] == 1
    assert "vidu_key" not in str(CAPTURED[0]).lower()
    assert "dummy" not in str(CAPTURED[0])

    adopted = client.post(f"/api/projects/{project['id']}/render/adopt",
                          json={"jobId": job["id"]})
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["version"] == "OUT-VIDU-v1"
    saved = client.get(f"/api/projects/{project['id']}").json()
    assert saved["stageProgress"]["render"] == "done"
    assert saved["audioTimeline"] is None

    # 独立 API 成片保留冻结输入，不被后续文案编辑误标为本地过期产物。
    latest = client.get(f"/api/projects/{project['id']}").json()
    patched = client.patch(f"/api/projects/{project['id']}", json={
        "expectedRevision": latest["revision"],
        "scriptRevisions": [{"id": "R1", "turns": [{
            "id": "T01", "speaker": "A", "lines": [{"id": "L01", "displayText": "后来脚本", "spokenText": "后来脚本"}],
        }]}],
        "currentDraftRevision": "R1",
    })
    assert patched.status_code == 200, patched.text
    assert patched.json()["stageProgress"]["render"] == "done"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg unavailable")
def test_vidu_visual_sample_uses_api_fields_and_queue(client, tmp_path):
    store = client.app.state.project_store
    project = store.create("VIDU-SAMPLE")
    revision = ScriptRevision(id="R1", turns=[Turn(
        id="T01", speaker="A", lines=[Line(id="L01", display_text="样片", spoken_text="样片")],
    )])
    project = store.apply(project.id, {"script_revisions": [revision], "current_draft_revision": "R1"}, project.revision)
    units = build_units(project, revision, {}, [])
    wav_path = tmp_path / "master.wav"
    with wave.open(str(wav_path), "wb") as output:
        output.setparams((1, 2, 48000, 48000, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 48000)
    assets = client.app.state.artifacts
    unit_asset = assets.register("AUD-SAMPLE-UNIT", "audio", str(wav_path), "fixture")
    master_asset = assets.register("AUD-SAMPLE-MASTER", "mixed", str(wav_path), "fixture")
    units[0].adopted_audio_asset_id = unit_asset["artifactId"]
    units[0].sample_count = 48000
    timeline = AudioTimeline(
        revision_id="R1", sample_rate=48000, sample_count=48000, units=units,
        unit_offsets={units[0].id: 0}, master_audio_asset_id=master_asset["artifactId"],
    )
    image_path = tmp_path / "visual.png"
    image_path.write_bytes(b"local image fixture")
    image = assets.register("IMG-SAMPLE", "image", str(image_path), "fixture")
    project = store.apply(project.id, {
        "audio_timeline": timeline,
        "visual_variants": [{"id": "V1", "masterImage": {
            "artifactId": image["artifactId"], "path": str(image_path), "fileHash": image["fileHash"],
        }, "aspect": "landscape"}],
        "selected_visual_variant_id": "V1",
    }, project.revision)
    accepted = client.post(f"/api/projects/{project.id}/visual/sample", json={
        "revisionId": "R1", "rangeStartSec": 0, "rangeEndSec": 1,
        "clientToken": "vidu-sample-fixture",
    })
    assert accepted.status_code == 200, accepted.text
    job = _wait(client, accepted.json()["jobId"])
    assert job["queueClass"] == "api"
    request = CAPTURED[-1]
    assert request["provider"] == "vidu"
    assert request["viduMode"] == "first-frame"
    assert request["imageArtifactIds"] == ["IMG-SAMPLE"]
    assert request["withMasterAudio"] is True
    assert request["viduDuration"] == 1
