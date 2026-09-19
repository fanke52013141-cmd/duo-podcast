"""声音域模型（01 §11.1）：VoiceBinding / SynthesisUnit / AudioTimeline 与资产引用。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .base import dc_config


class AssetRef(BaseModel):
    model_config = dc_config()
    artifact_id: str = ""
    path: str = ""
    file_hash: str = ""


class VoiceBinding(BaseModel):
    model_config = dc_config()
    id: str
    character_id: str
    provider_profile_id: str
    model_id: str = ""
    local_ref_audio: Optional[AssetRef] = None
    minimax_voice_id: Optional[str] = None
    audition_state: Literal["none", "passed", "stale"] = "none"


class SynthesisUnit(BaseModel):
    model_config = dc_config()
    id: str  # U12
    turn_id: str
    line_ids: list[str] = Field(default_factory=list)
    provider_profile_id: str = ""
    model_id: str = ""
    voice_binding_id: str = ""
    emotion: dict = Field(default_factory=lambda: {"label": "自然"})
    speed_ratio: float = 1.0
    pronunciation_revision: str = ""
    adopted_audio_asset_id: Optional[str] = None
    candidate_audio_asset_ids: list[str] = Field(default_factory=list)
    sample_count: int = 0  # 引擎返回的权威时长（样本数，48kHz 母轨口径）


class AudioTimeline(BaseModel):
    model_config = dc_config()
    revision_id: str = ""
    sample_rate: int = 48000
    sample_count: int = 0  # N：权威总长（整数样本）
    units: list[SynthesisUnit] = Field(default_factory=list)
    unit_offsets: dict[str, int] = Field(default_factory=dict)  # 整数样本偏移
    transition_gap_ms: list[int] = Field(default_factory=list)
    lead_in_ms: int = 0
    tail_out_ms: int = 0
    annotation_version: str = ""
    # 关口 A：真实引擎产出的整期拼接母轨（PCM16/48k mono）资产 id；mock 模式恒为 None
    master_audio_asset_id: Optional[str] = None
