"""Submit independent motion-pack candidate jobs, without project ownership."""
from fastapi import APIRouter, HTTPException, Request

from ..services.motion_preparation import PreparationRequest
from ..storage.motion_packs import InvalidMotionPack, MotionPackConflict

router = APIRouter(prefix="/api/motion-packs", tags=["motion-preparation"])


@router.post("/{pack_id}/prepare", status_code=202)
async def prepare_motion(pack_id: str, payload: PreparationRequest, request: Request):
    service = getattr(request.app.state, "motion_preparation_service", None)
    if service is None:
        raise HTTPException(503, "真实动作准备服务未配置")
    try:
        return service.submit(pack_id, payload)
    except MotionPackConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "motion pack not found") from exc
    except (InvalidMotionPack, ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/{pack_id}/prepare/{job_id}/resume", status_code=202)
async def resume_preparation(pack_id: str, job_id: str, request: Request):
    service = getattr(request.app.state, "motion_preparation_service", None)
    if service is None:
        raise HTTPException(503, "真实动作准备服务未配置")
    try:
        return service.resume(pack_id, job_id)
    except KeyError as exc:
        raise HTTPException(404, "motion preparation job not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
