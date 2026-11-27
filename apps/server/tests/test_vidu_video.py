"""No-cost Vidu CLI protocol tests: no network, Vidu process, or paid task."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

import duocast.adapters.vidu_video as module
from duocast.adapters.vidu_video import (
    ViduCliError, ViduVideoProvider, build_vidu_args, classify_vidu_task,
    validate_vidu_base_url,
)
from duocast.storage.artifacts import ArtifactStore


def _provider(tmp_path, model="3.2"):
    store = ArtifactStore(tmp_path / "artifacts")
    provider = ViduVideoProvider(
        api_key="test-secret", base_url="https://service.vidu.cn", model=model,
        artifact_store=store,
    )
    provider.poll_interval = 0
    return provider, store


def _probe(audio=False, duration="4", frames="96"):
    streams = [{
        "codec_type": "video", "width": 1920, "height": 1080,
        "avg_frame_rate": "24/1", "duration": duration, "nb_read_frames": frames,
    }]
    if audio:
        streams.append({"codec_type": "audio", "duration": duration})
    return {"streams": streams, "format": {"duration": duration}}


def _request(**changes):
    req = {
        "requestKey": "render-request-1", "provider": "vidu", "mode": "api",
        "candidate": True, "resumableVideo": True,
        "prompt": "A quiet studio conversation", "viduModel": "3.2", "viduMode": "text",
        "viduDuration": 4, "viduResolution": "1080p", "viduRatio": "16:9",
        "imageArtifactIds": [], "projectSnapshot": {}, "rangeStartSec": 0,
        "withMasterAudio": False,
    }
    req.update(changes)
    return req


def _stub_success(provider, raw=b"local-video", probe=None):
    calls = []

    async def fake_json(args, *, timeout=120):
        calls.append((list(args), timeout))
        if args[:2] == ["task", "submit"]:
            return {"task_id": "task-original"}
        assert args == ["task", "get", "task-original", "--details"]
        return {
            "task_id": "task-original", "state": "success",
            "creations": [{"nomark_uri": "https://cdn.example.test/result.mp4"}],
        }

    async def fake_download(url):
        assert url == "https://cdn.example.test/result.mp4"
        return raw

    async def fake_probe(path):
        assert Path(path).is_file()
        return probe or _probe()

    provider._run_json = fake_json
    provider._download = fake_download
    provider._probe = fake_probe
    return calls


def test_q3_wire_modes_and_image_order(tmp_path):
    text = build_vidu_args("3.2", "text", "  hello  ", 4, "1080p", "16:9", [])
    assert text[:6] == ["task", "submit", "--type", "text2video", "--model-version", "3.2"]
    assert "--prompt=hello" in text and "--aspect-ratio" in text
    assert "--transition" not in text and not any(arg.startswith("--image=") for arg in text)
    first = build_vidu_args("3.2", "first-frame", "p", 1, "1080p", "9:16", ["first.png"])
    assert first[3] == "img2video" and "--transition" in first
    assert "--aspect-ratio" not in first and first[-1] == "--image=first.png"
    both = build_vidu_args("3.2", "first-last-frame", "p", 16, "1080p", None, ["a.png", "b.png"])
    assert both[3] == "headtailimg2video"
    assert [arg for arg in both if arg.startswith("--image=")] == ["--image=a.png", "--image=b.png"]
    reference = build_vidu_args({
        "modelId": "3.2", "mode": "reference", "prompt": "p", "duration": 5,
        "resolution": "1080p", "ratio": "1:1", "imagePaths": [str(tmp_path / "ref.png")],
    })
    assert reference[3] == "character2video" and "--transition" in reference
    assert reference[reference.index("--aspect-ratio") + 1] == "1:1"


@pytest.mark.parametrize("resolution", ["540p", "720p", "1080p", "2k", "4k"])
def test_q4_reference_resolutions_and_no_transition(resolution):
    args = build_vidu_args("3.4", "reference", "p", 2, resolution, "4:3", ["r.png"])
    assert args[3] == "character2video" and args[5] == "3.4"
    assert "--transition" not in args
    assert args[args.index("--resolution") + 1] == resolution


@pytest.mark.parametrize("duration", [0, 17, 1.5, True, "4"])
def test_duration_is_a_strict_integer(duration):
    with pytest.raises(ViduCliError, match="1–16"):
        build_vidu_args("3.2", "text", "p", duration, "1080p", "16:9", [])


@pytest.mark.parametrize("mode,count", [("text", 1), ("first-frame", 0), ("first-frame", 2),
                                          ("first-last-frame", 1), ("first-last-frame", 3),
                                          ("reference", 0), ("reference", 8)])
def test_image_cardinality_is_strict(mode, count):
    with pytest.raises(ViduCliError):
        build_vidu_args("3.2", mode, "p", 4, "1080p", "16:9", [f"{n}.png" for n in range(count)])


def test_model_resolution_ratio_and_q4_mode_validation():
    with pytest.raises(ViduCliError, match="Q4"):
        build_vidu_args("3.4", "text", "p", 4, "540p", "16:9", [])
    with pytest.raises(ViduCliError, match="清晰度"):
        build_vidu_args("3.2", "text", "p", 4, "720p", "16:9", [])
    with pytest.raises(ViduCliError, match="画幅"):
        build_vidu_args("3.2", "text", "p", 4, "1080p", "2:1", [])
    with pytest.raises(ViduCliError, match="3.2"):
        build_vidu_args("3.3", "text", "p", 4, "1080p", "16:9", [])
    with pytest.raises(ViduCliError):
        build_vidu_args("3.2", "unknown", "p", 4, "1080p", "16:9", [])


def test_task_classification_https_priority_and_terminal_states():
    data = {"state": "success", "creations": [{
        "nomark_uri": "https://cdn.example.test/clean.mp4",
        "download_uri": "https://cdn.example.test/download.mp4",
    }]}
    assert classify_vidu_task(data) == {"status": "success", "url": "https://cdn.example.test/clean.mp4"}
    fallback = classify_vidu_task({"state": "success", "creations": [{
        "nomark_uri": "", "download_uri": "https://cdn.example.test/download.mp4",
    }]})
    assert fallback["url"] == "https://cdn.example.test/download.mp4"
    assert classify_vidu_task({"state": "failed"})["status"] == "failed"
    assert classify_vidu_task({"state": "canceled"})["status"] == "canceled"
    assert classify_vidu_task({"state": "cancelled"})["status"] == "canceled"
    for state in ("created", "queueing", "queued", "processing", "running", "future-state", None):
        assert classify_vidu_task({"state": state})["status"] == "running"
    assert classify_vidu_task({"state": "success", "creations": [{"uri": "http://unsafe/video"}]})["status"] == "unknown"


def test_base_url_allowlist():
    assert validate_vidu_base_url("https://service.vidu.com/") == "https://service.vidu.com"
    for value in ("http://service.vidu.cn", "https://service.vidu.cn.evil.test", "https://evil.test", ""):
        with pytest.raises(ViduCliError):
            validate_vidu_base_url(value)


def test_check_is_local_version_only(tmp_path):
    provider, _ = _provider(tmp_path)
    calls = []

    async def fake_cli(args, *, timeout, credentials):
        calls.append((list(args), timeout, credentials))
        return 0, b"vidu-cli v0.2.33\n"

    provider._run_cli = fake_cli
    result = asyncio.run(provider.check())
    assert result["ok"] is True and result["version"] == "0.2.33"
    assert calls == [(["--version"], 20, False)]

    async def old_cli(*_args, **_kwargs):
        return 0, b"vidu-cli v0.2.32"

    provider._run_cli = old_cli
    with pytest.raises(RuntimeError, match="0.2.33"):
        asyncio.run(provider.check())


class _Stream:
    def __init__(self, *chunks):
        self.chunks = list(chunks)

    async def read(self, _size):
        return self.chunks.pop(0) if self.chunks else b""


class _Process:
    def __init__(self, output, returncode=0):
        self.stdout = _Stream(output)
        self.stderr = _Stream(b"private stderr must be discarded")
        self.returncode = returncode
        self.stopped = False

    async def wait(self):
        return self.returncode

    async def stop(self):
        self.stopped = True


def test_native_spawn_uses_environment_token_and_discards_stderr(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path)
    executable = tmp_path / "vidu-cli.exe"
    executable.write_bytes(b"native fixture")
    provider.binary_path = str(executable)
    captured = {}
    process = _Process(b'{"task_id":"safe-task"}')

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(module, "spawn_owned", fake_spawn)
    result = asyncio.run(provider._run_json(["task", "get", "safe-task", "--details"]))
    assert result["task_id"] == "safe-task"
    assert captured["args"][0] == str(executable)
    assert captured["kwargs"]["env"]["VIDU_TOKEN"] == "test-secret"
    assert captured["kwargs"]["env"]["VIDU_BASE_URL"] == "https://service.vidu.cn"
    assert "shell" not in captured["kwargs"]
    assert all("test-secret" not in arg for arg in captured["args"])
    assert process.stopped


def test_cli_errors_and_oversized_output_are_sanitized(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path)
    executable = tmp_path / "vidu-cli.exe"
    executable.write_bytes(b"native fixture")
    provider.binary_path = str(executable)

    async def error_spawn(*_args, **_kwargs):
        return _Process(json.dumps({"ok": False, "error": {
            "type": "client_error", "message": "test-secret https://signed.example.test/private",
        }}).encode(), 1)

    monkeypatch.setattr(module, "spawn_owned", error_spawn)
    with pytest.raises(ViduCliError) as caught:
        asyncio.run(provider._run_json(["task", "submit"]))
    assert "test-secret" not in str(caught.value) and "signed.example" not in str(caught.value)

    async def large_spawn(*_args, **_kwargs):
        return _Process(b"x" * (module.MAX_CLI_OUTPUT + 1))

    monkeypatch.setattr(module, "spawn_owned", large_spawn)
    with pytest.raises(ViduCliError, match="超过本地限制"):
        asyncio.run(provider._run_json(["task", "get", "id", "--details"]))


def test_render_registers_candidate_and_never_persists_secret_or_prompt(tmp_path):
    provider, store = _provider(tmp_path)
    calls = _stub_success(provider)
    result = asyncio.run(provider.render(_request()))
    artifact_id = result["artifactIds"][0]
    entry = store.by_id(artifact_id)
    assert entry["kind"] == "video" and Path(entry["path"]).read_bytes() == b"local-video"
    assert entry["fileHash"] == hashlib.sha256(b"local-video").hexdigest()
    assert result["meta"] == {
        "candidate": True, "mode": "api", "provider": "vidu", "durationSec": 4.0,
        "resolution": "1920x1080", "fps": 24, "path": str(Path(entry["path"])),
        "viduTaskId": "task-original", "viduModel": "3.2", "viduMode": "text",
        "viduResolution": "1080p", "viduRatio": "16:9", "masterAudioApplied": False,
        "qualityStatus": "needs_review",
    }
    assert calls[0][0][:2] == ["task", "submit"]
    journal = provider._journal_path.read_text(encoding="utf-8")
    assert "task-original" in journal
    assert "test-secret" not in journal and "quiet studio" not in journal


def test_retry_after_poll_failure_only_queries_original_task(tmp_path):
    provider, _ = _provider(tmp_path)
    attempts = {"submit": 0, "poll": 0}

    async def fake_json(args, *, timeout=120):
        if args[:2] == ["task", "submit"]:
            attempts["submit"] += 1
            return {"task_id": "task-original"}
        attempts["poll"] += 1
        if attempts["poll"] == 1:
            raise RuntimeError("temporary local query failure")
        return {"task_id": "task-original", "state": "success",
                "creations": [{"download_uri": "https://cdn.example.test/v.mp4"}]}

    async def download(_url):
        return b"video"

    async def probe(_path):
        return _probe()

    provider._run_json = fake_json
    provider._download = download
    provider._probe = probe
    with pytest.raises(RuntimeError, match="temporary"):
        asyncio.run(provider.render(_request()))
    result = asyncio.run(provider.render(_request()))
    assert result["meta"]["viduTaskId"] == "task-original"
    assert attempts == {"submit": 1, "poll": 2}


def test_ambiguous_submit_is_not_replayed(tmp_path):
    provider, _ = _provider(tmp_path)
    submissions = 0

    async def fake_json(_args, *, timeout=120):
        nonlocal submissions
        submissions += 1
        raise RuntimeError("connection lost after write")

    provider._run_json = fake_json
    with pytest.raises(RuntimeError, match="connection lost"):
        asyncio.run(provider.render(_request()))
    with pytest.raises(ViduCliError, match="不会自动重复提交"):
        asyncio.run(provider.render(_request()))
    assert submissions == 1


def test_same_request_key_cannot_change_paid_parameters(tmp_path):
    provider, _ = _provider(tmp_path)
    calls = _stub_success(provider)
    asyncio.run(provider.render(_request()))
    before = len(calls)
    with pytest.raises(ViduCliError, match="参数已改变"):
        asyncio.run(provider.render(_request(prompt="different prompt")))
    assert len(calls) == before


def test_image_must_be_registered_image_with_valid_hash(tmp_path):
    provider, store = _provider(tmp_path)
    image = tmp_path / "image.png"
    image.write_bytes(b"image fixture")
    entry = store.register("IMG-1", "image", str(image), "dependency")
    calls = _stub_success(provider)
    req = _request(viduMode="first-frame", imageArtifactIds=["IMG-1"])
    result = asyncio.run(provider.render(req))
    assert result["artifactIds"]
    submit = calls[0][0]
    assert f"--image={image}" in submit

    image.write_bytes(b"tampered")
    with pytest.raises(ViduCliError, match="哈希"):
        asyncio.run(provider.render(_request(requestKey="other", viduMode="first-frame", imageArtifactIds=["IMG-1"])))
    wrong = store.register("AUD-1", "audio", str(image), "dependency", file_hash=entry["fileHash"])
    assert wrong["kind"] == "audio"
    with pytest.raises(ViduCliError, match="类型"):
        asyncio.run(provider.render(_request(requestKey="third", viduMode="first-frame", imageArtifactIds=["AUD-1"])))


def test_master_audio_is_sliced_and_muxed_from_verified_asset(tmp_path):
    provider, store = _provider(tmp_path)
    master = tmp_path / "master.wav"
    master.write_bytes(b"master fixture")
    store.register("AUD-MASTER", "mixed", str(master), "dependency")
    calls = _stub_success(provider, probe=_probe(audio=True))
    mux = {}

    async def fake_mux(video, audio, target, start, duration):
        mux.update(video=video, audio=audio, start=start, duration=duration)
        assert Path(video).read_bytes() == b"local-video"
        assert Path(audio) == master
        Path(target).write_bytes(b"muxed-video")

    provider._mux = fake_mux
    snapshot = {"id": "P1", "audioTimeline": {
        "masterAudioAssetId": "AUD-MASTER", "sampleRate": 48000, "sampleCount": 480000,
    }}
    result = asyncio.run(provider.render(_request(
        projectSnapshot=snapshot, withMasterAudio=True, rangeStartSec=2.5,
    )))
    assert mux["start"] == 2.5 and mux["duration"] == 4
    assert result["meta"]["masterAudioApplied"] is True
    entry = store.by_id(result["artifactIds"][0])
    assert Path(entry["path"]).read_bytes() == b"muxed-video"
    assert entry["paramsSnapshot"]["masterAudioArtifactId"] == "AUD-MASTER"
    assert entry["paramsSnapshot"]["masterAudioHash"] == store.by_id("AUD-MASTER")["fileHash"]


def test_with_master_without_timeline_keeps_vidu_video(tmp_path):
    provider, store = _provider(tmp_path)
    _stub_success(provider)

    async def forbidden_mux(*_args):
        raise AssertionError("mux must not run without a master")

    provider._mux = forbidden_mux
    result = asyncio.run(provider.render(_request(withMasterAudio=True)))
    assert result["meta"]["masterAudioApplied"] is False
    assert Path(store.by_id(result["artifactIds"][0])["path"]).read_bytes() == b"local-video"


class _DownloadResponse:
    def __init__(self, chunks, content_length=None):
        self.status_code = 200
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}
        self.chunks = chunks

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


class _DownloadClient:
    captured = {}
    response = _DownloadResponse([b"video"])

    def __init__(self, **kwargs):
        self.captured["init"] = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, method, url, headers):
        self.captured["request"] = (method, url, headers)
        response = self.response

        class Context:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *_args):
                return None

        return Context()


def test_download_validates_public_url_without_auth_and_bounds_size(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path)
    validated = []

    def validate(url, *, resolve):
        validated.append((url, resolve))
        return url

    monkeypatch.setattr(module, "validate_public_download_url", validate)
    monkeypatch.setattr(module.httpx, "AsyncClient", _DownloadClient)
    _DownloadClient.response = _DownloadResponse([b"vid", b"eo"])
    result = asyncio.run(provider._download("https://cdn.example.test/video.mp4"))
    assert result == b"video"
    assert validated == [("https://cdn.example.test/video.mp4", True)]
    assert _DownloadClient.captured["init"]["follow_redirects"] is False
    assert _DownloadClient.captured["init"]["trust_env"] is False
    headers = _DownloadClient.captured["request"][2]
    assert "Authorization" not in headers and "VIDU_TOKEN" not in headers
    provider.max_video_bytes = 3
    with pytest.raises(ViduCliError, match="大小上限"):
        asyncio.run(provider._download("https://cdn.example.test/video.mp4"))


def test_local_probe_contract_rejects_wrong_duration_or_missing_mux_audio():
    with pytest.raises(RuntimeError, match="时长"):
        ViduVideoProvider._video_details(_probe(duration="1"), 4, False)
    with pytest.raises(RuntimeError, match="音轨"):
        ViduVideoProvider._video_details(_probe(audio=False), 4, True)


def test_master_slice_cannot_exceed_authoritative_timeline(tmp_path):
    provider, store = _provider(tmp_path)
    master = tmp_path / "short.wav"
    master.write_bytes(b"short master")
    store.register("AUD-SHORT", "mixed", str(master), "dependency")

    async def forbidden_submit(*_args, **_kwargs):
        raise AssertionError("invalid local slice must not submit a paid task")

    provider._run_json = forbidden_submit
    snapshot = {"audioTimeline": {
        "masterAudioAssetId": "AUD-SHORT", "sampleRate": 48000, "sampleCount": 96000,
    }}
    with pytest.raises(ViduCliError, match="母轨范围"):
        asyncio.run(provider.render(_request(
            projectSnapshot=snapshot, withMasterAudio=True, rangeStartSec=0,
        )))


@pytest.mark.parametrize("layout", ["nested", "parent-lib"])
def test_binary_discovery_finds_native_npm_package_paths(tmp_path, monkeypatch, layout):
    provider, _ = _provider(tmp_path)
    executable = "vidu-cli.exe" if module.os.name == "nt" else "vidu-cli"
    path_root = tmp_path / "npm-root"
    if layout == "nested":
        binary = path_root / "node_modules" / "vidu-cli" / "bin" / executable
    else:
        binary = path_root.parent / "lib" / "node_modules" / "vidu-cli" / "bin" / executable
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"native fixture")
    monkeypatch.setenv("PATH", str(path_root))
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-appdata"))
    assert provider._binary() == str(binary)
