"""AI 视频超分（SeedVR2-3B FP8）——标准模式导出增强，默认关闭、渲染页可选开启。

背景：标准模式原生生成 480p，导出 720p/1080p 若用 ffmpeg lanczos 放大只拉伸不增细节。
本模块用 SeedVR2 一步扩散超分（numz/ComfyUI-SeedVR2_VideoUpscaler 独立 CLI），
单帧质感与时序稳定性实测优于 Real-ESRGAN（无磨皮塑料感、无过锐白边、无颗粒沸腾，
见 44 号文档第十节）。细节为模型重建而非真实恢复——观看净提升，非 forensic 级还原。

开关（默认关；本机超分约 2.5h/2分钟片，用户可选外部工具超分，故不默认启用）：
  渲染请求 exportSr=true/false   单次渲染级开关（渲染页"AI 超分"选项），优先于环境变量
  DUOCAST_EXPORT_SR=1            环境变量级默认开启（未传 exportSr 时生效）
  DUOCAST_SEEDVR2_PYTHON=<path>  启用时必填，含 torch/sageattention 的解释器（引擎 venv）
  DUOCAST_SEEDVR2_CLI=<path>     可选，默认 tools/seedvr2/inference_cli.py
  DUOCAST_SEEDVR2_MODEL_DIR=<path> 可选，默认 tools/seedvr2_models/
  DUOCAST_SEEDVR2_ATTENTION      可选，默认 sageattn_2（不可用时 CLI 自动回退 sdpa）

成本（5080 16GB 实测）：约 6.5min/5s 片段 → 2 分钟整片约 2.5h。
16GB 显存必须带 VAE 分块 + DiT/VAE 互卸载（命令已内置），否则编码阶段换页变慢。
外部本地依赖（tools/seedvr2 + tools/seedvr2_models，gitignore）缺失时明确报错而非静默降级。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]


def sr_enabled(override: bool | None = None) -> bool:
    """默认关闭；请求级 override（渲染页选项）优先，其次环境变量 DUOCAST_EXPORT_SR。"""
    if override is not None:
        return bool(override)
    val = os.environ.get("DUOCAST_EXPORT_SR", "0").strip().lower() or "0"
    return val in {"1", "true", "on"}


def needs_upscale(src_w: int, src_h: int, target_w: int, target_h: int) -> bool:
    """目标分辨率高于原生才需要超分（同分辨率/降采样不做）。"""
    return target_w > src_w or target_h > src_h


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


async def super_resolve_video(src: Path, dst: Path, target_w: int, target_h: int,
                              fps: int, run, workdir: Path | None = None) -> None:
    """把 src 视频用 SeedVR2 超分到 target_w×target_h，保留原音轨，输出到 dst。

    run: 异步命令执行器 async run(*args)->str（如 ComfyUIVideoProvider._command）。
    fps 参数保留接口兼容（SeedVR2 沿用输入帧率）。CLI 输出无音轨且沿用输入文件名；
    视频流直接 copy 避免二次编码损失。
    """
    cfg = find_seedvr2()
    if cfg is None:
        raise RuntimeError(
            "AI 超分已启用但 SeedVR2 依赖缺失：请设置 DUOCAST_SEEDVR2_PYTHON（含 torch 的解释器），"
            "并放置 tools/seedvr2/inference_cli.py 与 tools/seedvr2_models/ 权重"
            "（seedvr2_ema_3b_fp8_e4m3fn.safetensors + ema_vae_fp16.safetensors）；"
            "或设 DUOCAST_EXPORT_SR=0 关闭超分")
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