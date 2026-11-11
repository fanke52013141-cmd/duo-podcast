"""P1-C（42 号方案）：快速模式换角色/底图端点——派生新动作包再改。

POST /api/projects/{id}/motion/swap-image
  JSON: {imageArtifactId, role?|cameraId?, characterId?, expectedRevision}
  或 multipart: file（图片）+ meta（JSON：role/cameraId/expectedRevision）

仅快速模式且已绑动作包的工程可用；派生新包、重绑定、撤销样片确认，
新包不就绪 → 渲染门槛拦截 → 引导重新准备。原包与其他工程不受影响。
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Request

from ..domain.motion_pack import MotionMediaRef
from ..services.motion_swap import apply_image_swap

router = APIRouter(prefix="/api/projects/{project_id}/motion", tags=["motion-swap"])


def _image_ref(request: Request, artifact_id: str) -> MotionMediaRef:
    artifacts = request.app.state.artifacts
    entry = artifacts.by_id(artifact_id)
    if not entry or entry.get("kind") != "image":
        raise HTTPException(422, "请选择已登记的图片资产")
    from pathlib import Path
    if not Path(entry["path"]).is_file() or artifacts._hash_file(entry["path"]) != entry.get("fileHash"):
        raise HTTPException(409, "图片文件缺失或已改变，请重新上传")
    return MotionMediaRef(artifact_id=entry["artifactId"], file_hash=entry["fileHash"])


@router.post("/swap-image")
async def swap_image(project_id: str, request: Request) -> dict:
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "工程不存在")
    if project.generation_mode != "fast":
        raise HTTPException(422, "换角色/底图的动作包派生仅用于快速模式；原有模式请直接更换角色/底图")
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        meta = {}
        if form.get("meta"):
            try:
                meta = json.loads(str(form.get("meta")))
            except ValueError:
                raise HTTPException(422, "meta 不是合法 JSON")
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(422, "缺少上传文件字段 file")
        data = await upload.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(422, "图片超过 10MB 限制")
        from .assets import save_uploaded_image
        saved = save_uploaded_image(request, data, getattr(upload, "filename", ""))
        artifacts = request.app.state.artifacts
        artifact_id = f"IMG-SWAP-{uuid.uuid4().hex[:12]}"
        entry = artifacts.register(artifact_id, "image", saved["path"], artifacts._hash_file(saved["path"]),
                                   {"source": "motionSwap"})
        image_ref = MotionMediaRef(artifact_id=entry["artifactId"], file_hash=entry["fileHash"])
        payload = meta
    else:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(422, "请求体必须是 JSON 对象")
        image_ref = _image_ref(request, payload.get("imageArtifactId", ""))
    role = payload.get("role")
    camera_id = payload.get("cameraId")
    if role is not None and role not in ("A", "B"):
        raise HTTPException(422, "role 必须是 A 或 B")
    if role is not None and camera_id is not None:
        raise HTTPException(422, "role 与 cameraId 不能同时指定")
    character_meta = None
    if role is not None and payload.get("characterId"):
        character = request.app.state.character_store.get(payload["characterId"])
        if character:
            character_meta = {"characterId": character.get("id"), "name": character.get("name", "")}
    try:
        result = apply_image_swap(
            store, request.app.state.motion_pack_store, project,
            image_ref=image_ref, role=role, camera_id=camera_id,
            character_meta=character_meta, expected_revision=payload.get("expectedRevision"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    result.pop("project", None)
    return result