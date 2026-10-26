"""Read-only runtime and capacity checks; no model execution."""
import platform
import shutil
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


@router.get("/package")
def package(request: Request):
    """Allowlisted facts only: omit paths, configuration values, scripts and raw errors."""
    from collections import Counter
    from importlib.metadata import version, PackageNotFoundError
    from datetime import datetime, timezone
    jobs = sorted(request.app.state.job_manager.list(), key=lambda j: j.created_at, reverse=True)
    failures = []
    for job in jobs:
        if job.status.value != "failed":
            continue
        error = (job.error or "").lower()
        category = ("space" if any(s in error for s in ("空间", "no space", "disk full")) else
                    "credentials" if any(s in error for s in ("401", "403", "凭据", "unauthorized")) else
                    "memory" if any(s in error for s in ("显存", "out of memory", "cuda")) else
                    "missing_file" if any(s in error for s in ("缺失", "不存在", "not found", "no such file")) else "other")
        failures.append({"kind": job.kind if job.kind in {"script.generate", "script.rewrite", "tts.synthesize", "tts.audition", "visual.generate", "sample.generate", "renders"} else "other", "category": category})
        if len(failures) == 30:
            break
    snapshot = request.app.state.capabilities.snapshot()
    try:
        app_version = version("duocast-server")
    except PackageNotFoundError:
        app_version = "unknown"
    data = {"format": 1, "applicationVersion": app_version, "createdAt": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(), "system": platform.system(),
            "ffmpegAvailable": bool(shutil.which("ffmpeg")), "ffprobeAvailable": bool(shutil.which("ffprobe")),
            "freeBytes": shutil.disk_usage(request.app.state.artifacts.root).free,
            "modes": {k: v.get("mode") for k, v in snapshot.items()},
            "jobCounts": dict(Counter(j.status.value for j in jobs)), "recentFailures": failures,
            "notice": "未包含密钥、地址、路径、工程名、脚本、原始错误或日志；错误仅保留分类"}
    return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="duocast-diagnostics.json"'})


@router.get("")
def diagnostics(request: Request):
    from ..adapters.musetalk_video import require_engine
    cfg = request.app.state.runtime_settings
    try:
        fast = {"ready": True, "root": str(require_engine())}
    except ValueError as exc:
        fast = {"ready": False, "message": str(exc)}
    disk = shutil.disk_usage(request.app.state.artifacts.root)
    return {"python": platform.python_version(), "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"),
            "freeBytes": disk.free, "fast": fast, "checks": request.app.state.provider_checks,
            "references": {s: Path(p).is_file() if p else False for s, p in {"A": cfg.tts_ref_a, "B": cfg.tts_ref_b}.items()},
            "notice": "文件和节点检测不代表真实推理通过；请完成样片验证"}


@router.get("/fast-templates")
def fast_templates():
    from ..services.fast_catalog import catalog
    return catalog()
