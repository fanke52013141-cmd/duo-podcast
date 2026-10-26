"""Bounded manual face coordinates, tied to one video/role and revision."""
import asyncio
import hashlib
import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, StrictInt, ValidationError

from ..domain.motion_pack import MotionModel, MotionMediaRef, MotionPackDraft
from .motion_packs import _call, _required
from ..storage.motion_packs import MotionPackConflict, InvalidMotionPack

router = APIRouter(prefix="/api/motion-packs", tags=["motion-coordinates"])
MAX_BYTES = 8 * 1024 * 1024
MAX_FRAMES = 36000


class CoordinateRequest(MotionModel):
    expected_revision: int = Field(strict=True, ge=1)
    boxes: list[list[StrictInt] | None] = Field(min_length=1, max_length=MAX_FRAMES)


def _save(request, pack_id, clip_id, payload):
    store, artifacts = request.app.state.motion_pack_store, request.app.state.artifacts
    pack = _required(store, pack_id)
    if pack.revision != payload.expected_revision:
        raise MotionPackConflict("MOTION_PACK_REVISION_CONFLICT")
    clip = next((c for c in pack.clips if c.id == clip_id), None)
    if clip is None:
        raise KeyError(clip_id)
    if clip.frame_count > MAX_FRAMES or len(payload.boxes) != clip.frame_count:
        raise InvalidMotionPack("逐帧坐标数必须等于源视频帧数，且不超过36000帧")
    store._verify_ref(clip.media, "video")
    for index, box in enumerate(payload.boxes):
        if box is None:
            if clip.usable_start_frame <= index < clip.usable_end_frame:
                raise InvalidMotionPack("可用区间不能缺少人脸框")
            continue
        if len(box) != 4 or not (0 <= box[0] < box[2] <= clip.native_width and 0 <= box[1] < box[3] <= clip.native_height):
            raise InvalidMotionPack("人脸框必须为原生画面内的四个整数像素坐标")
    data = json.dumps({"videoFileHash": clip.media.file_hash, "role": clip.role, "boxes": payload.boxes}, separators=(",", ":")).encode()
    if len(data) > MAX_BYTES:
        raise InvalidMotionPack("坐标文件超过8MB")
    digest = hashlib.sha256(data).hexdigest()
    artifact_id = "MOTION-COORD-" + uuid.uuid4().hex
    folder = artifacts.root / "motion-coordinates"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (artifact_id + ".json")
    # Immutable artifact publication may survive a later revision conflict; it
    # remains unbound and cannot change a concurrently edited pack.
    with target.open("xb") as output:
        output.write(data)
    artifacts.register(artifact_id, "mixed", str(target), digest,
                       {"source": "manualCoordinates", "packId": pack_id, "clipId": clip_id,
                        "visibilityVerified": False, "automaticDetection": False}, file_hash=digest)
    draft = MotionPackDraft.model_validate(pack.model_dump(include=set(MotionPackDraft.model_fields)))
    selected = next(c for c in draft.clips if c.id == clip_id)
    selected.face_coordinates = MotionMediaRef(artifact_id=artifact_id, file_hash=digest)
    return store.update(pack_id, draft, payload.expected_revision)


@router.post("/{pack_id}/clips/{clip_id}/coordinates")
async def save_coordinates(pack_id: str, clip_id: str, request: Request):
    data = bytearray()
    async for block in request.stream():
        data.extend(block)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "坐标请求不得超过8MB")
    try:
        payload = CoordinateRequest.model_validate_json(bytes(data))
    except ValidationError as exc:
        raise HTTPException(422, "坐标请求字段或整数坐标不合法") from exc
    if not hasattr(request.app.state, "motion_coordinate_slots"):
        request.app.state.motion_coordinate_slots = asyncio.Semaphore(2)
    async with request.app.state.motion_coordinate_slots:
        worker = asyncio.create_task(asyncio.to_thread(_call, _save, request, pack_id, clip_id, payload))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
            raise
