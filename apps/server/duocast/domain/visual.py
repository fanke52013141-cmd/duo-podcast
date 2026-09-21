"""视觉域模型（01 §11.1）：VisualVariant / ShotPlan / 矩形与变换。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .base import dc_config

from ..domain.audio import AssetRef


VisualMode = Literal["twoShot", "overShoulder"]
SpeakerId = Literal["A", "B"]


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


class CameraAsset(BaseModel):
    """一个可用于对谈剪辑的固定机位。

    ``overShoulder`` 机位只让 ``subject_speaker`` 的脸作为清晰、可驱动主体；
    ``foreground_speaker`` 仅表示前景的肩背人物，可选的遮罩用于后续分层合成。
    ``twoShot`` 也可使用本模型，方便同一镜头规划接口同时处理两种模式。
    """

    model_config = dc_config()
    id: str
    master_image: AssetRef = Field(default_factory=AssetRef)
    mode: VisualMode = "twoShot"
    subject_speaker: Optional[SpeakerId] = None
    foreground_speaker: Optional[SpeakerId] = None
    subject_region: Rect = Field(default_factory=Rect)
    foreground_mask: Optional[AssetRef] = None


class DialogueCameraGroup(BaseModel):
    """一组共享角色、场景参考的对谈机位资产。"""

    model_config = dc_config()
    id: str
    visual_variant_id: Optional[str] = None
    mode: VisualMode = "twoShot"
    character_reference_images: dict[str, AssetRef] = Field(default_factory=dict)
    scene_reference_image: Optional[AssetRef] = None
    camera_assets: list[CameraAsset] = Field(default_factory=list)


class ShotSegment(BaseModel):
    """按权威母轨样本位置描述的一个可切换镜头片段。"""

    model_config = dc_config()
    start_sample: int = 0
    end_sample: int = 0
    camera_asset_id: Optional[str] = None
    speaker: Optional[SpeakerId] = None
    manual_fixed: bool = False


class VisualVariant(BaseModel):
    model_config = dc_config()
    id: str  # V3
    master_image: AssetRef = Field(default_factory=AssetRef)
    aspect: Literal["landscape", "portrait"] = "landscape"
    model_input_transform: Transform = Field(default_factory=Transform)
    person_regions: dict = Field(default_factory=lambda: {"A": Rect(), "B": Rect()})
    image_api_config_ref: Optional[str] = None
    # 旧工程未提供该字段时仍是同框模式，继续使用 master_image/person_regions。
    mode: VisualMode = "twoShot"
    camera_groups: list[DialogueCameraGroup] = Field(default_factory=list)


class ShotPlan(BaseModel):
    model_config = dc_config()
    id: str  # S2
    preset: Literal["stable", "pushPull", "speakerCrop"] = "stable"
    ranges: list[dict] = Field(default_factory=list)  # {startSample, endSample, crop?, move?}
    manual_fixed: bool = False
    # 新字段与旧 ranges 并存：ranges 继续服务原同框裁切，segments 服务真实机位切换。
    mode: VisualMode = "twoShot"
    camera_group_id: Optional[str] = None
    segments: list[ShotSegment] = Field(default_factory=list)
