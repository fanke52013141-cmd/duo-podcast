"""无费用的 MiniMax / 火山协议测试；所有上游响应均为本地夹具。"""
from __future__ import annotations

import asyncio
import base64
import io
import tarfile

import pytest

from duocast.adapters.cloud_tts import (
    MINIMAX_DEFAULT_VOICE, MiniMaxTTSProvider, VolcengineTTSProvider,
    audio_from_tar, build_minimax_body, build_volcengine_body,
)
from duocast.services.runtime_config import preflight_config, validate_config, validate_provider_url


def test_minimax_wire_body_uses_async_sample_rate_and_voice_fallback():
    body = build_minimax_body({"lineTexts": [" 你好。"], "speedRatio": 1.2}, "speech-2.8-hd")
    assert body["text"] == "你好。"
    assert body["voice_setting"]["voice_id"] == MINIMAX_DEFAULT_VOICE
    assert body["voice_setting"]["speed"] == 1.2
    assert body["audio_setting"] == {
        "audio_sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1,
    }
    assert "sample_rate" not in body["audio_setting"]
    custom = build_minimax_body({"lineTexts": ["你好"], "voiceId": " Chinese (Mandarin)_News_Anchor "},
                                "speech-2.8-turbo")
    assert custom["voice_setting"]["voice_id"] == "Chinese (Mandarin)_News_Anchor"


def test_volc_wire_body_and_validation():
    body = build_volcengine_body(
        {"lineTexts": ["第一句", "第二句"], "speedRatio": 1.25},
        "seed-audio-1.0", "speaker-a",
    )
    assert body["text_prompt"] == "第一句第二句"
    assert body["speaker"] == "speaker-a"
    assert body["audio_config"]["format"] == "wav"
    assert body["audio_config"]["sample_rate"] == 48000
    assert body["audio_config"]["speech_rate"] == 25
    with pytest.raises(ValueError, match="speaker"):
        build_volcengine_body({"lineTexts": ["你好"]}, "seed-audio-1.0")
    with pytest.raises(ValueError, match="仅支持"):
        build_volcengine_body({"lineTexts": ["你好"], "voiceId": "s"}, "other-model")


def _tar_with_audio(payload: bytes = b"fixture-mp3") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo("content-audio.mp3")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
        extra = tarfile.TarInfo("content.titles")
        extra.size = 3
        archive.addfile(extra, io.BytesIO(b"txt"))
    return output.getvalue()


