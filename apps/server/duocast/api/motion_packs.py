"""Artifact-referenced motion packs. Detector conclusions are server-owned."""
import asyncio
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, StrictBool

from ..domain.motion_pack import MotionModel, MotionPackDraft, MotionRegion, clip_fingerprint
from ..services.motion_quality import inspect_motion_clip
from ..storage.motion_packs import InvalidMotionPack, MotionPackConflict

router = APIRouter(prefix="/api/motion-packs", tags=["motion-packs"])


class UpdatePack(MotionModel):
    expected_revision: int = Field(ge=1, strict=True)
    draft: MotionPackDraft


class ReviewClip(MotionModel):
    expected_revision: int = Field(ge=1, strict=True)
    accepted: StrictBool
    reason: str = Field(default="", max_length=2000)


class InspectClip(MotionModel):
    expected_revision: int = Field(ge=1, strict=True)
    body_regions: list[MotionRegion] = Field(min_length=1, max_length=8)
    exclusion_regions: list[MotionRegion] = Field(min_length=1, max_length=16)
    background_region: MotionRegion


def _store(request):
    return request.app.state.motion_pack_store


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except MotionPackConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "motion pack or clip not found") from exc
    except (InvalidMotionPack, ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _required(store, pack_id, version=None):
    pack = _call(store.load, pack_id) if version is None else _call(store.load_version, pack_id, version)
    if pack is None:
        raise HTTPException(404, "motion pack not found")
    return pack


@router.get("")
def list_packs(request: Request):
    return _call(_store(request).list)


@router.post("", status_code=201)
def create_pack(payload: MotionPackDraft, request: Request):
    return _call(_store(request).create, payload)


@router.get("/{pack_id}")
def get_pack(pack_id: str, request: Request):
    return _required(_store(request), pack_id)


@router.put("/{pack_id}")
def update_pack(pack_id: str, payload: UpdatePack, request: Request):
    return _call(_store(request).update, pack_id, payload.draft, payload.expected_revision)


@router.get("/{pack_id}/versions/{version}")
def get_version(pack_id: str, version: str, request: Request):
    return _required(_store(request), pack_id, version)


@router.get("/{pack_id}/readiness")
def get_readiness(pack_id: str, request: Request,
                  style: Literal["twoShot", "overShoulder", "mixed"] = "twoShot",
                  version: str | None = None):
    return _call(_store(request).readiness, pack_id, style, version)


@router.post("/{pack_id}/clips/{clip_id}/review")
def review_clip(pack_id: str, clip_id: str, payload: ReviewClip, request: Request):
    return _call(_store(request).review, pack_id, clip_id, payload.accepted,
                 payload.expected_revision, payload.reason)


def _inspect(store, pack_id, clip_id, payload):
    pack = _required(store, pack_id)
    if pack.revision != payload.expected_revision:
        raise MotionPackConflict("MOTION_PACK_REVISION_CONFLICT")
    clip = next((c for c in pack.clips if c.id == clip_id), None)
    if clip is None:
        raise KeyError(clip_id)
    fingerprint = clip_fingerprint(pack, clip)
    # Lookup is the sole source of filesystem paths. The store verifies all
    # references and hashes again when recording the completed report.
    entry = store.artifact_lookup(clip.media.artifact_id) if store.artifact_lookup else None
    if not entry or entry.get("kind") != "video" or entry.get("fileHash") != clip.media.file_hash:
        raise InvalidMotionPack("referenced motion video is missing or changed")
    if not isinstance(entry.get("path"), str) or not entry["path"]:
        raise InvalidMotionPack("referenced motion video path is invalid")
    report = inspect_motion_clip(
        Path(entry["path"]), expected_file_hash=clip.media.file_hash,
        expected_frames=clip.frame_count, expected_fps=clip.fps,
        usable_start_frame=clip.usable_start_frame, usable_end_frame=clip.usable_end_frame,
        body_regions=[r.model_dump() for r in payload.body_regions],
        exclusion_regions=[r.model_dump() for r in payload.exclusion_regions],
        background_region=payload.background_region.model_dump())
    current = _required(store, pack_id)
    current_clip = next((c for c in current.clips if c.id == clip_id), None)
    if current.revision != payload.expected_revision or current_clip is None or clip_fingerprint(current, current_clip) != fingerprint:
        raise MotionPackConflict("MOTION_PACK_REVISION_CONFLICT")
    return store.record_checks(pack_id, clip_id, report, payload.expected_revision)


@router.post("/{pack_id}/clips/{clip_id}/inspect")
async def inspect_clip(pack_id: str, clip_id: str, payload: InspectClip, request: Request):
    # Admission is bounded per app. Retain the permit until the actual worker
    # ends, including when the HTTP request is cancelled during decoding.
    if not hasattr(request.app.state, "motion_inspection_slots"):
        request.app.state.motion_inspection_slots = asyncio.Semaphore(2)
    async with request.app.state.motion_inspection_slots:
        worker = asyncio.create_task(asyncio.to_thread(_call, _inspect, _store(request), pack_id, clip_id, payload))
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
            # Retrieve failure without replacing the original request cancellation.
            if not worker.cancelled():
                worker.exception()
            raise
