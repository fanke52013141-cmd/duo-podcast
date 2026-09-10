"""渲染域模型（01 §11.1）：RenderSegment 三区间。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .base import dc_config


class RenderSegment(BaseModel):
    model_config = dc_config()
    id: str  # #04（稳定，插入段不导致后续换种子）
    output_range: tuple[int, int] = (0, 0)  # 保留区间（不与其他段重复）
    render_range: tuple[int, int] = (0, 0)  # 实际生成区间（可含缓冲）
    context_range: Optional[dict] = None  # {range:[a,b], sourceAssetId}
    input_hash: str = ""
    effective_output_version: Optional[str] = None
