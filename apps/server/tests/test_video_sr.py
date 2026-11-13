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