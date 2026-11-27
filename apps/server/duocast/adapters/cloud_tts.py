"""MiniMax 与火山引擎语音合成适配器。

协议参考用户指定的 free-creation 当前实现（src/main/gateway/audio.ts）：
- MiniMax：POST /v1/t2a_async_v2 → 查询任务 → 检索文件 → 下载并解开 tar。
- 火山引擎：POST /api/v3/tts/create，X-Api-Key 鉴权，响应 audio 为 Base64。

两个适配器都只负责协议与媒体转换；返回统一的 48kHz PCM16 单声道 WAV 资产。
密钥仅由装配层从环境变量注入，不进入请求快照、日志或工程文件。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import tarfile
import threading
import uuid
import wave
from pathlib import Path
from typing import Any

import httpx

from ..services.runtime_config import validate_provider_url, validate_public_download_url

MASTER_SAMPLE_RATE = 48000
MAX_AUDIO_BYTES = 128 * 1024 * 1024
MINIMAX_MODELS = {
    "speech-2.8-hd", "speech-2.8-turbo", "speech-2.6-hd", "speech-2.6-turbo",
    "speech-02-hd", "speech-02-turbo", "speech-01-hd", "speech-01-turbo",
}
MINIMAX_DEFAULT_VOICE = "male-qn-qingse"
_AUDIO_SUFFIXES = {".mp3", ".wav", ".pcm", ".flac", ".ogg", ".m4a", ".aac", ".opus"}


def _text_and_speed(req: dict[str, Any], *, text_limit: int) -> tuple[str, float]:
    text = "".join(str(value) for value in (req.get("lineTexts") or [])).strip()
    if not text:
        raise ValueError("INVALID_AUDIO: 文本为空")
    if len(text) > text_limit:
        raise ValueError(f"INVALID_AUDIO: 单条朗读文本超过 {text_limit} 字，请拆分话轮")
    try:
        speed = float(req.get("speedRatio", 1.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("语速必须大于 0") from exc
    if not 0.5 <= speed <= 2.0:
        raise ValueError("语速必须在 0.5–2.0 之间")
    return text, speed


def _response_message(payload: Any, fallback: str) -> str:
    """提取受控的业务错误，不回显请求头、密钥或完整响应体。"""
    if not isinstance(payload, dict):
        return fallback
    base = payload.get("base_resp")
    if isinstance(base, dict) and base.get("status_code") not in (None, 0):
        message = str(base.get("status_msg") or "未知错误")[:240]
        return f"{message}（{base.get('status_code')}）"
    if type(payload.get("code")) is int and payload.get("code") != 0:
        return f"{str(payload.get('message') or '未知错误')[:240]}（{payload['code']}）"
    return fallback


def build_minimax_body(req: dict[str, Any], default_model: str, configured_voice: str = "") -> dict[str, Any]:
    """构造 MiniMax 异步 T2A 请求；空音色使用参考项目的系统音色。"""
    text, speed = _text_and_speed(req, text_limit=50_000)
    model = str(req.get("modelId") or default_model).strip()
    if model not in MINIMAX_MODELS:
        raise ValueError(f"MiniMax 异步语音合成不支持模型 {model}")
    voice = str(req.get("voiceId") or configured_voice or MINIMAX_DEFAULT_VOICE).strip()
    body: dict[str, Any] = {
        "model": model,
        "text": text,
        "voice_setting": {"voice_id": voice, "speed": speed, "vol": 1, "pitch": 0},
        "audio_setting": {
            "audio_sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1,
        },
        "language_boost": "auto",
        "aigc_watermark": False,
    }
    emotion = str((req.get("emotion") or {}).get("providerEmotion") or "").strip()
    if emotion in {"happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm"}:
        body["voice_setting"]["emotion"] = emotion
    return body


def build_volcengine_body(req: dict[str, Any], default_model: str, configured_voice: str = "") -> dict[str, Any]:
    """构造火山语音合成 1.0 请求；speaker 必须明确，避免悄悄使用未知声线。"""
    text, speed = _text_and_speed(req, text_limit=3_000)
    model = str(req.get("modelId") or default_model).strip()
    if model != "seed-audio-1.0":
        raise ValueError("火山语音合成 1.0 当前仅支持 seed-audio-1.0")
    speaker = str(req.get("voiceId") or configured_voice).strip()
    if not speaker:
        raise ValueError("火山引擎需要填写 speaker 音色 ID")
    return {
        "model": model,
        "text_prompt": text,
        "speaker": speaker,
        "audio_config": {
            "format": "wav", "sample_rate": MASTER_SAMPLE_RATE,
            "speech_rate": max(-50, min(100, round((speed - 1.0) * 100))),
            "loudness_rate": 0, "pitch_rate": 0, "enable_subtitle": False,
        },
        "watermark": {"aigc_watermark": False, "aigc_metadata": {"enable": False}},
    }


def audio_from_tar(data: bytes) -> bytes:
    """只读提取归档中的首个音频成员，不写目录、不信任成员路径。"""
    if len(data) < 1024 or not data[257:263].startswith(b"ustar"):
        return data
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                if not member.isfile() or Path(member.name).suffix.lower() not in _AUDIO_SUFFIXES:
                    continue
                if not 0 < member.size <= MAX_AUDIO_BYTES:
                    raise ValueError("MiniMax 归档中的音频大小无效")
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                payload = extracted.read(MAX_AUDIO_BYTES + 1)
                if len(payload) != member.size:
                    raise ValueError("MiniMax 归档中的音频不完整")
                return payload
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError("MiniMax 音频归档无法读取") from exc
    raise ValueError("MiniMax 下载结果中没有音频成员")


class _CloudTTSBase:
    name = "cloud-tts"
    url_field = ""

    def __init__(self, *, api_key: str, base_url: str, model: str, voices: dict[str, str],
                 artifacts_root: Path, artifact_store) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voices = voices
        self.artifacts_root = Path(artifacts_root)
        self.artifacts = artifact_store
        self.capabilities = {
            "simulated": False, "engine": self.name, "provider": self.name,
            "voiceCloning": False, "emotion": False, "speed": True, "pause": True,
            "pronunciation": False, "timestamps": False, "streaming": False,
            "cancel": False, "paid": True, "sampleRate": MASTER_SAMPLE_RATE,
        }

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    async def _json(self, method: str, path: str, *, payload: dict | None = None,
                    params: dict[str, str] | None = None, timeout: float = 60) -> dict:
        await asyncio.to_thread(validate_provider_url, self.base_url, self.url_field, resolve=True)
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
                response = await client.request(
                    method, self.base_url + path, headers=self._headers(), json=payload, params=params,
                )
        except (httpx.HTTPError, OSError) as exc:
            raise RuntimeError(f"{self.name} 连接失败或超时，请检查网络后重试") from exc
        if 300 <= response.status_code < 400:
            raise RuntimeError(f"{self.name} 返回重定向，为避免凭据外发已拒绝")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{self.name} 返回了无法解析的响应（HTTP {response.status_code}）") from exc
        if response.status_code >= 400:
            raise RuntimeError(f"{self.name} 请求失败（HTTP {response.status_code}）：{_response_message(data, '上游拒绝请求')}")
        message = _response_message(data, "")
        if message:
            raise RuntimeError(f"{self.name} 合成失败：{message}")
        if not isinstance(data, dict):
            raise RuntimeError(f"{self.name} 响应结构无效")
        return data

    async def _download(self, url: str) -> bytes:
        await asyncio.to_thread(validate_public_download_url, url, resolve=True)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600), trust_env=False,
                                         follow_redirects=False) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"MiniMax 音频下载失败（HTTP {response.status_code}）")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_AUDIO_BYTES:
                            raise ValueError("云端音频超过 128MB 上限")
                    return bytes(chunks)
        except (httpx.HTTPError, OSError) as exc:
            raise RuntimeError("云端音频下载失败或超时") from exc

    async def _save_audio(self, raw: bytes, suffix: str, req: dict[str, Any],
                          *, provider_task_id: str = "", voice_id: str = "") -> dict[str, Any]:
        if not raw or len(raw) > MAX_AUDIO_BYTES:
            raise ValueError("云端音频为空或超过大小上限")
        if suffix not in _AUDIO_SUFFIXES:
            raise ValueError("云端音频格式不受支持")
        from ..api.assets import _transcode_audio

        audio_dir = self.artifacts_root / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        asset_id = f"AUD-{uuid.uuid4().hex[:12]}"
        source = audio_dir / f"{asset_id}{suffix}"
        target = audio_dir / f"{asset_id}.wav"
        source.write_bytes(raw)
        registered = False
        try:
            returncode, _, error = await _transcode_audio(source, target, timeout=180)
            if returncode:
                detail = error.decode("utf-8", "replace")[-240:]
                raise ValueError("云端音频转码失败：" + detail)
            with wave.open(str(target), "rb") as reader:
                if (reader.getframerate() != MASTER_SAMPLE_RATE or reader.getnchannels() != 1
                        or reader.getsampwidth() != 2):
                    raise ValueError("云端音频未转成 48kHz PCM16 单声道")
                count = reader.getnframes()
            if count <= 0:
                raise ValueError("云端音频没有有效样本")
            speaker = str(req.get("speaker") or "A")
            model = str(req.get("modelId") or self.model)
            dependency = hashlib.sha256(json.dumps({
                "provider": self.name, "speaker": speaker, "model": model, "voiceId": voice_id,
                "text": "".join(req.get("lineTexts") or []), "speedRatio": req.get("speedRatio", 1),
            }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            self.artifacts.register(
                asset_id, "audio", str(target), dependency,
                params_snapshot={
                    "provider": self.name, "speaker": speaker, "modelId": model,
                    "voiceId": voice_id, "speedRatio": req.get("speedRatio", 1),
                    "chars": len("".join(req.get("lineTexts") or [])),
                    "providerTaskId": provider_task_id,
                },
                workflow_version=self.name,
            )
            registered = True
            return {
                "sampleRate": MASTER_SAMPLE_RATE, "sampleCount": count,
                "durationMs": round(count * 1000 / MASTER_SAMPLE_RATE),
                "providerTaskId": provider_task_id, "audioAssetId": asset_id,
                "audioPath": str(target),
            }
        finally:
            source.unlink(missing_ok=True)
            if not registered:
                target.unlink(missing_ok=True)

    async def audition(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self.synthesize(req)


class MiniMaxTTSProvider(_CloudTTSBase):
    name = "minimax"
    url_field = "minimax_base_url"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.poll_interval = 2.0
        self.poll_timeout = 600.0
        self._journal_path = self.artifacts_root / "minimax-tasks.json"
        self._journal_lock = threading.Lock()
        self.capabilities.update({"engine": "MiniMax T2A Async v2", "asynchronous": True})

    @staticmethod
    def _journal_key(req: dict[str, Any], body: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps({
            "requestKey": req.get("requestKey") or "standalone",
            "unitId": req.get("unitId") or "audition", "body": body,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _saved_task(self, key: str) -> dict[str, str]:
        with self._journal_lock:
            try:
                data = json.loads(self._journal_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except (OSError, ValueError) as exc:
                raise RuntimeError("MiniMax 本地任务台账无法读取；为避免重复创建，已停止重试") from exc
            entry = data.get(key, {})
            return {"taskId": str(entry.get("taskId") or ""), "fileId": str(entry.get("fileId") or "")}

    def _remember_task(self, key: str, task_id: str, file_id: str = "") -> None:
        """原子持久化远端 ID；台账只有哈希与任务号，不含文本、密钥或声线。"""
        with self._journal_lock:
            try:
                data = json.loads(self._journal_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = {}
            except (OSError, ValueError) as exc:
                raise RuntimeError("MiniMax 本地任务台账无法读取；为避免重复创建，已停止重试") from exc
            data[key] = {"taskId": task_id, "fileId": file_id}
            self._journal_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._journal_path.with_name(f".{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False)
                    handle.flush()
                temporary.replace(self._journal_path)
            finally:
                temporary.unlink(missing_ok=True)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def synthesize(self, req: dict[str, Any]) -> dict[str, Any]:
        speaker = str(req.get("speaker") or "A")
        configured_voice = self.voices.get(speaker, "")
        body = build_minimax_body(req, self.model, configured_voice)
        journal_key = self._journal_key(req, body)
        saved = self._saved_task(journal_key)
        task_id = saved.get("taskId", "")
        file_id = saved.get("fileId", "")
        if not task_id and not file_id:
            created = await self._json("POST", "/v1/t2a_async_v2", payload=body)
            task_id = str(created.get("task_id") or "").strip()
            file_id = str(created.get("file_id") or "").strip()
            if task_id or file_id:
                # 创建成功后先落台账再轮询。后续失败/重试只查询同一 task_id。
                self._remember_task(journal_key, task_id, file_id)
        if task_id:
            file_id = await self._wait_for_file(task_id)
            self._remember_task(journal_key, task_id, file_id)
        if not file_id:
            raise RuntimeError("MiniMax 未返回 task_id 或 file_id")
        retrieved = await self._json("GET", "/v1/files/retrieve", params={"file_id": file_id})
        download_url = str((retrieved.get("file") or {}).get("download_url") or "").strip()
        if not download_url:
            raise RuntimeError("MiniMax 未返回音频下载地址")
        raw = audio_from_tar(await self._download(download_url))
        return await self._save_audio(raw, ".mp3", req, provider_task_id=task_id,
                                      voice_id=body["voice_setting"]["voice_id"])

    async def _wait_for_file(self, task_id: str) -> str:
        deadline = asyncio.get_running_loop().time() + self.poll_timeout
        last_status = ""
        while asyncio.get_running_loop().time() < deadline:
            payload = await self._json("GET", "/v1/query/t2a_async_query_v2",
                                       params={"task_id": task_id})
            last_status = str(payload.get("status") or last_status)
            file_id = str(payload.get("file_id") or "").strip()
            if file_id and "success" in last_status.lower():
                return file_id
            if any(word in last_status.lower() for word in ("fail", "error", "expired", "timeout", "cancel")):
                raise RuntimeError(f"MiniMax 合成任务失败（状态 {last_status or '未知'}）")
            await asyncio.sleep(self.poll_interval)
        raise TimeoutError(f"MiniMax 合成超时（最后状态 {last_status or '未知'}）")


class VolcengineTTSProvider(_CloudTTSBase):
    name = "volcengine"
    url_field = "volcengine_base_url"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.capabilities.update({"engine": "火山引擎语音合成 1.0", "asynchronous": False})

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key, "X-Api-Request-Id": str(uuid.uuid4()),
                "Content-Type": "application/json"}

    async def synthesize(self, req: dict[str, Any]) -> dict[str, Any]:
        speaker = str(req.get("speaker") or "A")
        configured_voice = self.voices.get(speaker, "")
        body = build_volcengine_body(req, self.model, configured_voice)
        # 与参考实现一致，同步长文本请求使用独立的 120 秒有界超时。
        payload = await self._json("POST", "/api/v3/tts/create", payload=body, timeout=120)
        encoded = payload.get("audio") if isinstance(payload, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise RuntimeError("火山引擎未返回音频数据")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("火山引擎返回的 Base64 音频无效") from exc
        return await self._save_audio(raw, ".wav", req, provider_task_id="",
                                      voice_id=str(body["speaker"]))
