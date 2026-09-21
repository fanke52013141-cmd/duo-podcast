"""DuoCast 编排服务入口（06 §2 main.py）：装配路由、事件总线、调度器；启动自检。

启动：python -m duocast.main（或 uvicorn duocast.main:app）。
前端构建产物由本服务提供（06 §4.2 静态资源）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .adapters.base import CapabilityRegistry, TTSProvider
from .adapters.comfyui_tts import ComfyUITTSProvider
from .adapters.mock import MockImageProvider, MockTextProvider, MockTTSProvider, MockVideoProvider
from .adapters.toapis_image import ToAPIsImageProvider
from .core.config import PROJECT_ROOT, settings
from .core.eventbus import EventBus
from .core.logging import log, setup_logging
from .jobs.manager import JobManager
from .jobs.recovery import recover_on_startup
from .domain.project import Project, ScriptRevision
from .services.script_svc import apply_script_revision, build_script_revision
from .services.visual_svc import apply_visual_variant
from .services.voice_svc import find_revision, synthesize_timeline
from .storage.artifacts import ArtifactStore
from .storage.machine import CacheStore, MachineSettings
from .storage.project_store import InvalidProjectInput, ProjectStore

setup_logging()


def _job_runner(manager: JobManager, store: ProjectStore, artifacts: ArtifactStore,
                text_provider: MockTextProvider, image_provider: MockImageProvider,
                tts_provider: TTSProvider):
    """Job 执行分派：按 kind 路由到对应 service（06 §6.1 runner 装配）。"""

    async def runner(job):
        if job.kind == "script.generate":
            snapshot = job.input_snapshot
            result = await text_provider.generate({
                "sourceInput": snapshot.get("sourceInput", {}),
                "brief": snapshot.get("brief", {}),
            })
            project = store.load(job.project_id)
            if project is None:
                raise RuntimeError("project vanished")
            revision = build_script_revision(project, text_provider, result)
            apply_script_revision(store, job.project_id, revision,
                                  job.input_snapshot.get("expectedProjectRevision"))
            # mock 无真实媒体产物：artifactIds 留空，版本指针走 meta（11 报告 P2-4）
            return {"artifactIds": [], "meta": {"revisionId": revision.id}}
        if job.kind == "script.rewrite":
            # 候选改写：不落盘，返回差异供用户审阅（01 §4.2 接受后才替换）
            project = store.load(job.project_id)
            if project is None:
                raise RuntimeError("project vanished")
            rev = ScriptRevision.model_validate(job.input_snapshot["scriptSnapshot"])
            selected = [t.model_dump(by_alias=True) for t in rev.turns
                        if t.id in job.input_snapshot.get("turnIds", [])]
            result = await text_provider.rewrite({
                "turns": selected,
                "turnIds": job.input_snapshot.get("turnIds", []),
                "mode": job.input_snapshot.get("mode", "rewrite"),
            })
            return {"candidate": result, "revisionId": rev.id}
        if job.kind == "tts.synthesize":
            project = Project.model_validate(job.input_snapshot["projectSnapshot"])
            revision = find_revision(project, job.input_snapshot.get("revisionId"))
            timeline = await synthesize_timeline(
                store, tts_provider, project, revision,
                bindings=job.input_snapshot.get("voiceBindings", {}),
                transition_gap_ms=job.input_snapshot.get("transitionGapMs"),
                lead_in_ms=job.input_snapshot.get("leadInMs", 0),
                tail_out_ms=job.input_snapshot.get("tailOutMs", 0),
                artifact_store=artifacts,
                artifacts_root=artifacts.root,
            )
            unit_ids = [u.adopted_audio_asset_id for u in timeline.units if u.adopted_audio_asset_id]
            master_ids = [timeline.master_audio_asset_id] if timeline.master_audio_asset_id else []
            return {
                "artifactIds": [*unit_ids, *master_ids],
                "meta": {"sampleRate": timeline.sample_rate, "sampleCount": timeline.sample_count,
                         "units": len(timeline.units),
                         "masterAudioAssetId": timeline.master_audio_asset_id},
            }
        if job.kind == "tts.audition":
            snap = job.input_snapshot
            speaker = snap.get("speaker", "A")
            res = await tts_provider.audition({
                "lineTexts": [snap.get("text") or "你好，这是一段音色试听。"],
                "voiceBindingId": f"VB-{speaker}-audition",
                "speaker": speaker,
                "providerProfileId": "tts-audition", "modelId": "",
                "emotion": {"label": "自然"}, "speedRatio": snap.get("speedRatio", 1.0),
            })
            asset_id = res.get("audioAssetId")
            return {"artifactIds": [asset_id] if asset_id else [],
                    "meta": {"durationMs": res.get("durationMs"), "speaker": speaker}}
        if job.kind == "visual.generate":
            # 先走图片提供方（mock 阶段为占位延迟），await 结束后重读工程——
            # 避免用合成期间用户编辑前的旧 revision 提交导致冲突失败（11 报告 P2-2）
            res = await image_provider.generate({
                "aspect": job.input_snapshot.get("aspect", "landscape"),
                "prompt": job.input_snapshot.get("prompt", ""),
                "mode": job.input_snapshot.get("mode", "twoShot"),
                "subjectSpeaker": job.input_snapshot.get("subjectSpeaker"),
                "foregroundSpeaker": job.input_snapshot.get("foregroundSpeaker"),
            })
            project = store.load(job.project_id)
            if project is None:
                raise RuntimeError("project vanished")
            variants = apply_visual_variant(store, project, {
                "aspect": job.input_snapshot.get("aspect", "landscape"),
                "mode": job.input_snapshot.get("mode", "twoShot"),
                "cameraGroupId": job.input_snapshot.get("cameraGroupId"),
                "cameraAssetId": job.input_snapshot.get("cameraAssetId"),
                "subjectSpeaker": job.input_snapshot.get("subjectSpeaker"),
                "foregroundSpeaker": job.input_snapshot.get("foregroundSpeaker"),
                **{k: res.get(k) for k in ("artifactId", "path", "fileHash", "provider") if res.get(k)},
            })
            artifact_ids = [variants[0].master_image.artifact_id] if res.get("artifactId") else []
            return {"artifactIds": artifact_ids, "meta": {"variantId": variants[0].id}}
        if job.kind == "renders":
            # 模拟任务只验证流程；不得登记不存在的视频或覆盖真实输出指针。
            project = Project.model_validate(job.input_snapshot["projectSnapshot"])
            revision_id = job.input_snapshot["revisionId"]
            output = f"DEMO-{revision_id}-{job.id}"
            return {
                "artifactIds": [],
                "meta": {"outputVersion": output,
                         "resolution": job.input_snapshot.get("resolution"),
                         "fps": job.input_snapshot.get("fps")},
            }
        raise RuntimeError(f"unknown job kind: {job.kind}")

    # kind → 通道：真实适配器的任务不标记 simulated（关口 A：TTS 已接 ComfyUI）
    kind_channel = {"script.generate": "text", "script.rewrite": "text",
                    "tts.synthesize": "tts", "tts.audition": "tts",
                    "visual.generate": "image", "renders": "video"}
    channel_provider = {
        "text": text_provider, "image": image_provider,
        "tts": tts_provider, "video": None,
    }

    async def simulated_runner(job):
        provider = channel_provider.get(kind_channel.get(job.kind, ""))
        is_simulated = True if provider is None else bool(provider.capabilities.get("simulated"))
        return {**await runner(job), "simulated": is_simulated}

    return simulated_runner


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 存储层装配 ----
    project_store = ProjectStore(settings.storage_root / "projects", debounce_ms=settings.project_debounce_ms)
    artifacts = ArtifactStore(settings.storage_root / "artifacts")
    machine = MachineSettings(settings.storage_root / "machine")
    cache = CacheStore(settings.storage_root / "cache")

    # ---- 事件总线 + 任务系统 ----
    event_bus = EventBus(log_path=settings.storage_root / "events" / "events.jsonl", cap=settings.event_log_cap)
    event_bus.resume_from_log()  # 序号跨重启续接（12 报告 C-5），防止重连补发误判连续
    job_manager = JobManager(settings.storage_root / "jobs", event_bus, queue_caps={
        "gpu": settings.queue_cap_gpu, "api": settings.queue_cap_api, "cpu": settings.queue_cap_cpu,
    })

    # ---- 适配器（TTS/图片已接真实引擎；其余通道实测后替换） ----
    text_api = MockTextProvider()
    if settings.image_provider == "toapis" and settings.toapis_key:
        image_api = ToAPIsImageProvider(
            api_key=settings.toapis_key,
            artifacts_root=settings.storage_root / "artifacts",
            artifact_store=artifacts,
            size=settings.image_size, quality=settings.image_quality,
            resolution=settings.image_resolution,
        )
    else:
        image_api = MockImageProvider()
    if settings.tts_provider == "comfyui":
        local_tts: TTSProvider = ComfyUITTSProvider(
            base_url=settings.comfyui_url,
            input_dir=settings.comfyui_input,
            refs={"A": settings.tts_ref_a, "B": settings.tts_ref_b or settings.tts_ref_a},
            artifacts_root=settings.storage_root / "artifacts",
            artifact_store=artifacts,
        )
    else:
        local_tts = MockTTSProvider()
    video = MockVideoProvider()
    capabilities = CapabilityRegistry(
        text=text_api.capabilities,
        image=image_api.capabilities,
        tts=local_tts.capabilities,
        video=video.capabilities,
    )

    # ---- 装配 ----
    app.state.project_store = project_store
    app.state.artifacts = artifacts
    app.state.machine = machine
    app.state.cache = cache
    app.state.event_bus = event_bus
    app.state.job_manager = job_manager
    app.state.capabilities = capabilities
    job_manager.set_runner(_job_runner(job_manager, project_store, artifacts, text_api, image_api, local_tts))

    # ---- 启动自检（02 §7.4 最小版） ----
    recovered = recover_on_startup(settings.storage_root / "jobs")
    job_manager.restore(recovered)
    log("startup", "duocast server assembling", host=settings.host, port=settings.port,
        storage=settings.storage_root, recovered_jobs=len(recovered))
    job_manager.start()

    try:
        yield
    finally:
        await job_manager.stop()
        project_store.flush()
        log("shutdown", "duocast server stopping")


app = FastAPI(title="DuoCast 双声播客工坊", version="0.1.0", lifespan=lifespan)


@app.exception_handler(InvalidProjectInput)
async def invalid_project_input(request: Request, exc: InvalidProjectInput) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5178", "http://127.0.0.1:5178",
        "http://localhost:5173", "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

from .api import artifacts as artifacts_api
from .api import events as events_api
from .api import jobs as jobs_api
from .api import machine as machine_api
from .api import projects as projects_api
from .api import providers as providers_api
from .api import render as render_api
from .api import script as script_api
from .api import visual as visual_api
from .api import voice as voice_api

app.include_router(projects_api.router)
app.include_router(providers_api.router)
app.include_router(script_api.router)
app.include_router(voice_api.router)
app.include_router(visual_api.router)
app.include_router(render_api.router)
app.include_router(jobs_api.router)
app.include_router(events_api.router)
app.include_router(artifacts_api.router)
app.include_router(machine_api.router)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "service": "duocast", "version": "0.1.0"}


# 前端构建产物（apps/web/dist；06 §4.2：日常使用不跑 Vite）
_web_dist = PROJECT_ROOT / "apps" / "web" / "dist"
if _web_dist.exists():
    app.mount("/", StaticFiles(directory=str(_web_dist), html=True), name="web")


@app.get("/", response_model=None)
async def root():
    index = _web_dist / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"ok": True, "service": "duocast", "note": "前端未构建（cd apps/web && pnpm build）"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
