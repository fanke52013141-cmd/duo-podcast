"""配音接口（01 §12.1）：POST /api/projects/{id}/voice/synthesize → Job（gpu 队列）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager
from ..services.voice_svc import adopt_unit_candidate, find_revision

router = APIRouter(prefix="/api/projects/{project_id}/voice", tags=["voice"])


@router.post("/synthesize")
async def synthesize(project_id: str, payload: dict, request: Request) -> dict:
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        revision = find_revision(project, payload.get("revisionId"))
    except KeyError as exc:
        raise HTTPException(422, "没有可用脚本版本") from exc
    turn_ids = None
    if "turnIds" in payload:
        turn_ids = payload["turnIds"]
        valid_turn_ids = {turn.id for turn in revision.turns}
        if (not isinstance(turn_ids, list) or not turn_ids
                or any(not isinstance(turn_id, str) or not turn_id.strip() for turn_id in turn_ids)
                or len(set(turn_ids)) != len(turn_ids)
                or any(turn_id not in valid_turn_ids for turn_id in turn_ids)):
            raise HTTPException(422, "turnIds 必须是当前脚本中的非空、无重复话轮 ID 列表")
    client_token = payload.get("clientToken") or f"tts-{uuid.uuid4().hex[:8]}"
    snapshot = {
        "revisionId": revision.id,
        "projectSnapshot": project.model_dump(mode="json", by_alias=True),
        "voiceBindings": payload.get("voiceBindings", {}),
        "transitionGapMs": payload.get("transitionGapMs"),
        "leadInMs": payload.get("leadInMs", 0),
        "tailOutMs": payload.get("tailOutMs", 0),
        "turnIds": turn_ids,
        "projectId": project_id,
    }
    job = manager.submit(
        kind="tts.synthesize",
        project_id=project_id,
        queue_class="gpu",
        client_token=client_token,
        input_snapshot=snapshot,
    )
    return {"jobId": job.id, "clientToken": client_token}


@router.post("/units/{unit_id}/adopt")
async def adopt(project_id: str, unit_id: str, payload: dict, request: Request) -> dict:
    """采用一条候选音频，并据所有已采用单元重新建立权威母轨。"""
    candidate_asset_id = payload.get("candidateAudioAssetId")
    if not isinstance(candidate_asset_id, str) or not candidate_asset_id.strip():
        raise HTTPException(422, "candidateAudioAssetId 必须是非空字符串")
    try:
        timeline = adopt_unit_candidate(
            request.app.state.project_store, project_id, unit_id, candidate_asset_id,
            artifact_store=request.app.state.artifacts,
            artifacts_root=request.app.state.artifacts.root,
        )
    except KeyError as exc:
        if str(exc).strip("\"") == "PROJECT_NOT_FOUND":
            raise HTTPException(404, "project not found") from exc
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"projectId": project_id, "unitId": unit_id,
            "candidateAudioAssetId": candidate_asset_id,
            "audioTimeline": timeline.model_dump(mode="json", by_alias=True)}


@router.post("/audition")
async def audition(project_id: str, payload: dict, request: Request) -> dict:
    """短试听（绑定声线前的一句话验证）：gpu 队列 Job，产物可直接经 /api/artifacts/{id}/file 播放。"""
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    if store.load(project_id) is None:
        raise HTTPException(404, "project not found")
    speaker = payload.get("speaker", "A")
    if speaker not in ("A", "B"):
        raise HTTPException(422, "speaker 必须是 A 或 B")
    client_token = payload.get("clientToken") or f"aud-{uuid.uuid4().hex[:8]}"
    job = manager.submit(
        kind="tts.audition",
        project_id=project_id,
        queue_class="gpu",
        client_token=client_token,
        input_snapshot={
            "speaker": speaker,
            "text": (payload.get("text") or "")[:120],
            "speedRatio": payload.get("speedRatio", 1.0),
        },
    )
    return {"jobId": job.id, "clientToken": client_token}
