"""配音接口（01 §12.1）：POST /api/projects/{id}/voice/synthesize → Job（gpu 队列）。"""

from __future__ import annotations

import uuid
import wave

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager
from ..services.voice_svc import adopt_unit_candidate, find_revision
from ..services.voice_svc import retime_adopted_audio, effective_pronunciation

router = APIRouter(prefix="/api/projects/{project_id}/voice", tags=["voice"])


def _merged_pronunciation(request: Request, project) -> list[dict]:
    """合成用读音词典：先全局（machine.json）后本工程，同词条工程级覆盖全局。"""
    machine = getattr(request.app.state, "machine", None)
    return effective_pronunciation(project, machine.get_global_pronunciation() if machine is not None else [])


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
    gaps = payload.get("transitionGapMs")
    if type(gaps) is int:
        gaps = [gaps] * max(0, len(revision.turns) - 1)
    if gaps is not None and (not isinstance(gaps, list)
                            or any(type(value) is not int or value < 0 for value in gaps)):
        raise HTTPException(422, "transitionGapMs 必须为非负整数或非负整数列表")
    snapshot = {
        "revisionId": revision.id,
        "projectSnapshot": project.model_dump(mode="json", by_alias=True),
        "voiceBindings": payload.get("voiceBindings", {}),
        "transitionGapMs": gaps,
        "leadInMs": payload.get("leadInMs", 0),
        "tailOutMs": payload.get("tailOutMs", 0),
        "turnIds": turn_ids,
        # 读音词典在提交时冻结进快照（01 §4.2：读音在合成阶段读取）
        "pronunciation": _merged_pronunciation(request, project),
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


@router.post("/rhythm")
def update_rhythm(project_id: str, payload: dict, request: Request):
    from ..services.render_inputs import validate_render_options
    project = request.app.state.project_store.load(project_id)
    if project is None:
        raise HTTPException(404, "工程不存在")
    validate_render_options(project, {}, _merged_pronunciation(request, project), real_video=False)
    try:
        timeline = retime_adopted_audio(request.app.state.project_store, project_id, payload.get("transitionGapMs"), request.app.state.artifacts)
    except (ValueError, OSError, wave.Error) as exc:
        raise HTTPException(409, str(exc)) from exc
    return timeline.model_dump(by_alias=True)


@router.post("/restore-timeline")
def restore_timeline(project_id: str, payload: dict, request: Request):
    """批 3（42 号方案）P1-D：回退到 audioHistory 中的某条时间轨（按序号）。"""
    from ..services.voice_svc import restore_timeline_version
    try:
        timeline = restore_timeline_version(
            request.app.state.project_store, project_id, (payload or {}).get("historyIndex"),
            artifact_store=request.app.state.artifacts)
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"projectId": project_id, "audioTimeline": timeline.model_dump(mode="json", by_alias=True)}


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


@router.post("/units/{unit_id}/import-audio")
async def import_unit_audio(project_id: str, unit_id: str, request: Request) -> dict:
    """批 2（42 号方案）：上传成品音频替代该单元的 TTS 结果。

    转码为 48kHz PCM16 单声道（母轨权威规格）→ 登记资产 → 注入为候选；
    不自动采用，人试听后走现有 /units/{id}/adopt 采纳。
    """
    import asyncio
    import json as _json
    from ..services.voice_svc import attach_unit_candidate
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "缺少上传文件字段 file")
    data = await upload.read(50 * 1024 * 1024 + 1)
    if not data or len(data) > 50 * 1024 * 1024:
        raise HTTPException(422, "单元音频必须为 0–50MB")
    provenance = {"source": "external-upload"}
    meta_raw = form.get("meta")
    if meta_raw:
        try:
            meta = _json.loads(str(meta_raw))
            if isinstance(meta.get("provenance"), dict):
                provenance.update({k: str(v)[:200] for k, v in meta["provenance"].items()})
        except ValueError:
            raise HTTPException(422, "UPLOAD_SCHEMA: meta 不是合法 JSON")
    artifacts = request.app.state.artifacts
    root = artifacts.root / "audio"
    root.mkdir(parents=True, exist_ok=True)
    asset_id = "AUD-UP-" + uuid.uuid4().hex[:12]
    source, target = root / f"{asset_id}.source", root / f"{asset_id}.wav"
    source.write_bytes(data)
    registered = False
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-y", "-i", str(source),
            "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(target),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, error = await process.communicate()
        if process.returncode:
            raise HTTPException(422, "无法读取上传音频：" + error.decode("utf-8", "replace")[-300:])
        with wave.open(str(target)) as reader:
            sample_count = reader.getnframes()
            if sample_count <= 0 or reader.getframerate() != 48000 or reader.getnchannels() != 1 or reader.getsampwidth() != 2:
                raise HTTPException(422, "UPLOAD_AUDIO: 音频无法转成 48kHz PCM16 单声道")
        unit = next((u for u in (project.audio_timeline.units if project.audio_timeline else []) if u.id == unit_id), None)
        entry = artifacts.register(asset_id, "audio", str(target), artifacts._hash_file(str(target)),
                                   {"source": "unitImport", "turnId": unit.turn_id if unit else "",
                                    "provenance": provenance})
        registered = True
        attach_unit_candidate(store, project_id, unit_id, entry["artifactId"], sample_count)
        return {"unitId": unit_id, "candidateAudioAssetId": entry["artifactId"],
                "sampleCount": sample_count, "durationSec": round(sample_count / 48000, 3),
                "status": "pending-adoption",
                "adoptEndpoint": f"/api/projects/{project_id}/voice/units/{unit_id}/adopt"}
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        source.unlink(missing_ok=True)
        if not registered:
            target.unlink(missing_ok=True)


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
            "referenceAudio": payload.get("referenceAudio"),
        },
    )
    return {"jobId": job.id, "clientToken": client_token}
