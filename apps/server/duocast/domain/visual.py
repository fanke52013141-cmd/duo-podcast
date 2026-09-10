"""视觉域模型（01 §11.1）：VisualVariant / ShotPlan / 矩形与变换。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .base import dc_config

from ..domain.audio import AssetRef


class Rect(BaseModel):
    model_config = dc_config()
    x: float = 0.0  # 规范化坐标 0..1
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0


class Transform(BaseModel):
    model_config = dc_config()
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    crop: Optional[Rect] = None
    pad: str = "none"  # none | letterbox | cover


class CameraMove(BaseModel):
    model_config = dc_config()
    kind: Literal["none", "push", "pull", "pan", "tilt"] = "none"
    strength: float = 0.0


class VisualVariant(BaseModel):
    model_config = dc_config()
    id: str  # V3
    master_image: AssetRef = Field(default_factory=AssetRef)
    aspect: Literal["landscape", "portrait"] = "landscape"
    model_input_transform: Transform = Field(default_factory=Transform)
    person_regions: dict = Field(default_factory=lambda: {"A": Rect(), "B": Rect()})
    image_api_config_ref: Optional[str] = None


class ShotPlan(BaseModel):
    model_config = dc_config()
    id: str  # S2
    preset: Literal["stable", "pushPull", "speakerCrop"] = "stable"
    ranges: list[dict] = Field(default_factory=list)  # {startSample, endSample, crop?, move?}
    manual_fixed: bool = False
