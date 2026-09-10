"""工程域模型（01 §11.1 的 pydantic 落位：字段同名同义，camelCase alias 统一转换）。"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .audio import AudioTimeline, VoiceBinding
from .base import dc_config
from .visual import VisualVariant

StageId = Literal["create", "script", "voice", "visual", "render"]
Speaker = Literal["A", "B"]


class StageState(str, Enum):
    NOT_READY = "not_ready"
    READY = "ready"
    RUNNING = "running"
    PENDING_CONFIRM = "pending_confirm"
    DONE = "done"
    STALE = "stale"
    FAILED = "failed"


class ContentBrief(BaseModel):
    model_config = dc_config()
    core_question: str = ""
    key_points: list[str] = Field(default_factory=list)
    necessary_facts: list[str] = Field(default_factory=list)
    paragraph_goals: list[str] = Field(default_factory=list)
    speaker_duties: str = ""


class SourceAnchor(BaseModel):
    model_config = dc_config()
    ref: str = ""
    quote: str = ""


class Line(BaseModel):
    model_config = dc_config()
    id: str  # L019
    display_text: str = ""
    spoken_text: str = ""


class Turn(BaseModel):
    model_config = dc_config()
    id: str  # T07（稳定 ID，不随排序改变）
    speaker: Speaker
    intent: Literal["explain", "probe", "example", "challenge", "summarize", "other"] = "other"
    lines: list[Line] = Field(default_factory=list)
    tone: str = "自然"
    speed_ratio: float = 1.0
    source_anchors: list[SourceAnchor] = Field(default_factory=list)


class ScriptRevision(BaseModel):
    model_config = dc_config()
    id: str  # R8
    source_input: dict = Field(default_factory=dict)  # {kind: topic|article|script, content}
    content_brief: ContentBrief = Field(default_factory=ContentBrief)
    turns: list[Turn] = Field(default_factory=list)
    text_api_config_ref: str = ""


class Approval(BaseModel):
    model_config = dc_config()
    kind: Literal["script", "voice", "sample", "character", "composition", "shot"]
    input_revision_id: str = ""
    spec: dict = Field(default_factory=dict)
    decision: Literal["accepted", "rejected"] = "accepted"
    reason_target: Optional[StageId] = None
    note: str = ""
    at: str = ""


class Project(BaseModel):
    model_config = dc_config()
    schema_version: int = 1
    id: str
    title: str = "未命名节目"
    current_draft_revision: str = ""
    selected_output_version: Optional[str] = None
    aspect: Literal["landscape", "portrait"] = "landscape"
    script_revisions: list[ScriptRevision] = Field(default_factory=list)
    voice_bindings: list[VoiceBinding] = Field(default_factory=list)  # A/B 角色声音绑定（01 §11.2）
    audio_timeline: Optional[AudioTimeline] = None  # 阶段③产物（01 §11.2）
    visual_variants: list[VisualVariant] = Field(default_factory=list)  # 阶段④产物
    output_version: Optional[str] = None  # 阶段⑤产物版本（OUT-Rn-vN）
    stage_progress: dict[StageId, StageState] = Field(default_factory=dict)
    approvals: list[Approval] = Field(default_factory=list)
    revision: int = 0  # expectedRevision 冲突保护（01 §12.1）
    # 最后写入时间（ISO8601，UTC）。由 ProjectStore 在 apply()/create() 唯一入口打戳，
    # 供首页卡片「最后编辑 09-10」展示；旧工程文件缺该字段时保持空串，前端按缺省处理。
    updated_at: str = ""