def test_minimax_tar_extracts_audio_without_writing_members():
    assert audio_from_tar(_tar_with_audio()) == b"fixture-mp3"
    plain = b"not-a-tar"
    assert audio_from_tar(plain) is plain
    with pytest.raises(ValueError, match="没有音频"):
        text_only = io.BytesIO()
        with tarfile.open(fileobj=text_only, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            info = tarfile.TarInfo("content.titles")
            info.size = 3
            archive.addfile(info, io.BytesIO(b"txt"))
        audio_from_tar(text_only.getvalue())


def test_minimax_async_polls_original_task_before_retrieve(tmp_path):
    provider = MiniMaxTTSProvider(
        api_key="dummy", base_url="https://api.minimaxi.com", model="speech-2.8-hd", voices={},
        artifacts_root=tmp_path, artifact_store=None,
    )
    provider.poll_interval = 0
    calls = []
    saved = {}

    async def fake_json(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/v1/t2a_async_v2":
            return {"task_id": 1001, "file_id": "future", "base_resp": {"status_code": 0}}
        if path == "/v1/query/t2a_async_query_v2":
            assert kwargs["params"] == {"task_id": "1001"}
            return {"status": "Success", "file_id": "ready-file", "base_resp": {"status_code": 0}}
        assert path == "/v1/files/retrieve"
        assert kwargs["params"] == {"file_id": "ready-file"}
        return {"file": {"download_url": "https://cdn.example.com/audio.tar"}}

    async def fake_download(url):
        assert url == "https://cdn.example.com/audio.tar"
        return _tar_with_audio()

    async def fake_save(raw, suffix, req, **kwargs):
        saved.update(raw=raw, suffix=suffix, kwargs=kwargs)
        return {"sampleRate": 48000, "sampleCount": 480, "audioAssetId": "AUD-LOCAL"}

    provider._json = fake_json
    provider._download = fake_download
    provider._save_audio = fake_save
    result = asyncio.run(provider.synthesize({"lineTexts": ["你好"], "speaker": "A"}))
    assert result["audioAssetId"] == "AUD-LOCAL"
    assert saved["raw"] == b"fixture-mp3"
    assert saved["kwargs"]["provider_task_id"] == "1001"
    assert [path for _, path, _ in calls] == [
        "/v1/t2a_async_v2", "/v1/query/t2a_async_query_v2", "/v1/files/retrieve",
    ]


def test_volc_base64_response_and_http_business_error(tmp_path):
    provider = VolcengineTTSProvider(
        api_key="dummy", base_url="https://openspeech.bytedance.com", model="seed-audio-1.0",
        voices={"A": "speaker-a"}, artifacts_root=tmp_path, artifact_store=None,
    )
    captured = {}

    async def fake_json(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return {"code": 0, "audio": base64.b64encode(b"wav-fixture").decode()}

    async def fake_save(raw, suffix, req, **kwargs):
        captured.update(raw=raw, suffix=suffix, voice=kwargs["voice_id"])
        return {"sampleRate": 48000, "sampleCount": 480}

    provider._json = fake_json
    provider._save_audio = fake_save
    asyncio.run(provider.synthesize({"lineTexts": ["你好"], "speaker": "A"}))
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v3/tts/create"
    assert captured["timeout"] == 120
    assert captured["raw"] == b"wav-fixture"
    assert captured["voice"] == "speaker-a"

    async def invalid_json(*args, **kwargs):
        return {"code": 0, "audio": "not base64!"}

    provider._json = invalid_json
    with pytest.raises(ValueError, match="Base64"):
        asyncio.run(provider.synthesize({"lineTexts": ["你好"], "speaker": "A"}))


def test_minimax_retry_after_query_failure_never_creates_a_second_task(tmp_path):
    provider = MiniMaxTTSProvider(
        api_key="dummy", base_url="https://api.minimaxi.com", model="speech-2.8-hd", voices={},
        artifacts_root=tmp_path, artifact_store=None,
    )
    provider.poll_interval = 0
    create_calls = 0
    query_calls = 0

    async def fake_json(method, path, **kwargs):
        nonlocal create_calls, query_calls
        if path == "/v1/t2a_async_v2":
            create_calls += 1
            return {"task_id": "original-task", "base_resp": {"status_code": 0}}
        if path == "/v1/query/t2a_async_query_v2":
            query_calls += 1
            if query_calls == 1:
                raise RuntimeError("temporary query failure")
            return {"status": "Success", "file_id": "file-ready", "base_resp": {"status_code": 0}}
        return {"file": {"download_url": "https://cdn.example.com/result.mp3"}}

    async def fake_download(_url):
        return b"audio"

    async def fake_save(*_args, **_kwargs):
        return {"sampleRate": 48000, "sampleCount": 480}

    provider._json = fake_json
    provider._download = fake_download
    provider._save_audio = fake_save
    request = {"lineTexts": ["你好"], "speaker": "A", "requestKey": "job-token", "unitId": "U-T01"}
    with pytest.raises(RuntimeError, match="temporary"):
        asyncio.run(provider.synthesize(request))
    asyncio.run(provider.synthesize(request))
    assert create_calls == 1
    assert query_calls == 2


def test_cloud_runtime_config_is_validated_without_network():
    validate_config({"tts_provider": "minimax", "minimax_base_url": "https://api.minimaxi.com",
                     "minimax_voice_b": ""})
    validate_config({"tts_provider": "volcengine", "volcengine_base_url": "https://openspeech.bytedance.com",
                     "volcengine_voice_a": "", "volcengine_voice_b": ""})
    with pytest.raises(ValueError):
        validate_provider_url("https://api.minimaxi.com", "volcengine_base_url")
    with pytest.raises(ValueError):
        validate_provider_url("https://openspeech.bytedance.com", "minimax_base_url")
    result = preflight_config({"tts_provider": "volcengine", "volcengine_model": "seed-audio-1.0",
                               "volcengine_base_url": "https://openspeech.bytedance.com"})
    assert result["valid"] is True
    assert any("DUOCAST_VOLCENGINE_API_KEY" in warning for warning in result["warnings"])
