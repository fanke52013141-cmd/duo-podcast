"""Bounded immutable video imports; uploaded media is never implicitly adopted."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from ..services.motion_quality import inspect_motion_clip

router = APIRouter(prefix="/api/motion-media", tags=["motion-media"])
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


@router.post("/images")
async def upload_motion_image(request: Request) -> dict:
    if not hasattr(request.app.state, "motion_import_slots"):
        request.app.state.motion_import_slots = asyncio.Semaphore(2)
    async with request.app.state.motion_import_slots:
        data = bytearray()
        async for block in request.stream():
            data.extend(block)
            if len(data) > 10 * 1024 * 1024:
                raise HTTPException(413, "角色或机位参考图不得超过10MB")
        from .assets import save_uploaded_image
        saved = save_uploaded_image(request, bytes(data), "")
        from pathlib import Path
        target = Path(saved["path"])
        registered = False
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                "-show_entries", "stream=width,height,codec_type", "-of", "json", str(target),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(process.communicate(), 15)
            streams = json.loads(stdout).get("streams", [])
            if process.returncode or not streams or streams[0].get("codec_type") != "video":
                raise ValueError("无法解码参考图")
            width, height = streams[0].get("width", 0), streams[0].get("height", 0)
            if not 16 <= width <= 8192 or not 16 <= height <= 8192 or width * height > 32_000_000:
                raise ValueError("参考图尺寸不符合要求")
            store = request.app.state.artifacts
            digest = hashlib.sha256(data).hexdigest()
            asset_id = "MOTION-IMG-" + uuid.uuid4().hex
            store.register(asset_id, "image", str(target), digest,
                           {"source": "motionReference", "width": width, "height": height}, file_hash=digest)
            registered = True
            return {"artifactId": asset_id, "fileHash": digest}
        except (ValueError, OSError, asyncio.TimeoutError) as exc:
            raise HTTPException(422, "参考图校验失败") from exc
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            if not registered:
                target.unlink(missing_ok=True)


@router.post("")
async def upload_motion_media(
    request: Request,
    extension: Literal["mp4", "mov", "mkv", "webm", "avi"] = Query("mp4"),
) -> dict:
    """Receive raw video bytes, not client paths or trusted metadata."""
    if not hasattr(request.app.state, "motion_import_slots"):
        request.app.state.motion_import_slots = asyncio.Semaphore(2)
    async with request.app.state.motion_import_slots:
        return await _import_video(request, extension)


async def _import_video(request: Request, extension: str) -> dict:
    store = request.app.state.artifacts
    root = store.root / "motion-imports"
    root.mkdir(parents=True, exist_ok=True)
    asset_id = "MOTION-" + uuid.uuid4().hex
    target = root / f"{asset_id}.{extension}"
    digest, size, registered = hashlib.sha256(), 0, False
    try:
        with target.open("xb") as output:
            async for block in request.stream():
                size += len(block)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "动作视频不得超过200MB")
                output.write(block)
                digest.update(block)
        if not size:
            raise HTTPException(422, "动作视频不能为空")
        # Import validates actual media; quality/adoption requires a later,
        # role-bound check and explicit human review.
        analysis = asyncio.create_task(asyncio.to_thread(
            inspect_motion_clip, target, expected_file_hash=digest.hexdigest()))
        try:
            report = await asyncio.shield(analysis)
        except asyncio.CancelledError:
            # A thread cannot be cancelled. Wait for its bounded subprocesses
            # before removing the source, especially on Windows.
            while not analysis.done():
                try:
                    await asyncio.shield(analysis)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not analysis.cancelled():
                analysis.exception()
            raise
        native = report.get("native", {})
        media_failures = set(report.get("hardFailures", [])) - {"STATIC_SOURCE", "NO_STRUCTURAL_MOTION"}
        if (media_failures or not native.get("frames") or not native.get("durationSec")
                or not report.get("evidence", {}).get("completeSourceFramesDecoded")):
            raise HTTPException(422, {"message": "无法读取动作视频", "issues": report.get("issues", [])})
        entry = store.register(asset_id, "video", str(target), digest.hexdigest(),
                               {"source": "motionImport", "native": native},
                               file_hash=digest.hexdigest())
        registered = True
        return {"artifactId": asset_id, "fileHash": entry["fileHash"],
                "native": native, "reviewStatus": "unreviewed"}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        if not registered:
            target.unlink(missing_ok=True)
