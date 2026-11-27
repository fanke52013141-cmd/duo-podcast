"""Upload bounds and owned, whitelist-only media command regressions."""
from __future__ import annotations

import asyncio
import json
import shutil

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from duocast.api import assets, render
from duocast.storage.artifacts import ArtifactStore


class FakeUpload:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        assert size > 0, "upload readers must always request a bounded chunk"
        self.read_sizes.append(size)
        end = min(len(self.data), self.offset + size)
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk


class FakeOwnedProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, delay: float = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.delay = delay
        self.stops = 0
        self.communications = 0

    async def communicate(self):
        self.communications += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.stdout, self.stderr

    async def stop(self):
        self.stops += 1


def test_image_upload_reader_is_chunked_and_stops_at_limit():
    async def scenario():
        exact = FakeUpload(b"abcdef")
        assert await assets._read_upload_limited(exact, 6, "too large") == b"abcdef"
        assert exact.read_sizes and all(0 < size <= assets._UPLOAD_READ_CHUNK for size in exact.read_sizes)

        oversized = FakeUpload(b"abcdef")
        with pytest.raises(HTTPException) as caught:
            await assets._read_upload_limited(oversized, 5, "too large")
        assert caught.value.status_code == 422
        assert oversized.offset == 6
        assert all(0 < size <= assets._UPLOAD_READ_CHUNK for size in oversized.read_sizes)

    asyncio.run(scenario())


def test_image_endpoint_rejects_oversize_before_magic_or_write(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "_IMAGE_UPLOAD_LIMIT", 1024)
    app = FastAPI()
    app.state.artifacts = ArtifactStore(tmp_path / "artifacts")
    app.include_router(assets.router)
    with TestClient(app) as client:
        response = client.post("/api/uploads/image", files={"file": ("large.png", b"x" * 1025, "image/png")})
    assert response.status_code == 422
    assert "图片超过 10MB" in response.text
    assert not (tmp_path / "uploads" / "images").exists()


def test_audio_transcode_uses_owned_process_and_format_whitelist(tmp_path, monkeypatch):
    source = tmp_path / "input.wav"
    source.write_bytes(b"fixture")
    process = FakeOwnedProcess()
    calls = []

    async def fake_spawn(executable, *args, **kwargs):
        calls.append((executable, args, kwargs))
        return process

    monkeypatch.setattr(assets, "spawn_owned", fake_spawn)
    result = asyncio.run(assets._transcode_audio(source, tmp_path / "output.wav", timeout=1, max_duration=30))

    assert result == (0, b"", b"")
    assert process.stops == 1
    executable, args, kwargs = calls[0]
    assert executable == "ffmpeg"
    assert "ffmpeg" not in args
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.PIPE
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    formats = args[args.index("-format_whitelist") + 1].split(",")
    assert {"wav", "mp3", "flac", "ogg", "mp4"} <= set(formats)
    assert not {"hls", "dash", "concat", "ssegment", "rtsp", "http"} & set(formats)
    assert args.index("-protocol_whitelist") < args.index("-i")
    assert args[args.index("-f") + 1] == "wav"


def test_ffprobe_uses_owned_process_and_video_format_whitelist(tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"fixture")
    process = FakeOwnedProcess(stdout=json.dumps({"streams": [], "format": {}}).encode())
    calls = []

    async def fake_spawn(executable, *args, **kwargs):
        calls.append((executable, args, kwargs))
        return process

    monkeypatch.setattr(assets, "spawn_owned", fake_spawn)
    result = asyncio.run(render._probe_video(source, timeout=1, video_only=True))

    assert result == {"streams": [], "format": {}}
    assert process.stops == 1
    executable, args, _ = calls[0]
    assert executable == "ffprobe"
    assert "ffprobe" not in args
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    formats = args[args.index("-format_whitelist") + 1].split(",")
    assert {"mp4", "mov", "matroska", "webm", "avi"} <= set(formats)
    assert not {"hls", "dash", "concat", "ssegment", "rtsp", "http"} & set(formats)
    assert "v:0" in args


def test_remux_restricts_each_input_independently(tmp_path, monkeypatch):
    video = tmp_path / "input.mp4"
    audio = tmp_path / "master.wav"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    process = FakeOwnedProcess()
    calls = []

    async def fake_spawn(executable, *args, **kwargs):
        calls.append((executable, args, kwargs))
        return process

    monkeypatch.setattr(assets, "spawn_owned", fake_spawn)
    asyncio.run(render._remux_video(video, audio, tmp_path / "out.mp4", 1.25, timeout=1))

    assert process.stops == 1
    _, args, _ = calls[0]
    input_indexes = [index for index, value in enumerate(args) if value == "-i"]
    assert len(input_indexes) == 2
    assert args.count("-protocol_whitelist") == 2
    assert args.count("-format_whitelist") == 2
    for index in input_indexes:
        assert args.index("-protocol_whitelist", 0, index) < index
        assert args.index("-format_whitelist", 0, index) < index
    assert args[args.index("-f") + 1] == "mp4"


def test_media_timeout_still_stops_owned_process(monkeypatch):
    process = FakeOwnedProcess(delay=1)

    async def fake_spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(assets, "spawn_owned", fake_spawn)
    with pytest.raises(TimeoutError):
        asyncio.run(assets._run_media_command("ffmpeg", [], timeout=0.01))
    assert process.stops == 1


@pytest.mark.parametrize("value", [
    "http://127.0.0.1/media.wav",
    "https://example.invalid/media.mp4",
    r"\\server\share\media.wav",
    "//server/share/media.wav",
    "relative/media.wav",
])
def test_uri_unc_and_relative_media_paths_never_spawn(value, tmp_path, monkeypatch):
    spawned = False

    async def fake_spawn(*args, **kwargs):
        nonlocal spawned
        spawned = True
        return FakeOwnedProcess()

    monkeypatch.setattr(assets, "spawn_owned", fake_spawn)
    with pytest.raises(ValueError):
        asyncio.run(assets._transcode_audio(value, tmp_path / "out.wav", timeout=1))
    assert spawned is False


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg unavailable")
def test_reference_audio_endpoint_rejects_playlist_without_registering(tmp_path):
    playlist = tmp_path / "playlist.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:1,\nhttp://127.0.0.1:9/never.ts\n", encoding="utf-8")
    app = FastAPI()
    app.state.artifacts = ArtifactStore(tmp_path / "artifacts")
    app.include_router(assets.router)
    with TestClient(app) as client:
        response = client.post("/api/uploads/audio", files={"file": ("playlist.m3u8", playlist.read_bytes(), "audio/x-mpegurl")})
    assert response.status_code == 422
    assert app.state.artifacts._manifest()["artifacts"] == []
    assert not list((tmp_path / "artifacts" / "audio").glob("*.wav"))


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe unavailable")
def test_ffprobe_rejects_playlist_demuxer(tmp_path):
    playlist = tmp_path / "playlist.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:1,\nhttp://127.0.0.1:9/never.ts\n", encoding="utf-8")
    with pytest.raises(ValueError, match="无法读取上传视频"):
        asyncio.run(render._probe_video(playlist, timeout=5))
