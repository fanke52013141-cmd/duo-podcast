"""AI 视频超分（video_sr / SeedVR2）CPU 回归。

2026-11-13 起 SeedVR2 为正式方案（默认启用，Real-ESRGAN 路径已移除）：
验证开关解析（默认开/显式关）、依赖定位、命令构造纯函数、超分判定、
缺失依赖明确报错、音轨回贴不二次编码。真实画质/耗时留 GPU 按 44 号文档第十节实测。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from duocast.services import video_sr


# ---------- 开关：默认开，显式关 ----------

def test_sr_enabled_by_default(monkeypatch):
    monkeypatch.delenv("DUOCAST_EXPORT_SR", raising=False)
    assert video_sr.sr_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "on", "TRUE", "On", ""])
def test_sr_enabled_values(monkeypatch, val):
    monkeypatch.setenv("DUOCAST_EXPORT_SR", val)
    assert video_sr.sr_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "off", "no", "OFF", "False"])
def test_sr_disabled_values(monkeypatch, val):
    monkeypatch.setenv("DUOCAST_EXPORT_SR", val)
    assert video_sr.sr_enabled() is False


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


# ---------- 命令构造（纯函数） ----------

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


# ---------- 缺失依赖明确报错（不静默降级） ----------

def test_super_resolve_missing_deps_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(video_sr, "find_seedvr2", lambda: None)

    async def run(*a, **k):
        return ""

    with pytest.raises(RuntimeError, match="SeedVR2 依赖缺失"):
        asyncio.run(video_sr.super_resolve_video(tmp_path / "in.mp4", tmp_path / "out.mp4", 1920, 1080, 25, run))


# ---------- 产出回贴：视频 copy + 原音轨 ----------

def test_seedvr2_mux_copy_and_audio(tmp_path, monkeypatch):
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

    asyncio.run(video_sr.super_resolve_video(src, tmp_path / "out.mp4", 1920, 1080, 25, run,
                                             workdir=tmp_path / "work"))
    assert len(calls) == 2
    mux = calls[1]
    assert "-c:v copy" in " ".join(mux) and "-map 1:a:0" in " ".join(mux)


def test_seedvr2_no_output_raises(tmp_path, monkeypatch):
    py, cli, mdl = _seedvr2_tree(tmp_path)
    monkeypatch.setenv("DUOCAST_SEEDVR2_PYTHON", str(py))
    monkeypatch.setattr(video_sr, "_REPO_ROOT", tmp_path)

    async def run(*a, **k):
        return ""

    with pytest.raises(RuntimeError, match="未产出超分视频"):
        asyncio.run(video_sr.super_resolve_video(tmp_path / "in.mp4", tmp_path / "out.mp4",
                                                 1920, 1080, 25, run, workdir=tmp_path / "work"))