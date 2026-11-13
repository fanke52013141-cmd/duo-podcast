"""AI 视频超分（video_sr）CPU 回归。

默认关闭=现网导出行为零变化；验证开关解析、Real-ESRGAN 二进制定位、超分判定纯函数、
缺失依赖时的明确报错。真实超分画质/耗时留 GPU 按 44 号文档实测（Real-ESRGAN 为本地外部依赖）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from duocast.services import video_sr


# ---------- 开关：默认关，解析 ----------

def test_sr_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DUOCAST_EXPORT_SR", raising=False)
    assert video_sr.sr_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "on", "realesrgan", "TRUE", "On"])
def test_sr_enabled_values(monkeypatch, val):
    monkeypatch.setenv("DUOCAST_EXPORT_SR", val)
    assert video_sr.sr_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "off", "", "no", "xyz"])
def test_sr_disabled_values(monkeypatch, val):
    monkeypatch.setenv("DUOCAST_EXPORT_SR", val)
    assert video_sr.sr_enabled() is False


# ---------- Real-ESRGAN 定位 ----------

def test_find_realesrgan_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "realesrgan-ncnn-vulkan.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv("DUOCAST_REALESRGAN_BIN", str(fake))
    assert video_sr.find_realesrgan() == fake


def test_find_realesrgan_env_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DUOCAST_REALESRGAN_BIN", str(tmp_path / "nope.exe"))
    assert video_sr.find_realesrgan() is None


def test_find_realesrgan_default_location(tmp_path, monkeypatch):
    monkeypatch.delenv("DUOCAST_REALESRGAN_BIN", raising=False)
    monkeypatch.setattr(video_sr, "_REPO_ROOT", tmp_path)
    assert video_sr.find_realesrgan() is None  # 未放置二进制
    bin_dir = tmp_path / "tools" / "realesrgan"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "realesrgan-ncnn-vulkan.exe"
    fake.write_bytes(b"x")
    assert video_sr.find_realesrgan() == fake


def test_sr_model_dir(tmp_path):
    binary = tmp_path / "realesrgan-ncnn-vulkan.exe"
    assert video_sr.sr_model_dir(binary) == tmp_path / "models"


# ---------- 超分判定纯函数 ----------

@pytest.mark.parametrize("src,target,expected", [
    ((854, 480), (1920, 1080), True),
    ((854, 480), (1280, 720), True),
    ((854, 480), (854, 480), False),
    ((1920, 1080), (854, 480), False),
    ((854, 480), (1920, 480), True),
])
def test_needs_upscale(src, target, expected):
    assert video_sr.needs_upscale(src[0], src[1], target[0], target[1]) is expected


# ---------- 缺失依赖明确报错（不静默降级） ----------

def test_super_resolve_missing_binary_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(video_sr, "find_realesrgan", lambda: None)

    async def run(*a, **k):
        return ""

    with pytest.raises(RuntimeError, match="Real-ESRGAN"):
        asyncio.run(video_sr.super_resolve_video(tmp_path / "in.mp4", tmp_path / "out.mp4", 1920, 1080, 25, run))


def test_super_resolve_missing_model_raises(tmp_path, monkeypatch):
    binary = tmp_path / "realesrgan-ncnn-vulkan.exe"
    binary.write_bytes(b"x")
    monkeypatch.setattr(video_sr, "find_realesrgan", lambda: binary)

    async def run(*a, **k):
        return ""

    with pytest.raises(RuntimeError, match="模型缺失"):
        asyncio.run(video_sr.super_resolve_video(tmp_path / "in.mp4", tmp_path / "out.mp4", 1920, 1080, 25, run))


# ---------- SeedVR2 引擎选择 ----------

def test_sr_engine_default_realesrgan(monkeypatch):
    monkeypatch.delenv("DUOCAST_EXPORT_SR_ENGINE", raising=False)
    assert video_sr.sr_engine() == "realesrgan"


def test_sr_engine_seedvr2(monkeypatch):
    monkeypatch.setenv("DUOCAST_EXPORT_SR_ENGINE", "seedvr2")
    assert video_sr.sr_engine() == "seedvr2"


def test_sr_engine_invalid_raises(monkeypatch):
    monkeypatch.setenv("DUOCAST_EXPORT_SR_ENGINE", "topaz")
    with pytest.raises(ValueError, match="realesrgan 或 seedvr2"):
        video_sr.sr_engine()


# ---------- SeedVR2 依赖定位 ----------

def _seedvr2_tree(tmp_path):
    py = tmp_path / "python.exe"; py.write_bytes(b"x")
    cli = tmp_path / "tools" / "seedvr2" / "inference_cli.py"
    cli.parent.mkdir(parents=True); cli.write_bytes(b"x")
    mdl = tmp_path / "tools" / "seedvr2_models"; mdl.mkdir(parents=True)
    (mdl / "seedvr2_ema_3b_fp8_e4m3fn.safetensors").write_bytes(b"x")
    (mdl / "ema_vae_fp16.safetensors").write_bytes(b"x")
    return py, cli, mdl


def test_find_seedvr2_requires_python_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DUOCAST_SEEDVR2_PYTHON", raising=False)
    monkeypatch.setattr(video_sr, "_REPO_ROOT", tmp_path)
    assert video_sr.find_seedvr2() is None


def test_find_seedvr2_complete(tmp_path, monkeypatch):
    py, cli, mdl = _seedvr2_tree(tmp_path)
    monkeypatch.setenv("DUOCAST_SEEDVR2_PYTHON", str(py))
    monkeypatch.setattr(video_sr, "_REPO_ROOT", tmp_path)
    cfg = video_sr.find_seedvr2()
    assert cfg == {"python": py, "cli": cli, "model_dir": mdl}


def test_find_seedvr2_missing_weights(tmp_path, monkeypatch):
    py, cli, mdl = _seedvr2_tree(tmp_path)
    (mdl / "ema_vae_fp16.safetensors").unlink()
    monkeypatch.setenv("DUOCAST_SEEDVR2_PYTHON", str(py))
    monkeypatch.setattr(video_sr, "_REPO_ROOT", tmp_path)
    assert video_sr.find_seedvr2() is None


def test_find_seedvr2_env_overrides(tmp_path, monkeypatch):
    py, cli, mdl = _seedvr2_tree(tmp_path)
    alt_cli = tmp_path / "elsewhere" / "cli.py"; alt_cli.parent.mkdir(); alt_cli.write_bytes(b"x")
    alt_mdl = tmp_path / "weights"; alt_mdl.mkdir()
    for n in ("seedvr2_ema_3b_fp8_e4m3fn.safetensors", "ema_vae_fp16.safetensors"):
        (alt_mdl / n).write_bytes(b"x")
    monkeypatch.setenv("DUOCAST_SEEDVR2_PYTHON", str(py))
    monkeypatch.setenv("DUOCAST_SEEDVR2_CLI", str(alt_cli))
    monkeypatch.setenv("DUOCAST_SEEDVR2_MODEL_DIR", str(alt_mdl))
    cfg = video_sr.find_seedvr2()
    assert cfg["cli"] == alt_cli and cfg["model_dir"] == alt_mdl


# ---------- SeedVR2 命令构造（纯函数） ----------

@pytest.mark.parametrize("target,short", [((1920, 1080), 1080), ((1280, 720), 720), ((1080, 1920), 1080)])
def test_seedvr2_short_side(target, short):
    assert video_sr.seedvr2_short_side(*target) == short


def test_seedvr2_command_flags(tmp_path, monkeypatch):
    monkeypatch.delenv("DUOCAST_SEEDVR2_ATTENTION", raising=False)
    cfg = {"python": tmp_path / "py.exe", "cli": tmp_path / "cli.py", "model_dir": tmp_path / "mdl"}
    cmd = video_sr.seedvr2_command(cfg, tmp_path / "in.mp4", tmp_path / "out", 1920, 1080)
    assert cmd[0] == str(cfg["python"]) and str(cfg["cli"]) in cmd
    joined = " ".join(cmd)
    for flag in ("--resolution 1080", "--batch_size 5", "--uniform_batch_size",
                 "--temporal_overlap 2", "--attention_mode sageattn_2",
                 "--dit_offload_device cpu", "--vae_offload_device cpu",
                 "--vae_encode_tiled", "--vae_decode_tiled", "--seed 42"):
        assert flag in joined, flag


def test_seedvr2_command_attention_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DUOCAST_SEEDVR2_ATTENTION", "sdpa")
    cfg = {"python": tmp_path / "py.exe", "cli": tmp_path / "cli.py", "model_dir": tmp_path / "mdl"}
    cmd = video_sr.seedvr2_command(cfg, tmp_path / "in.mp4", tmp_path / "out", 1280, 720)
    assert "--attention_mode sdpa" in " ".join(cmd)
    assert "--resolution 720" in " ".join(cmd)


# ---------- SeedVR2 分发与缺失报错 ----------

def test_super_resolve_dispatch_seedvr2(tmp_path, monkeypatch):
    monkeypatch.setenv("DUOCAST_EXPORT_SR_ENGINE", "seedvr2")
    called = {}

    async def fake_seedvr2(src, dst, w, h, run, workdir=None):
        called["seedvr2"] = (w, h)

    monkeypatch.setattr(video_sr, "_super_resolve_seedvr2", fake_seedvr2)

    async def run(*a, **k):
        return ""

    asyncio.run(video_sr.super_resolve_video(tmp_path / "in.mp4", tmp_path / "out.mp4", 1920, 1080, 25, run))
    assert called["seedvr2"] == (1920, 1080)


def test_seedvr2_missing_deps_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(video_sr, "find_seedvr2", lambda: None)

    async def run(*a, **k):
        return ""

    with pytest.raises(RuntimeError, match="seedvr2 但依赖缺失"):
        asyncio.run(video_sr._super_resolve_seedvr2(tmp_path / "in.mp4", tmp_path / "out.mp4", 1920, 1080, run))


def test_seedvr2_mux_copy_and_audio(tmp_path, monkeypatch):
    """CLI 产出后：视频 copy（不二次编码）+ 回贴原音轨。"""
    py, cli, mdl = _seedvr2_tree(tmp_path)
    monkeypatch.setenv("DUOCAST_SEEDVR2_PYTHON", str(py))
    monkeypatch.setattr(video_sr, "_REPO_ROOT", tmp_path)
    src = tmp_path / "in.mp4"; src.write_bytes(b"v")
    calls = []

    async def run(*a, **k):
        calls.append(a)
        if str(cli) in " ".join(map(str, a)):
            out = tmp_path / "work" / "seedvr2_out"
            out.mkdir(parents=True, exist_ok=True)
            (out / "in.mp4").write_bytes(b"sr")
        return ""

    asyncio.run(video_sr._super_resolve_seedvr2(src, tmp_path / "out.mp4", 1920, 1080, run,
                                                workdir=tmp_path / "work"))
    assert len(calls) == 2
    mux = calls[1]
    assert "-c:v copy" in " ".join(mux) and "-map 1:a:0" in " ".join(mux)