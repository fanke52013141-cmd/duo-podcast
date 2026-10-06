"""渲染接口（01 §12.1）：POST /api/projects/{id}/render/generate → Job（gpu 队列）。"""

from __future__ import annotations

import uuid
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager
from ..services.render_inputs import dependency_hash, validate_render_options, resolve_visual_id, resolve_camera_mode, validate_motion_source
from .voice import _merged_pronunciation
from ..services.render_svc import apply_output_version
from ..services.listening_plan import listening_plan
from ..services.generation_profiles import validate_motion_reference, validate_video_stream

router = APIRouter(prefix="/api/projects/{project_id}/render", tags=["render"])


@router.post("/generate")
async def generate(project_id: str, payload: dict, request: Request) -> dict:
    manager: JobManager = request.app.state.job_manager
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    validate_render_options(project, payload, _merged_pronunciation(request, project),
                            real_video=not request.app.state.capabilities.video.get("simulated", True))
    validate_motion_source(project, payload, request.app.state.artifacts)
    validate_motion_reference(project, payload, request.app.state.artifacts)
    timeline = project.audio_timeline
    if timeline is None or timeline.sample_rate <= 0 or timeline.sample_count <= 0:
        raise HTTPException(422, "请先生成有效配音时间轨")
    if timeline.sample_count > 300 * timeline.sample_rate:
        raise HTTPException(422, "完整音轨超过 5 分钟，请先精简内容；不会自动截断")
    revision_id = payload.get("revisionId") or project.current_draft_revision
    if timeline.revision_id != revision_id or revision_id != project.current_draft_revision:
        raise HTTPException(409, "配音已过期，请先采用当前脚本对应的配音")
    client_token = payload.get("clientToken") or f"vid-{uuid.uuid4().hex[:8]}"
    snapshot = {
        "cacheOnly": bool(payload.get("reuseMotionArtifactId") and payload.get("redoFromSec") is None and payload.get("generationProfile") != "dialogue"),
        "revisionId": revision_id,
        "projectSnapshot": project.model_dump(mode="json", by_alias=True),
        "dependencyHash": dependency_hash(project),
        "visualVariantId": resolve_visual_id(project, payload),
        "performanceMode": payload.get("performanceMode", project.performance_mode),
        "generationProfile": payload.get("generationProfile", "accepted"),
        "motionReferenceArtifactId": payload.get("motionReferenceArtifactId"),
        "listeningPlan": listening_plan(project.model_dump(by_alias=True), resolve_visual_id(project, payload), payload.get("listeningSeed", 1)) if payload.get("generationProfile") == "dialogue" else None,
        "reuseMotionArtifactId": payload.get("reuseMotionArtifactId"),
        "redoFromSec": payload.get("redoFromSec"),
        "candidate": bool(payload.get("candidate", False)),
        "resumableVideo": True,
        "cameraMode": resolve_camera_mode(project, payload),
        "seed": payload.get("seed", 1),
        "listeningSeed": payload.get("listeningSeed", 1),
        "resolution": payload.get("resolution", "1920x1080"),
        "fps": payload.get("fps", 30),
        "projectId": project_id,
    }
    job = manager.submit(
        kind="renders",
        project_id=project_id,
        queue_class="cpu" if snapshot["cacheOnly"] or snapshot["generationProfile"] == "listener" else "gpu",
        client_token=client_token,
        input_snapshot=snapshot,
    )
    return {"jobId": job.id, "clientToken": client_token}


@router.post("/adopt")
async def adopt(project_id: str, payload: dict, request: Request):
    project = request.app.state.project_store.load(project_id)
    job = request.app.state.job_manager.get(payload.get("jobId", ""))
    if project is None:
        raise HTTPException(404, "工程不存在")
    if not job or job.project_id != project_id or job.kind != "renders" or job.status.value != "succeeded" or not job.result or not job.result.get("artifactIds"):
        raise HTTPException(422, "请选择本工程已完成的真实成片")
    if job.input_snapshot.get("dependencyHash") != dependency_hash(project):
        raise HTTPException(409, "候选生成后声画输入已改变")
    validate_render_options(project, {}, _merged_pronunciation(request, project), real_video=False)
    meta = job.result.get("meta", {})
    if meta.get("path") and not job.result.get("simulated", False):
        process = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_streams", "-of", "json", meta["path"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            if process.returncode:
                raise ValueError("成片文件无法读取")
            stream = json.loads(output)["streams"][0]
            validate_video_stream(stream, meta["durationSec"], meta["fps"])
        except (ValueError, KeyError, IndexError, asyncio.TimeoutError):
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise HTTPException(409, "成片画面不完整或文件无法读取，不能采用该候选")
    if project.output_manifest and project.output_manifest.get("artifactIds") == job.result["artifactIds"]:
        return {"version": project.output_manifest["version"]}
    result = {"artifactIds": job.result["artifactIds"], "meta": {**job.result["meta"], "candidate": False}}
    version = apply_output_version(request.app.state.project_store, project, job.input_snapshot["revisionId"], result)
    return {"version": version}


@router.post("/select-version")
async def select_version(project_id: str, payload: dict, request: Request):
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "工程不存在")
    versions = project.output_history + ([project.output_manifest] if project.output_manifest else [])
    manifest = next((v for v in versions if v["version"] == payload.get("version")), None)
    if manifest is None:
        raise HTTPException(404, "输出版本不存在")
    store.apply(project_id, {"selected_output_version": manifest["version"], "output_manifest": manifest}, expected_revision=project.revision)
    return {"version": manifest["version"]}


@router.get("/listening-plan")
async def preview_listening_plan(project_id: str, request: Request, visualVariantId: str | None = None, seed: int = 1):
    project = request.app.state.project_store.load(project_id)
    if project is None:
        raise HTTPException(404, "工程不存在")
    validate_render_options(project, {"visualVariantId":visualVariantId,"listeningSeed":seed}, _merged_pronunciation(request, project))
    if not project.audio_timeline or project.audio_timeline.sample_count <= 0:
        raise HTTPException(422, "请先建立有效配音")
    return listening_plan(project.model_dump(by_alias=True), resolve_visual_id(project, {"visualVariantId":visualVariantId}), seed)
