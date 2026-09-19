"""ComfyUI IndexTTS 2.5 真实 TTS 适配器（关口 A 落地，链路实测 2026-09-19 验证）。

契约对齐 MockTTSProvider：synthesize(req) → {sampleRate:48000, sampleCount, audioAssetId,...}。
引擎产物为 float32/22050 wav（实测），本适配器统一转 PCM16/48kHz mono 母轨后登记资产。
ComfyUI 实例约定：主包实例 @ http://127.0.0.1:8188（视频用 Runtime 实例，两实例互斥由 GPU 队列串行保证）。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import struct
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

# 4 节点 API 工作流（与 本地引擎工作流/indextts2.5-tts_参考工作流.json 同源）
WORKFLOW: dict[str, Any] = {
    "1": {"class_type": "BSAI_IndexTTS2.5Loader", "inputs": {
        "use_bf16_使用BF16": True, "device_设备": "auto",
        "use_qwen_emo_千问情绪模型": False, "force_reload_强制重载": False}},
    "2": {"class_type": "BSAI_IndexTTS2.5Synthesis", "inputs": {
        "tts_model_TTS模型": ["1", 0], "text_文本": "", "reference_audio_参考音频": ["4", 0],
        "lang_语言": "ZH", "use_emo_text_启用情绪文本": False, "use_random_随机生成": False,
        "duration_factor_语速因子": 1.0, "max_text_tokens_per_segment_每段最大文本Token": 100,
        "max_mel_tokens_最大MelToken": 1500, "temperature_温度": 0.8, "top_p_核采样": 0.8,
        "top_k_TopK采样": 30, "length_penalty_长度惩罚": 0.0, "num_beams_束宽": 3,
        "repetition_penalty_重复惩罚": 10.0, "do_sample_采样模式": True, "verbose_详细日志": False}},
    "3": {"class_type": "BSAI_IndexTTS2.5SaveAudio", "inputs": {
        "audio_音频": ["2", 0], "filename_prefix_文件名前缀": "duocast/tts",
        "format_格式": "wav", "mp3_bitrate_MP3码率": 192, "output_gain_输出增益": 1.0}},
    "4": {"class_type": "BSAI_IndexTTS2.5LoadAudio", "inputs": {"audio_音频": "reference.wav"}},
}

MASTER_SAMPLE_RATE = 48000


def _opener():
    # 本机 localhost 会被系统代理劫持（Windows 环境陷阱），必须显式直连。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _parse_wav(data: bytes) -> tuple[int, int, list[float]]:
    """返回 (sampleRate, channels, mono samples float)。支持 PCM16 / IEEE-float32。"""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("TTS_BAD_WAV: 非 RIFF/WAVE")
    i = data.find(b"fmt ")
    if i < 0:
        raise ValueError("TTS_BAD_WAV: 缺 fmt 块")
    fmt_tag, channels, sample_rate = struct.unpack("<HhI", data[i + 8:i + 16])
    bits = struct.unpack("<H", data[i + 22:i + 24])[0]
    j = data.find(b"data")
    if j < 0:
        raise ValueError("TTS_BAD_WAV: 缺 data 块")
    n_bytes = struct.unpack("<I", data[j + 4:j + 8])[0]
    payload = data[j + 8:j + 8 + n_bytes]
    if fmt_tag == 3 and bits == 32:
        samples = struct.unpack(f"<{len(payload) // 4}f", payload)
    elif fmt_tag == 1 and bits == 16:
        samples = [s / 32768.0 for s in struct.unpack(f"<{len(payload) // 2}h", payload)]
    else:
        raise ValueError(f"TTS_BAD_WAV: 不支持的格式 tag={fmt_tag} bits={bits}")
    if channels > 1:
        samples = [sum(samples[k:k + channels]) / channels for k in range(0, len(samples) - channels + 1, channels)]
    return sample_rate, 1, list(samples)


def _resample_linear(samples: list[float], src_sr: int, dst_sr: int) -> list[float]:
    if src_sr == dst_sr or not samples:
        return samples
    n = len(samples)
    out_len = int(round(n * dst_sr / src_sr))
    scale = (n - 1) / max(out_len - 1, 1)
    out = [0.0] * out_len
    for k in range(out_len):
        pos = k * scale
        i0 = int(pos)
        i1 = min(i0 + 1, n - 1)
        frac = pos - i0
        out[k] = samples[i0] * (1 - frac) + samples[i1] * frac
    return out


def _write_pcm16_wav(path: Path, samples: list[float], sample_rate: int) -> int:
    frames = b"".join(
        struct.pack("<h", max(-32768, min(32767, int(s * 32767)))) for s in samples
    )
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    data = b"data" + struct.pack("<I", len(frames)) + frames
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(fmt) + len(data)) + b"WAVE" + fmt + data)
    return len(samples)


class ComfyUITTSProvider:
    """TTSProvider 协议的本地 ComfyUI 实现。GPU 串行由任务队列 gpu=1 保证。"""

    name = "comfyui-indextts"

    def __init__(self, base_url: str, input_dir: Path, refs: dict[str, str],
                 artifacts_root: Path, artifact_store) -> None:
        self.base_url = base_url.rstrip("/")
        self.input_dir = Path(input_dir)
        self.refs = refs
        self.artifacts_root = Path(artifacts_root)
        self.artifacts = artifact_store
        self.capabilities = {
            "simulated": False,
            "engine": "ComfyUI IndexTTS 2.5",
            "voiceCloning": True, "emotion": False, "speed": True, "pause": True,
            "pronunciation": False, "timestamps": False, "streaming": False,
            "cancel": False, "paid": False, "sampleRate": MASTER_SAMPLE_RATE,
        }

    # ---- TTSProvider 协议 ----
    async def synthesize(self, req: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._synthesize_sync, req)

    async def audition(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self.synthesize(req)

    # ---- 同步实现 ----
    def _speaker_of(self, req: dict[str, Any]) -> str:
        m = re.search(r"-(A|B)-", str(req.get("voiceBindingId", "")))
        return m.group(1) if m else "A"

    def _ensure_ref_in_input(self, speaker: str) -> str:
        src = self.refs.get(speaker) or self.refs.get("A")
        if not src:
            raise RuntimeError("TTS_NO_REF: 未配置声线参考音频（DUOCAST_TTS_REF_A/B）")
        src = Path(src)
        if not src.exists():
            raise RuntimeError(f"TTS_NO_REF: 参考音频不存在 {src}")
        name = f"duocast_ref_{speaker}{src.suffix.lower()}"
        dst = self.input_dir / name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            self.input_dir.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
        return name

    def _http_json(self, path: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json"} if payload is not None else {},
        )
        try:
            with _opener().open(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            raise RuntimeError(f"TTS_COMFYUI_ERROR {path}: {e.code} {detail}") from e
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise RuntimeError(
                f"TTS_UNAVAILABLE: ComfyUI({self.base_url}) 不在线，请先启动主包实例（启动-IndexTTS25-ComfyUI.bat）"
            ) from e

    def _get_bytes(self, path: str) -> bytes:
        try:
            with _opener().open(self.base_url + path, timeout=120) as r:
                return r.read()
        except OSError as e:
            raise RuntimeError(f"TTS_UNAVAILABLE: 拉取产物失败 {path}: {e}") from e

    def _synthesize_sync(self, req: dict[str, Any]) -> dict[str, Any]:
        text = "".join(req.get("lineTexts") or []).strip()
        if not text:
            raise ValueError("INVALID_AUDIO: 文本为空")
        try:
            speed = float(req.get("speedRatio", 1.0))
        except (TypeError, ValueError):
            speed = 1.0
        if speed <= 0:
            raise ValueError("语速必须大于 0")
        speaker = self._speaker_of(req)
        ref_name = self._ensure_ref_in_input(speaker)

        wf = copy.deepcopy(WORKFLOW)
        wf["2"]["inputs"]["text_文本"] = text
        wf["2"]["inputs"]["duration_factor_语速因子"] = max(0.5, min(2.0, speed))
        wf["4"]["inputs"]["audio_音频"] = ref_name
        tag = uuid.uuid4().hex[:8]
        wf["3"]["inputs"]["filename_prefix_文件名前缀"] = f"duocast/tts_{tag}"

        prompt_id = self._http_json("/prompt", {"prompt": wf, "client_id": "duocast-server"})["prompt_id"]
        h = self._wait_history(prompt_id)
        outputs = h.get("outputs", {})
        files = [a for o in outputs.values() for a in (o.get("audio") or [])]
        if not files:
            raise RuntimeError(f"TTS_NO_OUTPUT: ComfyUI 未产出音频 (prompt {prompt_id})")
        a = files[0]
        view = (f"/view?filename={a['filename']}&type=output"
                f"&subfolder={a.get('subfolder', '')}&format=wav")
        raw = self._get_bytes(view)

        src_sr, _, samples = _parse_wav(raw)
        samples = _resample_linear(samples, src_sr, MASTER_SAMPLE_RATE)
        audio_dir = self.artifacts_root / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        dependency_hash = hashlib.sha256(
            json.dumps([text, speaker, speed, a["filename"]], ensure_ascii=False).encode()
        ).hexdigest()[:16]
        artifact_id = f"AUD-{uuid.uuid4().hex[:12]}"
        path = audio_dir / f"{artifact_id}.wav"
        count = _write_pcm16_wav(path, samples, MASTER_SAMPLE_RATE)
        self.artifacts.register(
            artifact_id, "audio", str(path), dependency_hash,
            params_snapshot={"provider": self.name, "speaker": speaker, "speedRatio": speed,
                             "chars": len(text), "sourceSampleRate": src_sr},
            workflow_version="indextts2.5-comfyui-4node",
        )
        return {
            "sampleRate": MASTER_SAMPLE_RATE, "sampleCount": count,
            "durationMs": round(count * 1000 / MASTER_SAMPLE_RATE),
            "providerTaskId": prompt_id, "audioAssetId": artifact_id, "audioPath": str(path),
        }

    def _wait_history(self, prompt_id: str, timeout_s: int = 600, interval_s: float = 2.0) -> dict:
        import time
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            hist = self._http_json(f"/history/{prompt_id}")
            if prompt_id in hist:
                entry = hist[prompt_id]
                if entry.get("status", {}).get("status_str") == "error":
                    msgs = [str(m)[:300] for m in (entry.get("status", {}).get("messages") or [])]
                    raise RuntimeError(f"TTS_COMFYUI_JOB_FAILED: {' | '.join(msgs)[:800]}")
                if entry.get("outputs"):
                    return entry
            time.sleep(interval_s)
        raise RuntimeError(f"TTS_TIMEOUT: ComfyUI 任务 {prompt_id} 超 {timeout_s}s 未完成")
