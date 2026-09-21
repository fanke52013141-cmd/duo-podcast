"""视觉业务编排（06 §3.2 visual_svc）：构图变体登记（01 §11.3）。

mock 阶段：结果仅占位元数据；真实实现由图片 API 下载并校验后登记 AssetRef。
"""

from __future__ import annotations

import uuid
from typing import Any

from ..domain.audio import AssetRef
from ..domain.project import Project
from ..domain.visual import CameraAsset, DialogueCameraGroup, VisualVariant
from ..storage.project_store import ProjectStore


def apply_visual_variant(store: ProjectStore, project: Project, result: dict[str, Any]) -> list[VisualVariant]:
    aspect = result.get("aspect", project.aspect)
    mode = result.get("mode", "twoShot")
    if mode == "overShoulder":
        return _apply_over_shoulder_camera(store, project, result, aspect)
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


def _asset(result: dict[str, Any]) -> AssetRef:
    return AssetRef(
        artifact_id=result.get("artifactId") or f"image-{uuid.uuid4().hex[:8]}",
        path=result.get("path", ""),
        file_hash=result.get("fileHash", ""),
    )


def _apply_over_shoulder_camera(store: ProjectStore, project: Project, result: dict[str, Any],
                                aspect: str) -> list[VisualVariant]:
    """将一次图片任务结果写入一组正反打机位，而非误建成独立同框底图。"""
    group_id = result.get("cameraGroupId") or f"CG-{uuid.uuid4().hex[:8]}"
    subject = result.get("subjectSpeaker")
    foreground = result.get("foregroundSpeaker")
    if subject not in {"A", "B"} or foreground not in {"A", "B"} or subject == foreground:
        raise ValueError("overShoulder camera requires distinct A/B subject and foreground speakers")
    camera_id = result.get("cameraAssetId") or f"CA-{subject}-{uuid.uuid4().hex[:6]}"
    asset = _asset(result)

    variants = [v.model_copy(deep=True) for v in project.visual_variants]
    target = next((v for v in variants if any(g.id == group_id for g in v.camera_groups)), None)
    if target is None:
        target = VisualVariant(
            id=f"V{len(variants) + 1:02d}", master_image=asset, aspect=aspect,
            image_api_config_ref=result.get("provider", "mock-image"), mode="overShoulder",
        )
        variants.append(target)
        group = DialogueCameraGroup(id=group_id, visual_variant_id=target.id, mode="overShoulder")
        target.camera_groups.append(group)
    else:
        group = next(g for g in target.camera_groups if g.id == group_id)

    camera = CameraAsset(
        id=camera_id, master_image=asset, mode="overShoulder",
        subject_speaker=subject, foreground_speaker=foreground,
    )
    existing = next((i for i, c in enumerate(group.camera_assets) if c.id == camera_id), None)
    if existing is None:
        group.camera_assets.append(camera)
    else:
        group.camera_assets[existing] = camera

    store.apply(project.id, {
        "visual_variants": [v.model_dump(by_alias=True) for v in variants],
        "approvals": [a for a in project.approvals if a.kind != "sample"],
    }, expected_revision=project.revision)
    return [target]
