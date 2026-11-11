"""渲染接口（01 §12.1）：POST /api/projects/{id}/render/generate → Job（gpu 队列）。"""

from __future__ import annotations

import uuid
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from ..jobs.manager import JobManager
from ..domain.job import JobStatus
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
    from ..services.motion_binding import validate_project_motion_binding
    try:
        if project.motion_pack is not None:
            validate_project_motion_binding(project, request.app.state.motion_pack_store)
    except (ValueError, KeyError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    from ..services.render_spec import render_options
    payload = render_options(project, payload, getattr(request.app.state.artifacts, "root", store.root))
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
        "fastQuality": payload.get("fastQuality", "original"),
        "projectId": project_id,
    }
    if project.motion_pack is not None and project.generation_mode == 'fast':
        from ..services.motion_runtime_identity import freeze_motion_runtime
        try:
            snapshot['fastRuntime'] = await freeze_motion_runtime()
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
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


@router.post("/import-output")
async def import_output(project_id: str, request: Request) -> dict:
    """批 2（42 号方案）：外部成片导入为候选，绝不静默采用。

    严格 ffprobe 校验 → 时长必须与权威时间轨一致 → 登记不可变资产 →
    生成 candidate 渲染记录，人走现有 /render/adopt 采用（依赖哈希+完整性复核）。
    meta.audioMode="master" 时仅取画面、音轨重混本工程母轨（保证时间权威）。
    """
    import hashlib
    from fractions import Fraction
    from pathlib import Path
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    timeline = project.audio_timeline
    if timeline is None or timeline.sample_count <= 0:
        raise HTTPException(422, "请先生成有效配音时间轨，再导入成片")
    if timeline.revision_id != project.current_draft_revision:
        raise HTTPException(409, "配音已过期，请先采用当前脚本对应的配音")
    form = await request.form()
    meta = {}
    if form.get("meta"):
        try:
            meta = json.loads(str(form.get("meta")))
            if not isinstance(meta, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "UPLOAD_SCHEMA: meta 不是合法 JSON 对象")
    payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else meta
    audio_mode = str(payload.get("audioMode") or "keep")
    if audio_mode not in ("keep", "master"):
        raise HTTPException(422, "audioMode 必须为 keep 或 master")
    provenance = {"source": "external-upload"}
    if isinstance(payload.get("provenance"), dict):
        provenance.update({k: str(v)[:200] for k, v in payload["provenance"].items()})
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "缺少上传文件字段 file")
    artifacts = request.app.state.artifacts
    root = artifacts.root / "output-imports"
    root.mkdir(parents=True, exist_ok=True)
    asset_id = "VID-IMPORT-" + uuid.uuid4().hex[:12]
    target = root / f"{asset_id}.mp4"
    digest, size = hashlib.sha256(), 0
    registered = False
    final_path = target
    try:
        with target.open("xb") as output:
            while True:
                block = await upload.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > 2 * 1024 * 1024 * 1024:
                    raise HTTPException(413, "成片视频不得超过 2GB")
                output.write(block)
                digest.update(block)
        if not size:
            raise HTTPException(422, "成片视频不能为空")

        async def probe(path: Path) -> dict:
            process = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=60)
            if process.returncode:
                raise ValueError("无法读取上传视频")
            return json.loads(output_bytes)

        info = await probe(target)
        stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        if stream is None:
            raise HTTPException(422, "上传文件不含视频流")
        try:
            fps = float(Fraction(stream.get("r_frame_rate") or stream.get("avg_frame_rate") or "0"))
        except (ValueError, ZeroDivisionError):
            fps = 0
        duration = float(stream.get("duration") or info.get("format", {}).get("duration") or 0)
        if fps <= 0 or duration <= 0:
            raise HTTPException(422, "无法解析视频帧率或时长")
        try:
            validate_video_stream(stream, duration, fps)
        except ValueError as exc:
            raise HTTPException(422, f"上传视频不完整：{exc}") from exc
        expected_sec = timeline.sample_count / timeline.sample_rate
        if abs(duration - expected_sec) > 0.2:
            raise HTTPException(422, f"上传视频时长 {duration:.2f}s 与当前配音 {expected_sec:.2f}s 不符；"
                                     "请导出与配音等长的视频，或选择 audioMode=master 仅取画面")
        if audio_mode == "master":
            master = artifacts.by_id(timeline.master_audio_asset_id or "")
            if not master or not Path(master["path"]).is_file():
                raise HTTPException(422, "本工程尚无母轨，无法重混；请先合成配音")
            remuxed = root / f"{asset_id}-remux.mp4"
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-y", "-i", str(target), "-i", master["path"],
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-t", f"{expected_sec:.3f}", "-movflags", "+faststart", str(remuxed),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(process.communicate(), timeout=600)
            if process.returncode:
                raise HTTPException(422, "音轨重混失败：" + err.decode("utf-8", "replace")[-300:])
            final_path = remuxed
            info = await probe(final_path)
            stream = next(s for s in info["streams"] if s.get("codec_type") == "video")
            duration = float(stream.get("duration") or info.get("format", {}).get("duration") or 0)
        resolution = f"{stream.get('width')}x{stream.get('height')}"
        file_hash = digest.hexdigest() if final_path == target else artifacts._hash_file(str(final_path))
        entry = artifacts.register(asset_id, "video", str(final_path), dependency_hash(project),
                                   {"source": "outputImport", "projectId": project_id,
                                    "projectSnapshot": project.model_dump(mode="json", by_alias=True),
                                    "provenance": provenance, "audioMode": audio_mode},
                                   file_hash=file_hash)
        registered = True
        manager: JobManager = request.app.state.job_manager
        client_token = str(payload.get("clientToken") or f"import-{uuid.uuid4().hex[:8]}")
        job = manager.submit(
            kind="renders", project_id=project_id, queue_class="cpu", client_token=client_token,
            input_snapshot={"revisionId": project.current_draft_revision,
                            "dependencyHash": dependency_hash(project),
                            "projectSnapshot": project.model_dump(mode="json", by_alias=True),
                            "candidate": True, "imported": True,
                            "resolution": resolution, "fps": fps, "projectId": project_id},
        )
        result = {"artifactIds": [entry["artifactId"]],
                  "meta": {"candidate": True, "imported": True, "durationSec": duration,
                           "fps": fps, "resolution": resolution, "path": str(final_path),
                           "source": "outputImport"}}
        manager.transition(job, JobStatus.RUNNING)
        manager.transition(job, JobStatus.SUCCEEDED, result=result)
        return {"jobId": job.id, "artifactId": entry["artifactId"], "durationSec": duration,
                "resolution": resolution, "fps": fps, "status": "pending-adoption",
                "adoptEndpoint": f"/api/projects/{project_id}/render/adopt"}
    except (ValueError, asyncio.TimeoutError) as exc:
        raise HTTPException(422, f"上传视频校验失败：{exc}") from exc
    finally:
        if not registered:
            target.unlink(missing_ok=True)
            (root / f"{asset_id}-remux.mp4").unlink(missing_ok=True)


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
    # 三个指针必须同步：output_version 供阶段⑤ 过期判断、selected_output_version 供
    # 「是否有产物」、output_manifest 供导出。此前只改后两个，切旧版本后阶段状态与导出不一致。
    store.apply(project_id, {"selected_output_version": manifest["version"],
                             "output_version": manifest["version"],
                             "output_manifest": manifest}, expected_revision=project.revision)
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
