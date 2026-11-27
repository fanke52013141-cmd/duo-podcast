"""直接上传视频：不依赖脚本、配音时间轨或本地合成的候选契约。"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from duocast.domain.audio import AudioTimeline
from duocast.main import app, settings


HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("duocast.main.settings", replace(
        settings, storage_root=tmp_path, text_provider="mock", image_provider="mock",
        tts_provider="mock", video_provider="mock"))
    with TestClient(app) as test_client:
        yield test_client


def _make_mp4(path: Path, seconds: float = 2.0) -> Path:
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=blue:s=160x96:r=25:d={seconds}",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True)
    return path


def _probe_payload(duration: float = 2.0, fps: int = 25) -> dict:
    return {
        "streams": [{
            "codec_type": "video", "width": 160, "height": 96,
            "r_frame_rate": f"{fps}/1", "duration": str(duration),
            "nb_frames": str(round(duration * fps)),
        }],
        "format": {"duration": str(duration)},
    }


def _create_project(client) -> dict:
    response = client.post("/api/projects", json={"title": "直接上传"})
    assert response.status_code == 200, response.text
    return response.json()


def _post_direct(client, project_id: str, content: bytes = b"direct-video-fixture", **meta):
    return client.post(
        f"/api/projects/{project_id}/render/import-output",
        files={"file": ("direct.mp4", content, "video/mp4")},
        data={"meta": json.dumps({"mode": "direct", **meta})},
    )


def test_timeline_import_still_requires_timeline(client):
    project = _create_project(client)
    response = client.post(
        f"/api/projects/{project['id']}/render/import-output",
        files={"file": ("timeline.mp4", b"not-probed", "video/mp4")},
    )
    assert response.status_code == 422
    assert "时间轨" in response.text


def test_direct_import_ignores_stale_timeline_and_duration(client, monkeypatch):
    project = _create_project(client)
    store = client.app.state.project_store
    loaded = store.load(project["id"])
    # 即使存在与当前草稿不匹配的一秒旧时间轨，direct 也不读取它，更不比较九秒视频时长。
    store.apply(loaded.id, {"audio_timeline": AudioTimeline(
        revision_id="R-OLD", sample_rate=48000, sample_count=48000)}, loaded.revision)

    async def probe(*_args, **_kwargs):
        return _probe_payload(duration=9.0)

    monkeypatch.setattr("duocast.api.render._probe_video", probe)
    response = _post_direct(client, project["id"])
    assert response.status_code == 200, response.text
    assert response.json()["durationSec"] == 9.0
    job = client.get(f"/api/jobs/{response.json()['jobId']}").json()
    assert job["inputSnapshot"]["revisionId"] == "DIRECT"
    assert job["inputSnapshot"]["mode"] == "direct"
    assert job["result"]["meta"]["mode"] == "direct"


def test_direct_adopt_skips_local_validation_but_rechecks_video(client, monkeypatch):
    project = _create_project(client)
    probe_calls = []

    async def probe(_path, **kwargs):
        probe_calls.append(kwargs)
        return _probe_payload()

    monkeypatch.setattr("duocast.api.render._probe_video", probe)
    imported = _post_direct(client, project["id"], clientToken="direct-contract")
    assert imported.status_code == 200, imported.text

    def forbidden_validation(*_args, **_kwargs):
        raise AssertionError("direct adoption must not run local render validation")

    monkeypatch.setattr("duocast.api.render.dependency_hash", forbidden_validation)
    monkeypatch.setattr("duocast.api.render.validate_render_options", forbidden_validation)
    adopted = client.post(
        f"/api/projects/{project['id']}/render/adopt",
        json={"jobId": imported.json()["jobId"]},
    )
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["version"] == "OUT-DIRECT-v1"
    assert len(probe_calls) == 2  # 导入一次，采用前完整性复核一次。
    assert probe_calls[-1]["video_only"] is True
    saved = client.get(f"/api/projects/{project['id']}").json()
    assert saved["currentDraftRevision"] == ""
    assert saved["outputManifest"]["version"] == "OUT-DIRECT-v1"
    assert saved["outputManifest"]["artifactIds"] == [imported.json()["artifactId"]]


def test_direct_adopt_rejects_failed_integrity_probe(client, monkeypatch):
    project = _create_project(client)

    async def valid_probe(*_args, **_kwargs):
        return _probe_payload()

    monkeypatch.setattr("duocast.api.render._probe_video", valid_probe)
    imported = _post_direct(client, project["id"])
    assert imported.status_code == 200, imported.text

    async def broken_probe(*_args, **_kwargs):
        raise ValueError("broken video")

    monkeypatch.setattr("duocast.api.render._probe_video", broken_probe)
    adopted = client.post(
        f"/api/projects/{project['id']}/render/adopt",
        json={"jobId": imported.json()["jobId"]},
    )
    assert adopted.status_code == 409
    assert "不完整" in adopted.text
    assert client.get(f"/api/projects/{project['id']}").json()["outputManifest"] is None


def test_direct_output_does_not_become_stale_when_script_is_added_later(client, monkeypatch):
    project = _create_project(client)

    async def probe(*_args, **_kwargs):
        return _probe_payload()

    monkeypatch.setattr("duocast.api.render._probe_video", probe)
    imported = _post_direct(client, project["id"])
    adopted = client.post(f"/api/projects/{project['id']}/render/adopt",
                          json={"jobId": imported.json()["jobId"]})
    assert adopted.status_code == 200, adopted.text
    store = client.app.state.project_store
    current = store.load(project["id"])
    store.apply(current.id, {
        "script_revisions": [{"id": "R1", "turns": [{
            "id": "T01", "speaker": "A",
            "lines": [{"id": "L01", "displayText": "后续脚本", "spokenText": "后续脚本"}],
        }]}],
        "current_draft_revision": "R1",
    }, current.revision)
    saved = client.get(f"/api/projects/{project['id']}").json()
    assert saved["outputVersion"] == "OUT-DIRECT-v1"
    assert saved["stageProgress"]["render"] == "done"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg unavailable")
def test_direct_real_mp4_without_script_preserves_file_and_adopts(client, tmp_path):
    project = _create_project(client)
    video = _make_mp4(tmp_path / "direct-with-audio.mp4")
    original = video.read_bytes()
    imported = _post_direct(client, project["id"], original)
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["status"] == "pending-adoption"
    assert client.get(f"/api/projects/{project['id']}").json()["outputManifest"] is None

    # direct 不做重混或转码，登记资产与上传文件逐字节一致，因此原音轨保持不变。
    artifact = client.get(f"/api/artifacts/{body['artifactId']}/file")
    assert artifact.status_code == 200
    assert artifact.content == original

    adopted = client.post(
        f"/api/projects/{project['id']}/render/adopt", json={"jobId": body["jobId"]})
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["version"] == "OUT-DIRECT-v1"
    saved = client.get(f"/api/projects/{project['id']}").json()
    assert saved["currentDraftRevision"] == ""
    assert saved["outputManifest"]["meta"]["mode"] == "direct"
    assert saved["outputManifest"]["meta"]["candidate"] is False
