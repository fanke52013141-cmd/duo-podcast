"""Vidu official native CLI video adapter.

The paid write operation is submitted at most once for a local request key.  A
small atomic journal records only request/task identifiers and hashes; Vidu
credentials are passed exclusively through the child process environment.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from ..services.owned_process import spawn_owned
from ..services.runtime_config import validate_public_download_url

VIDU_Q3_MODEL = "3.2"
VIDU_Q4_MODEL = "3.4"
VIDU_MODELS = (VIDU_Q4_MODEL, VIDU_Q3_MODEL)
VIDU_RESOLUTIONS = ("540p", "720p", "1080p", "2k", "4k")
VIDU_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")
VIDU_MODES = ("text", "first-frame", "first-last-frame", "reference")
VIDU_BASE_URLS = ("https://service.vidu.cn", "https://service.vidu.com")
MIN_CLI_VERSION = (0, 2, 33)
MAX_CLI_OUTPUT = 2 * 1024 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_VERSION = re.compile(r"vidu-cli\s+v?(\d+)\.(\d+)\.(\d+)", re.IGNORECASE)
_SCRIPT_SUFFIXES = {".bat", ".cmd", ".ps1", ".js", ".cjs", ".mjs", ".py", ".com"}


class ViduCliError(ValueError):
    """Safe, classified adapter error; never includes raw provider output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_vidu_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ViduCliError("INVALID_INPUT", "Vidu 地址无效")
    normalized = value.strip().rstrip("/")
    if normalized not in VIDU_BASE_URLS:
        raise ViduCliError(
            "INVALID_INPUT",
            "Vidu 地址必须为 https://service.vidu.cn 或 https://service.vidu.com",
        )
    return normalized


def build_vidu_args(
    model: str | Mapping[str, Any],
    mode: str = "text",
    prompt: str = "",
    duration: int = 5,
    resolution: str | None = None,
    ratio: str | None = None,
    image_paths: Sequence[str | os.PathLike[str]] | None = None,
    **aliases: Any,
) -> list[str]:
    """Build the v0.2.33+ CLI argv, without credentials or a compatibility API.

    A mapping with the reference implementation's camelCase field names is
    accepted as a convenience for protocol tests and callers.
    """
    if isinstance(model, Mapping):
        source = dict(model)
        model = source.get("model", source.get("modelId", source.get("model_id", "")))
        mode = source.get("mode", mode)
        prompt = source.get("prompt", prompt)
        duration = source.get("duration", duration)
        resolution = source.get("resolution", resolution)
        ratio = source.get("ratio", ratio)
        image_paths = source.get("imagePaths", source.get("image_paths", image_paths))
    model = aliases.pop("model_id", aliases.pop("modelId", model))
    image_paths = aliases.pop("imagePaths", image_paths)
    if aliases:
        raise ViduCliError("INVALID_INPUT", "Vidu 参数包含未知字段")
    if model not in VIDU_MODELS:
        raise ViduCliError("VIDU_MODEL_UNSUPPORTED", "Vidu 仅支持 Q3（3.2）与 Q4（3.4）")
    if mode not in VIDU_MODES:
        raise ViduCliError("INVALID_INPUT", "Vidu 生成模式无效")
    if model == VIDU_Q4_MODEL and mode != "reference":
        raise ViduCliError("INVALID_INPUT", "Vidu Q4 官方 CLI 仅支持参考生视频")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ViduCliError("INVALID_INPUT", "Vidu 提示词不能为空")
    if type(duration) is not int or not 1 <= duration <= 16:
        raise ViduCliError("INVALID_INPUT", "Vidu 视频时长必须为 1–16 秒的整数")
    resolution = resolution or ("540p" if model == VIDU_Q4_MODEL else "1080p")
    allowed_resolutions = VIDU_RESOLUTIONS if model == VIDU_Q4_MODEL else ("1080p",)
    if resolution not in allowed_resolutions:
        raise ViduCliError("INVALID_INPUT", "Vidu 清晰度无效")
    if ratio and ratio not in VIDU_RATIOS:
        raise ViduCliError("INVALID_INPUT", "Vidu 画幅无效")
    if image_paths is None:
        paths = []
    elif isinstance(image_paths, (str, bytes)) or not isinstance(image_paths, Sequence):
        raise ViduCliError("INVALID_INPUT", "Vidu 图片路径必须为数组")
    else:
        paths = list(image_paths)
    expected = {"text": 0, "first-frame": 1, "first-last-frame": 2}.get(mode)
    if expected is not None:
        valid_images = len(paths) == expected
    else:
        valid_images = 1 <= len(paths) <= 7
    if not valid_images or any(not str(path).strip() for path in paths):
        messages = {
            "text": "Vidu 文生视频不能连接图片",
            "first-frame": "Vidu 首帧生视频需要 1 张图片",
            "first-last-frame": "Vidu 首尾帧需要按顺序连接 2 张图片",
            "reference": "Vidu 参考生视频需要 1–7 张图片",
        }
        raise ViduCliError("INVALID_INPUT", messages[mode])
    task_type = {
        "text": "text2video",
        "first-frame": "img2video",
        "first-last-frame": "headtailimg2video",
        "reference": "character2video",
    }[mode]
    args = [
        "task", "submit", "--type", task_type,
        "--model-version", model,
        f"--prompt={prompt.strip()}",
        "--duration", str(duration),
        "--resolution", resolution,
    ]
    if task_type in {"text2video", "character2video"}:
        args.extend(["--aspect-ratio", ratio or "16:9"])
    if model == VIDU_Q3_MODEL and task_type != "text2video":
        args.extend(["--transition", "pro"])
    args.extend(["--schedule-mode", "normal", "--sample-count", "1", "--codec", "h264"])
    for path in paths:
        args.append(f"--image={Path(path)}")
    return args


