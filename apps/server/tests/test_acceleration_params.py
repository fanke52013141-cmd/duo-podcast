"""44 号 · 标准模式加速调优参数 CPU 回归。

验证：默认值=现网行为（零变化）、环境变量解析、工作流参数化纯函数、
缓存指纹隔离、装配接线可见性。GPU 效果与提速幅度留 RTX 5080 按 44 号文档梯级实测。
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from duocast.adapters.comfyui_video import ComfyUIVideoProvider
from duocast.core.config import Settings
from duocast.main import app, settings

TEMPLATE = json.loads((Path(__file__).resolve().parents[1] / "duocast" / "adapters" / "multitalk_workflow.json").read_text(encoding="utf-8"))


def _provider(tmp_path, **kwargs):
    return ComfyUIVideoProvider(base_url="http://127.0.0.1:8191",
                                input_dir=tmp_path / "input", artifact_store=None, **kwargs)


# ---------- 配置默认与环境变量 ----------

def test_settings_defaults_match_current_behavior(monkeypatch):
    for var in ("DUOCAST_VIDEO_ATTENTION", "DUOCAST_VIDEO_BLOCKS_TO_SWAP", "DUOCAST_VIDEO_PREFETCH_BLOCKS",
                "DUOCAST_VIDEO_TEXT_CFG", "DUOCAST_VIDEO_AUDIO_CFG", "DUOCAST_VIDEO_COMPILE",
                "DUOCAST_VIDEO_LORA", "DUOCAST_VIDEO_LORA_STRENGTH"):
        monkeypatch.delenv(var, raising=False)
    cfg = Settings.from_env()
    assert cfg.video_attention == "sdpa"
    assert cfg.video_blocks_to_swap == 38
    assert cfg.video_prefetch_blocks == 0
    assert cfg.video_text_cfg == 2.0
    assert cfg.video_audio_cfg == 1.0
    assert cfg.video_compile is False
    assert cfg.video_lora == ""
    assert cfg.video_lora_strength == 0.8


def test_settings_env_overrides(monkeypatch):
    monkeypatch.setenv("DUOCAST_VIDEO_ATTENTION", "sageattn")
    monkeypatch.setenv("DUOCAST_VIDEO_BLOCKS_TO_SWAP", "24")
    monkeypatch.setenv("DUOCAST_VIDEO_PREFETCH_BLOCKS", "1")
    monkeypatch.setenv("DUOCAST_VIDEO_TEXT_CFG", "1.0")
    monkeypatch.setenv("DUOCAST_VIDEO_AUDIO_CFG", "2.0")
    monkeypatch.setenv("DUOCAST_VIDEO_COMPILE", "1")
    monkeypatch.setenv("DUOCAST_VIDEO_LORA", "FusionX.safetensors")
    monkeypatch.setenv("DUOCAST_VIDEO_LORA_STRENGTH", "1.0")
    cfg = Settings.from_env()
    assert (cfg.video_attention, cfg.video_blocks_to_swap, cfg.video_prefetch_blocks) == ("sageattn", 24, 1)
    assert (cfg.video_text_cfg, cfg.video_audio_cfg) == (1.0, 2.0)
    assert cfg.video_compile is True
    assert (cfg.video_lora, cfg.video_lora_strength) == ("FusionX.safetensors", 1.0)


def test_settings_zero_swap_allowed(monkeypatch):
    monkeypatch.setenv("DUOCAST_VIDEO_BLOCKS_TO_SWAP", "0")
    assert Settings.from_env().video_blocks_to_swap == 0


def test_settings_bad_values_fall_back(monkeypatch):
    monkeypatch.setenv("DUOCAST_VIDEO_BLOCKS_TO_SWAP", "abc")
    monkeypatch.setenv("DUOCAST_VIDEO_TEXT_CFG", "xyz")
    cfg = Settings.from_env()
    assert cfg.video_blocks_to_swap == 38
    assert cfg.video_text_cfg == 2.0


# ---------- 装配入口边界校验 ----------

@pytest.mark.parametrize("kwargs", [
    {"attention": "xformers"},
    {"blocks_to_swap": 49}, {"blocks_to_swap": -1},
    {"prefetch_blocks": 41},
    {"text_cfg": 31}, {"audio_cfg": -1},
])
def test_provider_rejects_invalid(tmp_path, kwargs):
    with pytest.raises(ValueError):
        _provider(tmp_path, **kwargs)


# ---------- 工作流参数化 ----------

def test_apply_defaults_preserve_template(tmp_path):
    wf = json.loads(json.dumps(TEMPLATE))
    _provider(tmp_path)._apply_acceleration(wf)
    assert wf["122"]["inputs"]["attention_mode"] == "sdpa"
    assert wf["134"]["inputs"]["blocks_to_swap"] == 38
    assert wf["134"]["inputs"]["prefetch_blocks"] == 0
    assert wf["138"]["inputs"]["strength"] == 0.8
    assert wf["138"]["inputs"]["lora"] == TEMPLATE["138"]["inputs"]["lora"]  # 未覆盖保持模板
    assert wf["198"]["inputs"]["audio_cfg_scale"] == 1
    assert wf["199"]["inputs"]["cfg"] == 2.0
    assert "compile_args" not in wf["122"]["inputs"]  # 默认不接线，与现网一致


def test_apply_custom_params(tmp_path):
    wf = json.loads(json.dumps(TEMPLATE))
    _provider(tmp_path, attention="sageattn", blocks_to_swap=24, prefetch_blocks=1,
              text_cfg=1.0, audio_cfg=2.0, compile_model=True,
              lora="FusionX.safetensors", lora_strength=1.0)._apply_acceleration(wf)
    assert wf["122"]["inputs"]["attention_mode"] == "sageattn"
    assert wf["134"]["inputs"]["blocks_to_swap"] == 24
    assert wf["134"]["inputs"]["prefetch_blocks"] == 1
    assert wf["199"]["inputs"]["cfg"] == 1.0
    assert wf["198"]["inputs"]["audio_cfg_scale"] == 2.0
    assert wf["122"]["inputs"]["compile_args"] == ["177", 0]
    assert wf["138"]["inputs"]["lora"] == "FusionX.safetensors"
    assert wf["138"]["inputs"]["strength"] == 1.0


def test_apply_does_not_touch_other_nodes(tmp_path):
    wf = json.loads(json.dumps(TEMPLATE))
    before = json.dumps(wf["192"], sort_keys=True)
    _provider(tmp_path, attention="sageattn")._apply_acceleration(wf)
    assert json.dumps(wf["192"], sort_keys=True) == before


# ---------- 缓存指纹隔离 ----------

def test_fingerprint_separates_configs(tmp_path):
    base = _provider(tmp_path).acceleration_fingerprint()
    tuned = _provider(tmp_path, blocks_to_swap=24, prefetch_blocks=1).acceleration_fingerprint()
    cfg_swap = _provider(tmp_path, text_cfg=1.0, audio_cfg=2.0).acceleration_fingerprint()
    assert base != tuned != cfg_swap and base != cfg_swap
    assert base == _provider(tmp_path).acceleration_fingerprint()  # 同配置稳定


# ---------- 装配接线可见性 ----------

def test_app_exposes_acceleration_config(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, text_provider='mock', image_provider='mock',
        tts_provider='mock', video_provider='comfyui',
        video_attention='sageattn', video_blocks_to_swap=24, video_prefetch_blocks=1,
        video_text_cfg=1.0, video_audio_cfg=2.0))
    with TestClient(app) as c:
        active = c.get("/api/machine").json()["activeConfig"]
        accel = active["video_acceleration"]
        assert accel["attention"] == "sageattn"
        assert accel["blocks_to_swap"] == 24
        assert accel["prefetch_blocks"] == 1
        assert accel["text_cfg"] == 1.0
        assert accel["audio_cfg"] == 2.0