"""AI 视频超分（Real-ESRGAN）——标准模式导出可选增强。

背景：标准模式原生生成 480p，导出 720p/1080p 默认用 ffmpeg lanczos 放大，细节不增、
仍带压缩噪点。开启本开关后改用 Real-ESRGAN 逐帧超分（4× 重建后降采样到目标分辨率），
观感清晰度显著提升（实测脸部 Laplacian 约为 ffmpeg 的 4×）。细节为模型重建而非真实
恢复，皮肤略有磨皮感——对观看是净提升，非 forensic 级还原。

开关（默认关，不改变现有导出行为）：
  DUOCAST_EXPORT_SR=1          启用导出 AI 超分（仅当导出分辨率高于原生时生效）
  DUOCAST_REALESRGAN_BIN=<path> 可选，显式指定 realesrgan-ncnn-vulkan.exe；
                               默认用仓库 tools/realesrgan/（本地依赖，不入库）。

Real-ESRGAN 为外部本地依赖（xinntao/Real-ESRGAN release，realesrgan-x4plus 模型），
二进制+模型放 tools/realesrgan/，缺失时启用开关会明确报错而非静默降级。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]


def sr_enabled() -> bool:
    return os.environ.get("DUOCAST_EXPORT_SR", "").strip().lower() in {"1", "true", "on", "realesrgan"}


def find_realesrgan() -> Path | None:
    env = os.environ.get("DUOCAST_REALESRGAN_BIN", "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    default = _REPO_ROOT / "tools" / "realesrgan" / "realesrgan-ncnn-vulkan.exe"
    return default if default.is_file() else None


def needs_upscale(src_w: int, src_h: int, target_w: int, target_h: int) -> bool:
    """目标分辨率高于原生才需要超分（同分辨率/降采样不做）。"""
    return target_w > src_w or target_h > src_h


def sr_model_dir(binary: Path) -> Path:
    return binary.parent / "models"


async def super_resolve_video(src: Path, dst: Path, target_w: int, target_h: int,
                              fps: int, run, workdir: Path | None = None) -> None:
    """把 src 视频用 Real-ESRGAN 超分到 target_w×target_h，保留原音轨，输出到 dst。

    run: 异步命令执行器 async run(*args)->str（如 ComfyUIVideoProvider._command）。
    流程：抽帧 → Real-ESRGAN 4× 逐帧重建 → 降采样到目标并回贴原音轨。
    """
    binary = find_realesrgan()
    if binary is None:
        raise RuntimeError("AI 超分已启用但找不到 Real-ESRGAN：请设置 DUOCAST_REALESRGAN_BIN 或放置 tools/realesrgan/")
    model_dir = sr_model_dir(binary)
    if not (model_dir / "realesrgan-x4plus.bin").is_file():
        raise RuntimeError(f"Real-ESRGAN 模型缺失：{model_dir / 'realesrgan-x4plus.bin'}")
    src, dst = Path(src), Path(dst)
    tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="duocast_sr_"))
    frames, sr_frames = tmp / "frames", tmp / "sr_frames"
    frames.mkdir(parents=True, exist_ok=True)
    sr_frames.mkdir(parents=True, exist_ok=True)
    await run("ffmpeg", "-v", "error", "-y", "-i", str(src), str(frames / "f%04d.png"))
    await run(str(binary), "-i", str(frames), "-o", str(sr_frames),
              "-n", "realesrgan-x4plus", "-s", "4", cwd=str(binary.parent))
    await run("ffmpeg", "-v", "error", "-y", "-framerate", str(fps), "-i", str(sr_frames / "f%04d.png"),
              "-i", str(src), "-vf", f"scale={target_w}:{target_h}:flags=lanczos",
              "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-crf", "14", "-preset", "medium",
              "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(dst))