def parse_vidu_output(raw: str | bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        data = json.loads(text.strip())
        if isinstance(data, dict):
            return data
    except (UnicodeError, ValueError, TypeError, AttributeError):
        pass
    raise ViduCliError("VIDU_BAD_OUTPUT", "Vidu CLI 返回了无法解析的结果；请核对原任务，避免重复提交")


def classify_vidu_task(data: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(data, Mapping):
        raise ViduCliError("VIDU_BAD_OUTPUT", "Vidu 任务响应结构无效")
    state = data.get("state")
    if state == "success":
        creations = data.get("creations")
        first = creations[0] if isinstance(creations, list) and creations and isinstance(creations[0], Mapping) else {}
        url = next((first.get(key) for key in ("nomark_uri", "download_uri", "uri")
                    if isinstance(first.get(key), str) and first.get(key)), "")
        try:
            parsed = urlsplit(url)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                return {"status": "success", "url": url}
        except ValueError:
            pass
        return {"status": "unknown", "error": "Vidu 成功任务没有有效的 HTTPS 视频地址"}
    if state == "failed":
        return {"status": "failed", "error": "Vidu 远端生成失败，请查看原任务"}
    if state in {"canceled", "cancelled"}:
        return {"status": "canceled", "error": "Vidu 任务已取消"}
    # Unknown/missing states remain running: the upstream task may already be billed.
    return {"status": "running"}


def _atomic_json_write(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=str(path.parent), prefix=".vidu-", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
    output = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(output)
        if len(output) + len(chunk) > limit:
            raise ViduCliError("VIDU_BAD_OUTPUT", "Vidu CLI 输出超过本地限制；请核对原任务")
        output.extend(chunk)


async def _drain(stream: asyncio.StreamReader) -> None:
    while await stream.read(65536):
        pass


class ViduVideoProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        artifact_store: Any,
        binary_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = validate_vidu_base_url(base_url)
        if model not in VIDU_MODELS:
            raise ViduCliError("VIDU_MODEL_UNSUPPORTED", "Vidu 模型仅支持 3.2 或 3.4")
        self.model = model
        self.artifacts = artifact_store
        self.binary_path = str(binary_path) if binary_path else None
        self._resolved_binary: str | None = None
        root = getattr(artifact_store, "root", None)
        if root is None:
            raise ValueError("Vidu 需要 ArtifactStore 及其 root")
        self.root = Path(root)
        self._journal_path = self.root / "vidu-tasks.json"
        self._journal_lock = threading.RLock()
        self._request_locks: dict[str, asyncio.Lock] = {}
        self.poll_interval = 2.0
        self.poll_timeout = 20 * 60.0
        self.max_video_bytes = MAX_VIDEO_BYTES
        self.capabilities = {
            "simulated": False,
            "provider": "vidu",
            "engine": "Vidu official CLI",
            "models": list(VIDU_MODELS),
            "modes": list(VIDU_MODES),
            "paid": True,
            "asynchronous": True,
            "progress": True,
            "cancel": False,
            "candidate": True,
        }

    def _binary(self) -> str:
        if self._resolved_binary:
            return self._resolved_binary
        selected = self.binary_path
        if selected is None:
            executable = "vidu-cli.exe" if os.name == "nt" else "vidu-cli"
            roots: list[Path] = []
            appdata = os.environ.get("APPDATA")
            if appdata:
                roots.append(Path(appdata) / "npm")
            roots.extend(Path(item) for item in os.environ.get("PATH", "").split(os.pathsep) if item)
            candidates: list[Path] = []
            for root in roots:
                candidates.extend([
                    root / executable,
                    root / "node_modules" / "vidu-cli" / "bin" / executable,
                    root.parent / "lib" / "node_modules" / "vidu-cli" / "bin" / executable,
                ])
            selected = str(next((path for path in candidates if path.is_file()), Path(executable)))
        suffix = Path(selected).suffix.lower()
        if suffix in _SCRIPT_SUFFIXES or (os.name == "nt" and suffix != ".exe"):
            raise ViduCliError("VIDU_CLI_MISSING", "Vidu 仅允许原生可执行文件，不允许 shell 或脚本 shim")
        if self.binary_path and not Path(selected).is_file():
            raise ViduCliError("VIDU_CLI_MISSING", "Vidu CLI 原生可执行文件不存在")
        return str(selected)

    async def _run_cli(self, args: Sequence[str], *, timeout: float, credentials: bool) -> tuple[int, bytes]:
        if credentials:
            if not isinstance(self.api_key, str) or not self.api_key.strip():
                raise ViduCliError("PROVIDER_NO_KEY", "请配置 Vidu Token")
        binary = self._binary()
        env = os.environ.copy()
        if credentials:
            env["VIDU_TOKEN"] = self.api_key
            env["VIDU_BASE_URL"] = self.base_url
        try:
            process = await spawn_owned(
                binary, *map(str, args),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (OSError, ValueError) as exc:
            raise ViduCliError("VIDU_CLI_MISSING", "Vidu CLI 不可用，请安装原生 vidu-cli >=0.2.33") from exc
        try:
            # Stderr is drained but never retained or forwarded (it may contain secrets).
            result = await asyncio.wait_for(
                asyncio.gather(_read_limited(process.stdout, MAX_CLI_OUTPUT), _drain(process.stderr), process.wait()),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ViduCliError("TIMEOUT", "Vidu CLI 调用超时；已提交任务请先核对，避免重复计费") from exc
        finally:
            await process.stop()
        return int(process.returncode or 0), result[0]

    async def check(self) -> dict[str, Any]:
        """Local executable/version check only: no token use and no network request."""
        returncode, output = await self._run_cli(["--version"], timeout=20, credentials=False)
        match = _VERSION.search(output.decode("utf-8", "replace")[:4096])
        version = tuple(int(part) for part in match.groups()) if match else None
        if returncode or version is None or version < MIN_CLI_VERSION:
            raise RuntimeError("Vidu Q4 需要原生 vidu-cli v0.2.33 或更新版本")
        self._resolved_binary = self._binary()
        return {
            "ok": True,
            "provider": "vidu",
            "version": ".".join(str(part) for part in version),
            "message": "本地 Vidu CLI 版本检查通过；未发起网络或付费生成",
        }

    async def _run_json(self, args: Sequence[str], *, timeout: float = 120) -> dict[str, Any]:
        returncode, raw = await self._run_cli(args, timeout=timeout, credentials=True)
        try:
            data = parse_vidu_output(raw)
        except ViduCliError as exc:
            if returncode:
                raise ViduCliError(
                    "VIDU_CLI_FAILED",
                    "Vidu CLI 调用失败；已提交任务请先核对，避免重复计费",
                ) from exc
            raise
        error = data.get("error")
        if data.get("ok") is False or error:
            detail = error if isinstance(error, Mapping) else {}
            try:
                http_status = int(detail.get("http_status", 0))
            except (TypeError, ValueError):
                http_status = 0
            if detail.get("type") == "client_error":
                raise ViduCliError("INVALID_INPUT", "Vidu 拒绝了本次参数，请检查模型、模式、图片、清晰度与时长")
            if http_status == 429:
                raise ViduCliError("UPSTREAM_RATE_LIMIT", "Vidu 查询触发限流，请稍后继续核对原任务")
            is_get = list(args[:2]) == ["task", "get"]
            if http_status >= 500 and is_get:
                raise ViduCliError("TIMEOUT", "Vidu 暂时无法查询任务，请稍后继续核对原任务")
            if http_status >= 500:
                raise ViduCliError("TASK_OUTCOME_UNKNOWN", "Vidu 提交状态未知，请核对原任务，避免重复计费")
            raise ViduCliError("VIDU_CLI_FAILED", f"Vidu 服务拒绝请求（HTTP {http_status or '未知'}），请检查 Token、额度和参数")
        return data

    @staticmethod
    def _request_hash(generation: Mapping[str, Any]) -> str:
        encoded = json.dumps(generation, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _read_journal(self) -> dict[str, Any]:
        try:
            data = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"tasks": {}}
        except (OSError, ValueError) as exc:
            raise ViduCliError("VIDU_JOURNAL_FAILED", "Vidu 本地任务台账无法读取；为避免重复创建，已停止重试") from exc
        if not isinstance(data, dict) or not isinstance(data.get("tasks", {}), dict):
            raise ViduCliError("VIDU_JOURNAL_FAILED", "Vidu 本地任务台账结构无效；为避免重复创建，已停止重试")
        return data

    def _task_for_request(self, key: str, request_hash: str) -> dict[str, Any]:
        with self._journal_lock:
            entry = self._read_journal().get("tasks", {}).get(key, {})
            if entry and entry.get("requestHash") != request_hash:
                raise ViduCliError("REQUEST_KEY_CONFLICT", "同一 requestKey 的 Vidu 请求参数已改变；请使用新的 requestKey")
            return dict(entry)

    def _write_task(self, key: str, request_hash: str, task_id: str = "", *, submitting: bool = False) -> None:
        with self._journal_lock:
            data = self._read_journal()
            tasks = data.setdefault("tasks", {})
            previous = tasks.get(key, {})
            if previous and previous.get("requestHash") != request_hash:
                raise ViduCliError("REQUEST_KEY_CONFLICT", "同一 requestKey 的 Vidu 请求参数已改变")
            entry = {"requestHash": request_hash, "submitState": "submitting" if submitting else "submitted"}
            if task_id:
                if not _TASK_ID.fullmatch(task_id):
                    raise ViduCliError("VIDU_BAD_OUTPUT", "Vidu 未返回有效任务 ID，请核对原任务")
                entry["taskId"] = task_id
            tasks[key] = entry
            try:
                _atomic_json_write(self._journal_path, data)
            except OSError as exc:
                raise ViduCliError("VIDU_JOURNAL_FAILED", "Vidu 任务台账写入失败；为避免重复计费，请核对提交状态") from exc

    async def _submit_once(self, args: Sequence[str]) -> str:
        data = await self._run_json(args, timeout=120)
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ViduCliError("VIDU_BAD_OUTPUT", "Vidu 未返回有效任务 ID，请核对原任务，避免重复提交")
        return task_id

    async def _poll(self, task_id: str) -> dict[str, str]:
        data = await self._run_json(["task", "get", task_id, "--details"], timeout=45)
        if data.get("task_id") != task_id:
            raise ViduCliError("VIDU_BAD_OUTPUT", "Vidu 返回的任务 ID 不匹配")
        return classify_vidu_task(data)

    async def _wait(self, task_id: str, progress: Any = None) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.poll_timeout
        while True:
            state = await self._poll(task_id)
            status = state.get("status")
            if status == "success" and state.get("url"):
                return state["url"]
            if status in {"failed", "canceled"}:
                raise ViduCliError("VIDU_TASK_" + status.upper(), state.get("error", "Vidu 任务未成功"))
            if status == "unknown":
                raise ViduCliError("VIDU_BAD_OUTPUT", state.get("error", "Vidu 成功结果无效"))
            if progress:
                progress("Vidu 原任务生成中")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Vidu 原任务等待超时；重试将只查询原任务")
            await asyncio.sleep(min(self.poll_interval, remaining))

    def _asset(self, asset_id: Any, kinds: set[str]) -> tuple[Path, dict[str, Any]]:
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ViduCliError("MEDIA_NOT_FOUND", "Vidu 资产 ID 无效")
        entry = self.artifacts.by_id(asset_id)
        if not isinstance(entry, Mapping) or entry.get("kind") not in kinds:
            raise ViduCliError("MEDIA_NOT_FOUND", "Vidu 参考资产类型或登记信息无效")
        path = Path(str(entry.get("path", "")))
        if not path.is_file():
            raise ViduCliError("MEDIA_NOT_FOUND", "Vidu 参考资产文件不存在")
        try:
            actual_hash = _sha256_file(path)
        except OSError as exc:
            raise ViduCliError("MEDIA_NOT_FOUND", "Vidu 参考资产无法读取") from exc
        if not entry.get("fileHash") or entry.get("fileHash") != actual_hash:
            raise ViduCliError("MEDIA_INTEGRITY", "Vidu 参考资产哈希已改变")
        return path, dict(entry)

    def _master(self, project: Mapping[str, Any], start: float, duration: int | None = None) -> tuple[Path, dict[str, Any]] | None:
        timeline = project.get("audioTimeline") or project.get("audio_timeline") or {}
        if not isinstance(timeline, Mapping):
            raise ViduCliError("INVALID_INPUT", "Vidu 时间轨结构无效")
        asset_id = timeline.get("masterAudioAssetId") or timeline.get("master_audio_asset_id")
        if not asset_id:
            return None
        master, entry = self._asset(asset_id, {"audio", "mixed"})
        rate = timeline.get("sampleRate", timeline.get("sample_rate", 0))
        count = timeline.get("sampleCount", timeline.get("sample_count", 0))
        if type(rate) is int and type(count) is int and rate > 0 and count > 0:
            total = count / rate
            if start >= total or (duration is not None and start + duration > total + 0.001):
                raise ViduCliError("INVALID_INPUT", "rangeStartSec 与 Vidu 时长超出母轨范围")
        return master, entry

    async def _download(self, url: str) -> bytes:
        await asyncio.to_thread(validate_public_download_url, url, resolve=True)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(180), trust_env=False, follow_redirects=False,
            ) as client:
                async with client.stream("GET", url, headers={"Accept": "video/*"}) as response:
                    if response.status_code != 200:
                        raise ViduCliError("VIDU_DOWNLOAD_FAILED", "Vidu 视频下载失败或被重定向")
                    length = response.headers.get("Content-Length")
                    if length and int(length) > self.max_video_bytes:
                        raise ViduCliError("VIDU_DOWNLOAD_TOO_LARGE", "Vidu 视频超过本地下载大小上限")
                    output = bytearray()
                    async for chunk in response.aiter_bytes():
                        output.extend(chunk)
                        if len(output) > self.max_video_bytes:
                            raise ViduCliError("VIDU_DOWNLOAD_TOO_LARGE", "Vidu 视频超过本地下载大小上限")
                    return bytes(output)
        except ViduCliError:
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ViduCliError("VIDU_DOWNLOAD_FAILED", "Vidu 视频下载失败或超时") from exc

    async def _command(self, *args: Any, timeout: float = 120) -> bytes:
        try:
            process = await spawn_owned(
                *map(str, args), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RuntimeError("本地媒体工具不可用") from exc
        try:
            result = await asyncio.wait_for(
                asyncio.gather(_read_limited(process.stdout, 1024 * 1024), _drain(process.stderr), process.wait()),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError("本地媒体处理超时") from exc
        finally:
            await process.stop()
        if process.returncode:
            raise RuntimeError("本地媒体工具处理失败")
        return result[0]

    async def _probe(self, path: Path) -> dict[str, Any]:
        raw = await self._command(
            "ffprobe", "-v", "error", "-protocol_whitelist", "file",
            "-format_whitelist", "mp4,mov,m4v,matroska,webm", "-count_frames",
            "-show_streams", "-show_format", "-of", "json", path,
            timeout=90,
        )
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Vidu 本地视频信息无法解析") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Vidu 本地视频信息无效")
        return data

    async def _mux(self, video: Path, master: Path, target: Path, start: float, duration: int) -> None:
        await self._command(
            "ffmpeg", "-v", "error", "-nostdin", "-y",
            "-protocol_whitelist", "file", "-format_whitelist", "mp4,mov,m4v,matroska,webm", "-i", video,
            "-protocol_whitelist", "file", "-format_whitelist", "wav,mp3,flac,ogg,oga,opus,m4a,aac",
            "-ss", f"{start:.3f}", "-i", master,
            "-map", "0:v:0", "-map", "1:a:0", "-t", str(duration),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mp4", target,
            timeout=300,
        )

    @staticmethod
    def _video_details(probe: Mapping[str, Any], expected_duration: int, require_audio: bool) -> tuple[float, str, float]:
        streams = probe.get("streams")
        if not isinstance(streams, list):
            raise RuntimeError("Vidu 视频缺少流信息")
        video = next((item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "video"), None)
        if not video:
            raise RuntimeError("Vidu 下载结果没有视频流")
        try:
            width, height = int(video["width"]), int(video["height"])
            rate_text = video.get("avg_frame_rate") or video.get("r_frame_rate")
            fps = float(Fraction(str(rate_text)))
            duration = float(video.get("duration") or (probe.get("format") or {}).get("duration"))
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise RuntimeError("Vidu 视频流参数无效") from exc
        if width <= 0 or height <= 0 or not math.isfinite(fps) or not 1 <= fps <= 120:
            raise RuntimeError("Vidu 视频尺寸或帧率无效")
        if not math.isfinite(duration) or duration <= 0 or abs(duration - expected_duration) > max(0.5, 2 / fps):
            raise RuntimeError("Vidu 视频时长与请求不一致或文件不完整")
        frames = video.get("nb_read_frames", video.get("nb_frames"))
        if frames is not None:
            try:
                if int(frames) <= 0:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Vidu 视频没有完整帧") from exc
        if require_audio and not any(isinstance(item, Mapping) and item.get("codec_type") == "audio" for item in streams):
            raise RuntimeError("Vidu 母轨混音结果缺少音轨")
        normalized_fps: float | int = round(fps) if abs(fps - round(fps)) < 0.0001 else fps
        return duration, f"{width}x{height}", normalized_fps

    async def render(self, req: dict[str, Any], progress: Any = None) -> dict[str, Any]:
        if not isinstance(req, dict):
            raise ViduCliError("INVALID_INPUT", "Vidu 请求结构无效")
        model = req.get("viduModel") or self.model
        mode = req.get("viduMode") or "text"
        duration = req.get("viduDuration", 5)
        resolution = req.get("viduResolution") or ("540p" if model == VIDU_Q4_MODEL else "1080p")
        ratio = req.get("viduRatio") or "16:9"
        prompt = req.get("prompt", "")
        image_ids = req.get("imageArtifactIds") or []
        if not isinstance(image_ids, list):
            raise ViduCliError("INVALID_INPUT", "imageArtifactIds 必须为数组")
        image_paths: list[Path] = []
        image_hashes: list[str] = []
        for asset_id in image_ids:
            path, entry = self._asset(asset_id, {"image"})
            image_paths.append(path)
            image_hashes.append(entry["fileHash"])
        args = build_vidu_args(model, mode, prompt, duration, resolution, ratio, image_paths)
        project = req.get("projectSnapshot") or {}
        if not isinstance(project, Mapping):
            raise ViduCliError("INVALID_INPUT", "projectSnapshot 必须为对象")
        start_value = req.get("rangeStartSec", 0)
        if type(start_value) not in (int, float) or not math.isfinite(start_value) or start_value < 0:
            raise ViduCliError("INVALID_INPUT", "rangeStartSec 必须为有限的非负数")
        start = float(start_value)
        with_master = bool(req.get("withMasterAudio", False))
        master_info = self._master(project, start, duration) if with_master else None
        generation = {
            "provider": "vidu", "baseUrl": self.base_url, "model": model, "mode": mode,
            "prompt": prompt.strip() if isinstance(prompt, str) else prompt,
            "duration": duration, "resolution": resolution, "ratio": ratio,
            "imageArtifactIds": image_ids, "imageHashes": image_hashes,
            "withMasterAudio": with_master, "rangeStartSec": start,
            "masterAudioArtifactId": master_info[1]["artifactId"] if master_info else None,
            "masterAudioHash": master_info[1]["fileHash"] if master_info else None,
        }
        request_hash = self._request_hash(generation)
        request_key = req.get("requestKey") or req.get("clientToken") or "standalone"
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 512:
            raise ViduCliError("INVALID_INPUT", "requestKey 必须为 1–512 字符的非空字符串")
        key = hashlib.sha256(request_key.encode("utf-8")).hexdigest()
        lock = self._request_locks.setdefault(key, asyncio.Lock())
        async with lock:
            saved = self._task_for_request(key, request_hash)
            task_id = saved.get("taskId", "")
            if not task_id:
                if saved:
                    raise ViduCliError("TASK_OUTCOME_UNKNOWN", "Vidu 上次提交结果未确认；不会自动重复提交，请核对原任务")
                # A durable pre-submit marker closes the crash window where the remote
                # accepted a task but its ID never reached this process.
                self._write_task(key, request_hash, submitting=True)
                if progress:
                    progress("Vidu 提交任务")
                task_id = await self._submit_once(args)
                # The upstream ID is durable before the first status query.
                self._write_task(key, request_hash, task_id)
            if progress:
                progress("Vidu 查询原任务")
            url = await self._wait(task_id, progress)
            if progress:
                progress("Vidu 下载并校验视频")
            raw = await self._download(url)
            if not isinstance(raw, bytes) or not raw or len(raw) > self.max_video_bytes:
                raise ViduCliError("VIDU_DOWNLOAD_FAILED", "Vidu 视频为空、格式无效或超过大小上限")
            artifact_id = "VID-" + uuid.uuid4().hex
            folder = self.root / "video" / "vidu"
            folder.mkdir(parents=True, exist_ok=True)
            source = folder / f".{artifact_id}.source.mp4"
            partial = folder / f".{artifact_id}.partial.mp4"
            final = folder / f"{artifact_id}.mp4"
            try:
                await asyncio.to_thread(source.write_bytes, raw)
                muxed = master_info is not None
                if muxed:
                    # Revalidate the authoritative master immediately before use.
                    self._asset(master_info[1]["artifactId"], {"audio", "mixed"})
                    await self._mux(source, master_info[0], partial, start, duration)
                    os.replace(partial, final)
                    source.unlink(missing_ok=True)
                else:
                    os.replace(source, final)
                probe = await self._probe(final)
                actual_duration, actual_resolution, fps = self._video_details(probe, duration, muxed)
                snapshot = {
                    "provider": "vidu", "mode": "api", "viduTaskId": task_id,
                    "requestHash": request_hash, "model": model, "viduMode": mode,
                    "durationSec": duration, "viduResolution": resolution, "viduRatio": ratio,
                    "imageArtifactIds": image_ids, "withMasterAudio": with_master,
                    "masterAudioApplied": muxed, "rangeStartSec": start,
                    "masterAudioArtifactId": master_info[1]["artifactId"] if master_info else None,
                    "masterAudioHash": master_info[1]["fileHash"] if master_info else None,
                    "projectId": req.get("projectId") or project.get("id"),
                }
                self.artifacts.register(
                    artifact_id, "video", str(final), request_hash, snapshot, "vidu-cli-v1",
                )
            except BaseException:
                source.unlink(missing_ok=True)
                partial.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
                raise
            if progress:
                progress("Vidu 候选视频已登记")
            return {
                "artifactIds": [artifact_id],
                "meta": {
                    "candidate": True,
                    "mode": "api",
                    "provider": "vidu",
                    "durationSec": actual_duration,
                    "resolution": actual_resolution,
                    "fps": fps,
                    "path": str(final),
                    "viduTaskId": task_id,
                    "viduModel": model,
                    "viduMode": mode,
                    "viduResolution": resolution,
                    "viduRatio": ratio,
                    "masterAudioApplied": muxed,
                    "qualityStatus": "needs_review",
                },
            }
