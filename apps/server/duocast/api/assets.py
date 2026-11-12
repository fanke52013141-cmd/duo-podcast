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
    root = request.app.state.artifacts.root.parent / _UPLOAD_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


@router.post("/projects/{project_id}/characters/{speaker}")
async def bind_character(project_id: str, speaker: str, payload: dict, request: Request):
    if speaker not in {"A", "B"}:
        raise HTTPException(422, "角色位置须为 A/B")
    store = request.app.state.project_store
    project = store.load(project_id)
    character = _store(request).get(payload.get("characterId"))
    if project is None or character is None:
        raise HTTPException(404, "工程或角色不存在")
    snapshot = dict(character)
    image_path = snapshot.get("imagePath", "")
    image_ref = None
    if image_path.startswith("/api/assets/images/"):
        file = _images_root(request) / Path(image_path).name
        if file.is_file():
            assets = request.app.state.artifacts
            aid = "IMG-CHAR-" + uuid.uuid4().hex
            entry = assets.register(aid, "image", str(file), assets._hash_file(str(file)))
            snapshot["imageAssetId"] = entry["artifactId"]
            from ..domain.motion_pack import MotionMediaRef
            image_ref = MotionMediaRef(artifact_id=entry["artifactId"], file_hash=entry["fileHash"])
    bindings = [b.model_dump(by_alias=True) for b in project.voice_bindings if b.character_id != speaker]
    if snapshot.get("localRefAudio"):
        bindings.append({"id": f"VB-{speaker}", "characterId": speaker, "providerProfileId": "comfyui-indextts", "localRefAudio": snapshot["localRefAudio"]})
    extra = {"character_bindings": {**project.character_bindings, speaker: snapshot}, "voice_bindings": bindings}
    if project.generation_mode == "fast":
        # P1-C（42 号方案）：派生新动作包替换角色参考图，原包与其他工程不受影响
        if image_ref is None:
            raise HTTPException(422, "该角色没有可用的参考图，无法更换快速模式人物")
        from ..services.motion_swap import apply_image_swap
        try:
            result = apply_image_swap(
                store, request.app.state.motion_pack_store, project, image_ref=image_ref, role=speaker,
                character_meta={"characterId": character.get("id"), "name": character.get("name", "")},
                expected_revision=payload.get("expectedRevision", project.revision), extra_patch=extra)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        store.flush()
        return {**result["project"].model_dump(by_alias=True),
                "motionSwap": {k: result[k] for k in ("motionPack", "derivedFrom", "ready", "missing", "notice")}}
    saved = store.apply(project_id, extra, payload.get("expectedRevision", project.revision))
    store.flush()
    return saved.model_dump(by_alias=True)


@router.post("/characters/{character_id}/voice")
async def bind_character_voice(character_id: str, payload: dict, request: Request):
    assets = request.app.state.artifacts
    entry = assets.by_id(payload.get("artifactId"))
    if not entry or entry["kind"] != "audio" or entry.get("paramsSnapshot", {}).get("source") != "voiceReference":
        raise HTTPException(422, "请选择已上传的参考音频")
    updated = _store(request).update(character_id, {"localRefAudio": {"artifactId": entry["artifactId"], "path": entry["path"], "fileHash": entry["fileHash"]}})
    if not updated:
        raise HTTPException(404, "角色不存在")
    return updated


def _validate_regions(regions) -> None:
    """A/B 人物区域校验（归一化 0..1）；区域标注与带区域导入共用同一口径。"""
    import math
    if not isinstance(regions, dict) or set(regions) != {"A", "B"}:
        raise HTTPException(422, "需要 A/B 两个人物区域")
    for region in regions.values():
        if not isinstance(region, dict) or set(region) != {"x", "y", "width", "height"} or any(type(n) not in (int, float) or not math.isfinite(n) for n in region.values()):
            raise HTTPException(422, "区域坐标无效")
        x, y, w, h = (region[k] for k in ("x", "y", "width", "height"))
        if min(x, y) < 0 or min(w, h) <= 0 or x+w > 1.000001 or y+h > 1.000001:
            raise HTTPException(422, "人物框必须位于画布内")


