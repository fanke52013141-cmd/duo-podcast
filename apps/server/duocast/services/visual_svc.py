"""视觉业务编排（06 §3.2 visual_svc）：构图变体登记（01 §11.3）。

mock 阶段：结果仅占位元数据；真实实现由图片 API 下载并校验后登记 AssetRef。
"""

from __future__ import annotations

import uuid
from typing import Any

from ..domain.audio import AssetRef
from ..domain.project import Project
from ..domain.visual import VisualVariant
from ..storage.project_store import ProjectStore


def apply_visual_variant(store: ProjectStore, project: Project, result: dict[str, Any]) -> list[VisualVariant]:
    aspect = result.get("aspect", project.aspect)
    # 真实图片提供方（ToAPIs）返回 artifactId/path/fileHash；mock 阶段保持占位
    variant = VisualVariant(
        id=f"V{len(project.visual_variants) + 1:02d}",
        master_image=AssetRef(
            artifact_id=result.get("artifactId") or f"image-{uuid.uuid4().hex[:8]}",
            path=result.get("path", ""),
            file_hash=result.get("fileHash", ""),
        ),
        aspect=aspect,
        image_api_config_ref=result.get("provider", "mock-image"),
    )
    variants = [v.model_dump(by_alias=True) for v in project.visual_variants] + [variant.model_dump(by_alias=True)]
    store.apply(project.id, {"visual_variants": variants,
                             "approvals": [a for a in project.approvals if a.kind != 'sample']},
                expected_revision=project.revision)
    return [variant]
