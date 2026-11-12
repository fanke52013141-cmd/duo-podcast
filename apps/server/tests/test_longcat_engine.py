"""LongCat-Video-Avatar-1.5 引擎接入（43 号方案批 B）CPU 回归。

验证：工作流模板结构与接线、镜头参数化纯函数、能力边界显式拒绝、
配置/运行时配置/装配接线。GPU 效果与显存实测留 RTX 5080（HANDOFF 边界）。
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from duocast.adapters.longcat_video import (
    ENGINE, LongCatVideoProvider, build_shot_workflow, generation_canvas,
    load_template, validate_longcat_request,
)
from duocast.core.config import Settings
from duocast.main import app, settings
from duocast.services.runtime_config import preflight_config, validate_config


# ---------- 模板结构与接线 ----------

REQUIRED_NODES = {
    "100": "WanVideoModelLoader", "101": "WanVideoBlockSwap", "102": "WanVideoLoraSelect",
    "103": "WanVideoVAELoader", "104": "LoadImage", "105": "ImageResizeKJv2",
    "106": "WanVideoEncode", "107": "WhisperModelLoader", "108": "VHS_LoadAudioUpload",
    "109": "VHS_LoadAudioUpload", "110": "LongCatAvatarWhisperEmbeds",
    "111": "LoadImage", "112": "LoadImage", "113": "ImageToMask", "114": "ImageToMask",
    "115": "BatchMasksNode", "116": "WanVideoLongCatAvatarExtendEmbeds",
    "117": "WanVideoTextEncodeCached", "118": "WanVideoSchedulerv2",
    "119": "WanVideoSamplerv2", "120": "WanVideoDecode", "121": "VHS_LoadAudioUpload",
    "122": "VHS_VideoCombine",
}


def test_template_nodes_present():
    wf = load_template()
    for node_id, class_type in REQUIRED_NODES.items():
        assert wf[node_id]["class_type"] == class_type, node_id


def test_template_links_resolve():
    wf = load_template()
    for node_id, node in wf.items():
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in wf, f"{node_id} 引用了不存在节点 {value[0]}"


def test_template_dual_audio_and_masks():
    wf = load_template()
    embeds = wf["110"]["inputs"]
    assert embeds["audio_1"] == ["108", 0] and embeds["audio_2"] == ["109", 0]
    assert embeds["multi_audio_type"] == "para"
    assert embeds["ref_target_masks"] == ["115", 0]
    assert wf["115"]["inputs"]["masks.mask0"] == ["113", 0]
    assert wf["115"]["inputs"]["masks.mask1"] == ["114", 0]


def test_template_longcat_specifics():
    wf = load_template()
    # bf16 强制（官方示例注记：LongCat 仅 bf16 基精度）；蒸馏调度器与少步。
    assert wf["100"]["inputs"]["base_precision"] == "bf16"
    assert wf["118"]["inputs"]["scheduler"] == "longcat_distill_euler"
    assert wf["118"]["inputs"]["steps"] <= 16
    # v1.5 Whisper 路径：ExtendEmbeds 接 Whisper embeds，参考 latent 锚定身份。
    assert wf["116"]["inputs"]["audio_embeds"] == ["110", 0]
    assert wf["116"]["inputs"]["prev_latents"] == ["106", 0]
    assert wf["116"]["inputs"]["ref_latent"] == ["106", 0]
    assert wf["119"]["inputs"]["scheduler"] == ["118", 0]
    assert wf["119"]["inputs"]["image_embeds"] == ["116", 0]


# ---------- 镜头参数化 ----------

def _build(**overrides):
    args = dict(image_name="duo.png", audio_a="t_a.wav", audio_b="t_b.wav",
                audio_mix="t_mix.wav", mask_a="m_a.png", mask_b="m_b.png",
                positive_prompt="podcast", seed=7, frame_count=129,
                width=832, height=480, filename_prefix="completion/t")
    args.update(overrides)
    return build_shot_workflow(load_template(), **args)


def test_build_parameterizes_all_inputs():
    wf = _build()
    assert wf["104"]["inputs"]["image"] == "duo.png"
    assert wf["108"]["inputs"]["audio"] == "t_a.wav"
    assert wf["109"]["inputs"]["audio"] == "t_b.wav"
    assert wf["121"]["inputs"]["audio"] == "t_mix.wav"
    assert wf["111"]["inputs"]["image"] == "m_a.png"
    assert wf["112"]["inputs"]["image"] == "m_b.png"
    assert wf["110"]["inputs"]["num_frames"] == 129
    assert wf["116"]["inputs"]["num_frames"] == 129
    assert wf["119"]["inputs"]["seed"] == 7
    assert wf["122"]["inputs"]["filename_prefix"] == "completion/t"
    assert wf["105"]["inputs"]["width"] == 832 and wf["105"]["inputs"]["height"] == 480
    assert wf["117"]["inputs"]["positive_prompt"] == "podcast"


def test_build_overrides_model_and_lora():
    wf = _build(model="LongCat/custom.gguf", lora="custom_lora.safetensors")
    assert wf["100"]["inputs"]["model"] == "LongCat/custom.gguf"
    assert wf["102"]["inputs"]["lora"] == "custom_lora.safetensors"


def test_build_does_not_mutate_template():
    template = load_template()
    _build(image_name="changed.png")
    assert template["104"]["inputs"]["image"] == "podcast_duo.png"


@pytest.mark.parametrize("resolution,canvas", [
    ("854x480", (832, 480)), ("1280x720", (1280, 720)), ("1920x1080", (1280, 720)),
])
def test_generation_canvas(resolution, canvas):
    assert generation_canvas(resolution) == canvas


# ---------- 能力边界：显式拒绝，不静默回落 ----------

TWO_SHOT = {"mode": "twoShot"}


def test_accepts_standard_two_shot():
    validate_longcat_request({"generationProfile": "accepted"}, TWO_SHOT)
    validate_longcat_request({}, TWO_SHOT)
    validate_longcat_request({"cacheOnly": True}, TWO_SHOT)


@pytest.mark.parametrize("req,visual,keyword", [
    ({"generationProfile": "accepted"}, {"mode": "overShoulder"}, "过肩"),
    ({"generationProfile": "dialogue"}, TWO_SHOT, "MultiTalk"),
    ({"generationProfile": "listener"}, TWO_SHOT, "MultiTalk"),
    ({"generationProfile": "reference"}, TWO_SHOT, "MultiTalk"),
    ({"generationProfile": "expertMotionReference"}, TWO_SHOT, "MultiTalk"),
    ({"reuseMotionArtifactId": "VID-x"}, TWO_SHOT, "局部重做"),
    ({"redoFromSec": 10}, TWO_SHOT, "局部重做"),
])
def test_rejects_out_of_scope(req, visual, keyword):
    with pytest.raises(RuntimeError, match=keyword):
        validate_longcat_request(req, visual)


def test_capabilities_declare_boundary():
    caps = LongCatVideoProvider.capabilities
    assert caps["engine"] == ENGINE
    assert caps["overShoulder"] is False
    assert caps["generationProfiles"] == ["accepted"]
    assert caps["simulated"] is False


# ---------- 配置与装配 ----------

def test_settings_env(monkeypatch):
    monkeypatch.setenv("DUOCAST_VIDEO_ENGINE", "longcat")
    monkeypatch.setenv("DUOCAST_LONGCAT_MODEL", "LongCat/x.gguf")
    cfg = Settings.from_env()
    assert cfg.video_engine == "longcat"
    assert cfg.longcat_model == "LongCat/x.gguf"
    monkeypatch.delenv("DUOCAST_VIDEO_ENGINE")
    assert Settings.from_env().video_engine == "multitalk"


def test_runtime_config_accepts_engine_fields():
    validate_config({"video_engine": "longcat", "longcat_model": "m.gguf", "longcat_lora": "l.safetensors"})
    with pytest.raises(ValueError):
        validate_config({"video_engine": "humo"})
    report = preflight_config({"video_provider": "comfyui", "video_engine": "longcat", "video_url": "http://127.0.0.1:8191"})
    assert any("LongCat" in w for w in report["warnings"])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, text_provider='mock', image_provider='mock',
        tts_provider='mock', video_provider='comfyui', video_engine='longcat'))
    with TestClient(app) as c:
        yield c


def test_app_wires_longcat_engine(client):
    caps = client.get("/api/providers/capabilities").json()
    assert caps["video"]["capabilities"]["engine"] == ENGINE
    assert caps["video"]["mode"] == "live"
    config = client.get("/api/machine").json()
    assert config["activeConfig"]["video_engine"] == "longcat"


def test_app_default_engine_multitalk(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, text_provider='mock', image_provider='mock',
        tts_provider='mock', video_provider='comfyui', video_engine='multitalk'))
    with TestClient(app) as c:
        caps = c.get("/api/providers/capabilities").json()
        assert caps["video"]["capabilities"].get("engine") is None  # MultiTalk 保持原能力契约
        assert caps["video"]["capabilities"]["overShoulder"] is True