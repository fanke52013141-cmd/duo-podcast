"""AI 视频超分——标准模式导出可选增强（Real-ESRGAN / SeedVR2 双引擎）。

背景：标准模式原生生成 480p，导出 720p/1080p 默认用 ffmpeg lanczos 放大，细节不增、
仍带压缩噪点。开启本开关后改用 AI 超分，观感清晰度显著提升。细节为模型重建而非真实
恢复——对观看是净提升，非 forensic 级还原。

开关（默认关，不改变现有导出行为）：
  DUOCAST_EXPORT_SR=1          启用导出 AI 超分（仅当导出分辨率高于原生时生效）
  DUOCAST_EXPORT_SR_ENGINE     realesrgan（默认）| seedvr2
  DUOCAST_REALESRGAN_BIN=<path> 可选，显式指定 realesrgan-ncnn-vulkan.exe；
                               默认用仓库 tools/realesrgan/（本地依赖，不入库）。

SeedVR2 引擎（numz/ComfyUI-SeedVR2_VideoUpscaler 独立 CLI，3B FP8 + VAE）：
  DUOCAST_SEEDVR2_PYTHON=<path>  必填，含 torch/sageattention 的解释器（引擎 venv）
  DUOCAST_SEEDVR2_CLI=<path>     可选，默认 tools/seedvr2/inference_cli.py
  DUOCAST_SEEDVR2_MODEL_DIR=<path> 可选，默认 tools/seedvr2_models/
  DUOCAST_SEEDVR2_ATTENTION      可选，默认 sageattn_2（不可用时 CLI 自动回退 sdpa）
  实测（5080 16GB，5s/125帧 480p→1080p）：约 6.5 分钟；单帧质感/时序稳定性均优于
  Real-ESRGAN（无磨皮塑料感、无过锐白边、无颗粒沸腾），代价是耗时约为其 6-10×。
  16GB 显存必须带 VAE 分块 + DiT/VAE 互卸载（命令已内置），否则编码阶段换页变慢。

外部本地依赖缺失时启用开关会明确报错而非静默降级。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]


def sr_enabled() -> bool:
    return os.environ.get("DUOCAST_EXPORT_SR", "").strip().lower() in {"1", "true", "on", "realesrgan"}


def sr_engine() -> str:
    """超分引擎选择：默认 realesrgan（现网行为）；seedvr2 为可选高质量档。"""
    val = os.environ.get("DUOCAST_EXPORT_SR_ENGINE", "realesrgan").strip().lower() or "realesrgan"
    if val not in {"realesrgan", "seedvr2"}:
        raise ValueError(f"DUOCAST_EXPORT_SR_ENGINE 须为 realesrgan 或 seedvr2，收到: {val}")
    return val


def find_seedvr2() -> dict | None:
    """定位 SeedVR2 独立 CLI 依赖；任一缺失返回 None（启用时明确报错）。"""
    py = os.environ.get("DUOCAST_SEEDVR2_PYTHON", "").strip()
    if not py:
        return None
    python = Path(py)
    if not python.is_file():
        return None
    cli = Path(os.environ.get("DUOCAST_SEEDVR2_CLI", "").strip()
               or _REPO_ROOT / "tools" / "seedvr2" / "inference_cli.py")
    model_dir = Path(os.environ.get("DUOCAST_SEEDVR2_MODEL_DIR", "").strip()
                     or _REPO_ROOT / "tools" / "seedvr2_models")
    if not cli.is_file():
        return None
    for name in ("seedvr2_ema_3b_fp8_e4m3fn.safetensors", "ema_vae_fp16.safetensors"):
        if not (model_dir / name).is_file():
            return None
    return {"python": python, "cli": cli, "model_dir": model_dir}


def seedvr2_short_side(target_w: int, target_h: int) -> int:
    """SeedVR2 --resolution 语义为目标短边像素。"""
    return min(target_w, target_h)


def seedvr2_command(cfg: dict, src: Path, out_dir: Path, target_w: int, target_h: int) -> list[str]:
    """构造 SeedVR2 CLI 命令（纯函数，CPU 可测）。

    16GB 显存实测必需：VAE 分块（768）+ DiT/VAE 互卸载，否则编码阶段显存顶满换页。
    batch_size=5（4n+1）+ uniform + temporal_overlap=2 保时序一致。
    """
    attention = os.environ.get("DUOCAST_SEEDVR2_ATTENTION", "sageattn_2").strip() or "sageattn_2"
    return [
        str(cfg["python"]), "-X", "utf8", str(cfg["cli"]), str(src),
        "--output", str(out_dir), "--model_dir", str(cfg["model_dir"]),
        "--dit_model", "seedvr2_ema_3b_fp8_e4m3fn.safetensors",
        "--resolution", str(seedvr2_short_side(target_w, target_h)),
        "--batch_size", "5", "--uniform_batch_size", "--temporal_overlap", "2",
        "--attention_mode", attention, "--video_backend", "ffmpeg", "--seed", "42",
        "--dit_offload_device", "cpu", "--vae_offload_device", "cpu",
        "--vae_encode_tiled", "--vae_encode_tile_size", "768",
        "--vae_decode_tiled", "--vae_decode_tile_size", "768",
    ]


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
    """把 src 视频 AI 超分到 target_w×target_h，保留原音轨，输出到 dst。

    run: 异步命令执行器 async run(*args)->str（如 ComfyUIVideoProvider._command）。
    引擎由 DUOCAST_EXPORT_SR_ENGINE 选择：realesrgan（默认）或 seedvr2。
    """
    if sr_engine() == "seedvr2":
        return await _super_resolve_seedvr2(src, dst, target_w, target_h, run, workdir)
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


async def _super_resolve_seedvr2(src: Path, dst: Path, target_w: int, target_h: int,
                                 run, workdir: Path | None = None) -> None:
    """SeedVR2-3B FP8 一步扩散超分：整段视频进 CLI（自带时序一致），回贴原音轨。

    注意：CLI 输出无音轨且沿用输入文件名；视频流直接 copy 避免二次编码损失。
    """
    cfg = find_seedvr2()
    if cfg is None:
        raise RuntimeError(
            "AI 超分引擎设为 seedvr2 但依赖缺失：请设置 DUOCAST_SEEDVR2_PYTHON（含 torch 的解释器），"
            "并放置 tools/seedvr2/inference_cli.py 与 tools/seedvr2_models/ 权重"
            "（seedvr2_ema_3b_fp8_e4m3fn.safetensors + ema_vae_fp16.safetensors）")
    src, dst = Path(src), Path(dst)
    tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="duocast_seedvr2_"))
    out_dir = tmp / "seedvr2_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    await run(*seedvr2_command(cfg, src, out_dir, target_w, target_h))
    produced = out_dir / src.name
    if not produced.is_file():
        candidates = sorted(out_dir.rglob("*.mp4"))
        if not candidates:
            raise RuntimeError("SeedVR2 未产出超分视频")
        produced = candidates[0]
    await run("ffmpeg", "-v", "error", "-y", "-i", str(produced), "-i", str(src),
              "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
              "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(dst))