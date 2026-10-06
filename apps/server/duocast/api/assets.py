"""资产库接口（01 §1.5）：全局角色 CRUD + 图片上传 + 静态图片读取。

角色图片 / 通用上传图片统一落 storage/uploads/images/，经 GET /api/assets/images/{name}
读取（文件名白名单校验，防目录穿越）。上传校验 PNG/JPEG magic bytes，不信任扩展名。
"""

from __future__ import annotations

import re
import uuid
import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..core.config import settings
from ..services.visual_svc import apply_visual_variant

router = APIRouter(prefix="/api", tags=["assets"])


@router.post("/uploads/audio")
async def upload_audio(request: Request) -> dict:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "请上传参考音频")
    data = await upload.read(20*1024*1024+1)
    if not data or len(data) > 20*1024*1024:
        raise HTTPException(422, "参考音频必须为 0–20MB")
    store = request.app.state.artifacts
    root = store.root / "audio"
    root.mkdir(parents=True, exist_ok=True)
    asset_id = "AUD-REF-" + uuid.uuid4().hex
    source, target = root / f"{asset_id}.source", root / f"{asset_id}.wav"
    source.write_bytes(data)
    try:
        process = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-y", "-i", str(source),
            "-t", "30", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(target),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, error = await process.communicate()
        if process.returncode:
            raise HTTPException(422, "无法读取参考音频：" + error.decode("utf-8", "replace")[-300:])
        import wave
        with wave.open(str(target)) as reader:
            if reader.getnframes() < reader.getframerate():
                raise HTTPException(422, "参考音频至少需要 1 秒")
        entry = store.register(asset_id, "audio", str(target), store._hash_file(str(target)),
                               {"source": "voiceReference"})
        return {"artifactId": asset_id, "path": str(target), "fileHash": entry["fileHash"]}
    finally:
        source.unlink(missing_ok=True)

_UPLOAD_SUBDIR = Path("uploads") / "images"
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _images_root(request: Request) -> Path:
    root = settings.storage_root / _UPLOAD_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_uploaded_image(request: Request, data: bytes, filename: str) -> dict:
    """校验并保存上传图片，返回 {path(绝对), webPath(URL), fileHash, ext}。

    供角色图片与阶段④底图上传共用；非 PNG/JPEG 一律 422。
    """
    is_png = data[:8] == _PNG_MAGIC
    is_jpeg = data[:3] == _JPEG_MAGIC
    if not (is_png or is_jpeg):
        raise HTTPException(422, "仅支持 PNG / JPEG 图片")
    ext = ".png" if is_png else ".jpg"
    import hashlib

    file_hash = hashlib.sha256(data).hexdigest()[:16]
    name = f"{uuid.uuid4().hex[:12]}-{file_hash}{ext}"
    target = _images_root(request) / name
    target.write_bytes(data)
    del filename  # 原文件名仅作日志语义，落盘名统一受控
    return {"path": str(target), "webPath": f"/api/assets/images/{name}", "fileHash": file_hash}


def _store(request: Request):
    return request.app.state.character_store


@router.get("/characters")
async def list_characters(request: Request) -> list[dict]:
    return _store(request).list()


@router.post("/characters")
async def create_character(payload: dict, request: Request) -> dict:
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(422, "角色名称不能为空")
    return _store(request).create(
        name=name,
        speaker=str(payload.get("speaker", "A")),
        role=str(payload.get("role", "主持")),
        desc=str(payload.get("desc", "")),
        traits=str(payload.get("traits", "")),
    )


@router.patch("/characters/{character_id}")
async def update_character(character_id: str, payload: dict, request: Request) -> dict:
    updated = _store(request).update(character_id, payload)
    if updated is None:
        raise HTTPException(404, "character not found")
    return updated


@router.post("/characters/{character_id}/image")
async def upload_character_image(character_id: str, request: Request) -> dict:
    store = _store(request)
    if store.get(character_id) is None:
        raise HTTPException(404, "character not found")
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "缺少上传文件字段 file")
    data = await upload.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(422, "图片超过 10MB 限制")
    saved = save_uploaded_image(request, data, getattr(upload, "filename", ""))
    web_path = saved["webPath"]
    updated = store.update(character_id, {"imagePath": web_path})
    return updated or {}


@router.post("/uploads/image")
async def upload_image(request: Request) -> dict:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "缺少上传文件字段 file")
    data = await upload.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(422, "图片超过 10MB 限制")
    return save_uploaded_image(request, data, getattr(upload, "filename", ""))


@router.get("/assets/images/{name}")
async def read_image(name: str, request: Request) -> FileResponse:
    if not _NAME_RE.match(name) or ".." in name:
        raise HTTPException(422, "非法文件名")
    target = _images_root(request) / name
    if not target.exists():
        raise HTTPException(404, "image not found")
    return FileResponse(target)


@router.post("/projects/{project_id}/visual/upload-master")
async def upload_master_image(project_id: str, request: Request) -> dict:
    """阶段④底图上传（01 §6.3「图片 API 生成或上传」）：上传图片 → 登记为主图变体。

    与生成同框底图同一落点（apply_visual_variant），imageApiConfigRef=upload；
    默认人物区域 A 左 / B 右半幅，后续可在人物区域标注中调整。
    """
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    form = await request.form()
    mode = str(form.get("mode") or "twoShot")
    camera_fields = {}
    if mode not in {"twoShot", "overShoulder"}:
        raise HTTPException(422, "未知机位模式")
    if mode == "overShoulder":
        subject, foreground = form.get("subjectSpeaker"), form.get("foregroundSpeaker")
        if subject not in {"A", "B"} or foreground not in {"A", "B"} or subject == foreground:
            raise HTTPException(422, "请指定不同的主体与前景角色")
        camera_fields = {k: str(form.get(k) or "") for k in ("cameraGroupId", "cameraAssetId", "subjectSpeaker", "foregroundSpeaker")}
        if not camera_fields["cameraGroupId"] or not camera_fields["cameraAssetId"]:
            raise HTTPException(422, "请指定机位组和机位标识")
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "缺少上传文件字段 file")
    data = await upload.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(422, "图片超过 10MB 限制")
    saved = save_uploaded_image(request, data, getattr(upload, "filename", ""))
    aspect = str(form.get("aspect") or project.aspect)
    if aspect not in ("landscape", "portrait"):
        aspect = project.aspect
    import hashlib

    artifact_id = f"IMG-up-{saved['fileHash'][:12]}"
    artifacts = request.app.state.artifacts
    entry = artifacts.register(artifact_id, "image", saved["path"], saved["fileHash"],
                               params_snapshot={"source": "upload"})
    variants = apply_visual_variant(store, project, {
        "aspect": aspect,
        "mode": mode,
        **camera_fields,
        "artifactId": artifact_id,
        "path": saved["path"],
        "fileHash": entry.get("fileHash", saved["fileHash"]),
        "provider": "upload",
    })
    return {"variantId": variants[0].id, "webPath": saved["webPath"]}
