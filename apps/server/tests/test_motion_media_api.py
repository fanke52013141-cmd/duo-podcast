import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from duocast.api import motion_media
from duocast.storage.artifacts import ArtifactStore


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.state.artifacts = ArtifactStore(tmp_path / "artifacts")
    app.include_router(motion_media.router)
    with TestClient(app) as client:
        yield client, app.state.artifacts


def test_invalid_upload_does_not_register_or_leave_media(client):
    client, store = client
    assert client.post("/api/motion-media", content=b"not a video").status_code == 422
    assert client.post("/api/motion-media", content=b"").status_code == 422
    assert store._manifest()["artifacts"] == []
    assert list((store.root / "motion-imports").iterdir()) == []


def test_upload_bound_and_extension_validation(client, monkeypatch):
    client, store = client
    monkeypatch.setattr(motion_media, "MAX_UPLOAD_BYTES", 4)
    assert client.post("/api/motion-media", content=b"12345").status_code == 413
    assert client.post("/api/motion-media?extension=../evil", content=b"1").status_code == 422
    assert store._manifest()["artifacts"] == []


def test_success_imports_actual_video_without_adopting_or_exposing_path(client, tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg required")
    client, store = client
    video = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=96x54:r=25:d=1", "-c:v", "mpeg4", str(video)],
                   check=True, timeout=20, capture_output=True)
    response = client.post("/api/motion-media", content=video.read_bytes())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["native"]["frames"] == 25
    assert data["reviewStatus"] == "unreviewed"
    assert "path" not in data and "checks" not in data
    entry = store.by_id(data["artifactId"])
    assert entry["fileHash"] == data["fileHash"]
    assert entry["kind"] == "video"


def test_partial_analysis_is_not_import_success(client, monkeypatch):
    client, store = client
    monkeypatch.setattr(motion_media, "inspect_motion_clip", lambda *a, **k: {
        "native": {"frames": 10, "durationSec": 1}, "hardFailures": [],
        "issues": [{"code": "ANALYSIS_TIMEOUT"}], "evidence": {}})
    assert client.post("/api/motion-media", content=b"video").status_code == 422
    assert store._manifest()["artifacts"] == []


def test_cancel_waits_for_analyzer_before_removing_source(tmp_path, monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace

    store = ArtifactStore(tmp_path / "artifacts")
    started, finish = threading.Event(), threading.Event()

    def analyze(path, **kwargs):
        started.set()
        assert finish.wait(5)
        assert path.exists()
        return {}

    class FakeRequest:
        app = SimpleNamespace(state=SimpleNamespace(artifacts=store))

        async def stream(self):
            yield b"source"

    monkeypatch.setattr(motion_media, "inspect_motion_clip", analyze)

    async def scenario():
        task = asyncio.create_task(motion_media.upload_motion_media(FakeRequest(), "mp4"))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        assert len(list((store.root / "motion-imports").iterdir())) == 1
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert list((store.root / "motion-imports").iterdir()) == []
    assert store._manifest()["artifacts"] == []


def test_image_import_returns_full_digest_and_cleans_invalid(client, tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg required")
    import hashlib
    client, store = client
    image = tmp_path / "source.png"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=96x54", "-frames:v", "1", str(image)],
                   check=True, timeout=20, capture_output=True)
    data = image.read_bytes()
    response = client.post("/api/motion-media/images", content=data)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["fileHash"] == hashlib.sha256(data).hexdigest()
    assert "path" not in result
    assert store.by_id(result["artifactId"])["kind"] == "image"
    bad = client.post("/api/motion-media/images", content=b"\x89PNG\r\n\x1a\ninvalid")
    assert bad.status_code == 422
    assert len(store._manifest()["artifacts"]) == 1
    assert len(list((store.root.parent / "uploads" / "images").iterdir())) == 1