@router.post("/projects/{project_id}/visual/regions")
async def update_regions(project_id: str, payload: dict, request: Request):
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "工程不存在")
    regions = payload.get("regions", {})
    _validate_regions(regions)
    # 显式近景虚线框（可选）：{}=恢复自动半身推导；提供时须 A/B 齐全且同口径校验。
    closeups = payload.get("closeupRegions")
    if closeups is not None and closeups != {}:
        _validate_regions(closeups)
    variants = [v.model_copy(deep=True) for v in project.visual_variants]
    target = next((v for v in variants if v.id == payload.get("variantId")), None)
    if target is None:
        raise HTTPException(404, "画面版本不存在")
    target.person_regions = regions
    if closeups is not None:
        target.closeup_regions = closeups
    saved = store.apply(project_id, {"visual_variants": variants}, payload.get("expectedRevision", project.revision))
    store.flush()
    return saved.model_dump(by_alias=True)


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


@router.get("/scenes")
def list_scenes(request: Request):
    return [{"id": e["artifactId"], "name": e.get("paramsSnapshot", {}).get("name", "场景"), "fileHash": e["fileHash"]}
            for e in request.app.state.artifacts._manifest()["artifacts"]
            if e["kind"] == "image" and e.get("paramsSnapshot", {}).get("source") == "scene"]


@router.post("/scenes")
async def upload_scene(request: Request):
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "请选择 PNG/JPEG 场景图片")
    data = await upload.read(10*1024*1024+1)
    if len(data) > 10*1024*1024:
        raise HTTPException(422, "场景图片超过 10MB")
    saved = save_uploaded_image(request, data, getattr(upload, "filename", ""))
    assets = request.app.state.artifacts
    entry = assets.register("IMG-SCENE-"+uuid.uuid4().hex, "image", saved["path"], saved["fileHash"],
                            {"source": "scene", "name": str(form.get("name") or getattr(upload, "filename", "场景"))[:100]})
    return {"id": entry["artifactId"], "name": entry["paramsSnapshot"]["name"]}


@router.post("/projects/{project_id}/visual/use-scene")
def use_scene(project_id: str, payload: dict, request: Request):
    store = request.app.state.project_store
    project = store.load(project_id)
    entry = request.app.state.artifacts.by_id(payload.get("sceneId"))
    if project is None or not entry or entry["kind"] != "image" or entry.get("paramsSnapshot", {}).get("source") != "scene":
        raise HTTPException(404, "工程或场景不存在")
    if payload.get("expectedRevision") != project.revision:
        raise HTTPException(409, "工程已变化，请刷新后选择场景")
    assets = request.app.state.artifacts
    if not Path(entry["path"]).is_file() or assets._hash_file(entry["path"]) != entry["fileHash"]:
        raise HTTPException(409, "场景文件缺失或已改变，请重新上传")
    if project.generation_mode == "fast":
        # P1-C（42 号方案）：派生新动作包替换同框机位底图，原包与其他工程不受影响
        from ..domain.motion_pack import MotionMediaRef
        from ..services.motion_swap import apply_image_swap
        image_ref = MotionMediaRef(artifact_id=entry["artifactId"], file_hash=entry["fileHash"])
        try:
            result = apply_image_swap(store, request.app.state.motion_pack_store, project,
                                      image_ref=image_ref, expected_revision=project.revision)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        store.flush()
        return {"motionSwap": {k: result[k] for k in ("motionPack", "derivedFrom", "ready", "missing")},
                "notice": result["notice"]}
    variants = apply_visual_variant(store, project, {"aspect": project.aspect, "mode": "twoShot", "artifactId": entry["artifactId"],
                                                   "path": entry["path"], "fileHash": entry["fileHash"], "provider": "scene-library"})
    current = store.load(project_id)
    store.apply(project_id, {"selected_visual_variant_id": variants[0].id}, current.revision)
    store.flush()
    return {"variantId": variants[0].id, "notice": "已用于本期底图，请确认画面包含正确人物并校准区域；空场景不能直接驱动人物"}


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
    if set(payload) - {"name", "role", "desc", "traits", "speaker"}:
        raise HTTPException(422, "图片和音频请使用上传接口绑定")
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
    if project.generation_mode == "fast":
        # P1-C（42 号方案）：快速模式上传底图 → 派生新动作包替换同框机位底图
        if mode != "twoShot":
            raise HTTPException(422, "快速模式过肩机位底图请在动作包管理页更换")
        from ..domain.motion_pack import MotionMediaRef
        from ..services.motion_swap import apply_image_swap
        image_ref = MotionMediaRef(artifact_id=entry["artifactId"], file_hash=entry["fileHash"])
        try:
            result = apply_image_swap(store, request.app.state.motion_pack_store, project,
                                      image_ref=image_ref, expected_revision=project.revision)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        store.flush()
        return {"webPath": saved["webPath"],
                "motionSwap": {k: result[k] for k in ("motionPack", "derivedFrom", "ready", "missing")},
                "notice": result["notice"]}
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


@router.post("/projects/{project_id}/visual/import-variant")
async def import_variant(project_id: str, request: Request) -> dict:
    """批 2（42 号方案）：图片 + 区域 JSON 一次性导入为视觉变体。

    与 upload-master 同一落点（apply_visual_variant，provider=upload），
    额外支持随上传直接给定 A/B 人物区域（归一化坐标），省去二次标注。
    meta 为 JSON 字符串：{mode, aspect, personRegions, subjectSpeaker,
    foregroundSpeaker, cameraGroupId, cameraAssetId, provenance}。
    """
    import json as _json
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    form = await request.form()
    meta = {}
    if form.get("meta"):
        try:
            meta = _json.loads(str(form.get("meta")))
            if not isinstance(meta, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "UPLOAD_SCHEMA: meta 不是合法 JSON 对象")
    payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else meta
    mode = str(payload.get("mode") or "twoShot")
    if mode not in {"twoShot", "overShoulder"}:
        raise HTTPException(422, "未知机位模式")
    camera_fields = {}
    if mode == "overShoulder":
        subject, foreground = payload.get("subjectSpeaker"), payload.get("foregroundSpeaker")
        if subject not in {"A", "B"} or foreground not in {"A", "B"} or subject == foreground:
            raise HTTPException(422, "请指定不同的主体与前景角色")
        camera_fields = {k: str(payload.get(k) or "") for k in ("cameraGroupId", "cameraAssetId", "subjectSpeaker", "foregroundSpeaker")}
        if not camera_fields["cameraGroupId"] or not camera_fields["cameraAssetId"]:
            raise HTTPException(422, "请指定机位组和机位标识")
    regions = payload.get("personRegions")
    if regions is not None:
        _validate_regions(regions)
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "缺少上传文件字段 file")
    data = await upload.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(422, "图片超过 10MB 限制")
    saved = save_uploaded_image(request, data, getattr(upload, "filename", ""))
    aspect = str(payload.get("aspect") or project.aspect)
    if aspect not in ("landscape", "portrait"):
        aspect = project.aspect
    provenance = {"source": "external-upload"}
    if isinstance(payload.get("provenance"), dict):
        provenance.update({k: str(v)[:200] for k, v in payload["provenance"].items()})
    artifact_id = f"IMG-up-{saved['fileHash'][:12]}"
    artifacts = request.app.state.artifacts
    entry = artifacts.register(artifact_id, "image", saved["path"], saved["fileHash"],
                               params_snapshot={"source": "upload", "provenance": provenance})
    variants = apply_visual_variant(store, project, {
        "aspect": aspect, "mode": mode, **camera_fields,
        "artifactId": artifact_id, "path": saved["path"],
        "fileHash": entry.get("fileHash", saved["fileHash"]), "provider": "upload",
    })
    variant_id = variants[0].id
    if regions is not None and mode == "twoShot":
        current = store.load(project_id)
        updated_variants = [v.model_dump(by_alias=True) for v in current.visual_variants]
        target = next(v for v in updated_variants if v["id"] == variant_id)
        target["personRegions"] = regions
        store.apply(project_id, {"visual_variants": updated_variants}, current.revision)
    store.flush()
    return {"variantId": variant_id, "webPath": saved["webPath"],
            "regionsApplied": regions is not None and mode == "twoShot",
            "status": "pending-selection"}
