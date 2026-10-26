"""Recorded motion-workflow evidence only; no inference, probing or arbitrary paths."""
import math
import re
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/api/diagnostics", tags=["motion-diagnostics"])
CACHE_KEYS = ("audioFeatureCacheHits", "audioFeatureCacheMisses", "audioFeatureCacheInvalid", "audioFeaturePasses", "latentCacheHits", "latentCacheMisses", "latentCacheInvalid")
TIMING_KEYS = ("audioFeaturesSec", "latentPreparationSec", "cameraRenderSec", "finalComposeSec")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _text(value):
    return value if isinstance(value, str) and len(value) <= 256 and not any(c in value for c in ("/", "\\", ":", "\n", "\r")) else None


def _native(value):
    value = _dict(value)
    return {key: _number(value.get(key)) for key in ("width", "height", "fps", "frames", "durationSec")}


def _elapsed(job):
    if not job.started_at or not job.finished_at:
        return None
    try:
        start, end = datetime.fromisoformat(job.started_at.replace("Z", "+00:00")), datetime.fromisoformat(job.finished_at.replace("Z", "+00:00"))
        return _number((end - start).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def _relevant(job):
    snapshot = _dict(job.input_snapshot)
    return job.kind == "motion.prepare" or job.kind in {"renders", "sample.generate"} and bool(_dict(snapshot.get("projectSnapshot")).get("motionPack"))


def _classification(job):
    result = _dict(job.result)
    meta = _dict(result.get("meta"))
    metrics = _dict(meta.get("generationStats"))
    provenance = _dict(_dict(result.get("candidate")).get("provenance"))
    completed = _dict(_dict(job.completed_calls).get("motionPreparation"))
    if any(container.get("simulated") is True for container in (result, meta, metrics, provenance, _dict(job.input_snapshot))):
        return "simulated"
    if metrics.get("motionSource") == "pinned-reviewed-motion-pack" or job.kind == "motion.prepare" and (job.provider_task_id or completed.get("promptId") or provenance.get("promptId")):
        return "real"
    return "unverified"


def _job_record(job, artifacts, motion_store=None):
    result, snapshot = _dict(job.result), _dict(job.input_snapshot)
    meta = _dict(result.get("meta"))
    metrics = _dict(meta.get("generationStats"))
    candidate = _dict(result.get("candidate"))
    provenance = _dict(candidate.get("provenance"))
    classification = _classification(job)
    counters = _dict(metrics.get("stats"))
    cache = {key: _count(counters.get(key, metrics.get(key))) for key in CACHE_KEYS}
    raw_timings = _dict(metrics.get("stageTimings"))
    timings = {key: _number(raw_timings.get(key)) for key in TIMING_KEYS}
    project = _dict(snapshot.get("projectSnapshot"))
    pack_snapshot = _dict(snapshot.get("packSnapshot"))
    pin = _dict(project.get("motionPack")) or {"id": pack_snapshot.get("id"), "version": pack_snapshot.get("version")}
    if not pack_snapshot and motion_store and _text(pin.get("id")) and _text(pin.get("version")):
        try:
            pinned = motion_store.load_version(pin["id"], pin["version"])
            pack_snapshot = pinned.model_dump(mode="json", by_alias=True) if pinned else {}
        except (ValueError, OSError):
            pack_snapshot = {}
    inputs = []
    for camera in pack_snapshot.get("cameras", [])[:100] if isinstance(pack_snapshot.get("cameras"), list) else []:
        if isinstance(camera, dict):
            ref = _dict(camera.get("masterImage"))
            if ref:
                inputs.append({"artifactId": _text(ref.get("artifactId")), "fileHash": _text(ref.get("fileHash")),
                               "kind": "cameraReference", "cameraId": _text(camera.get("id"))})
    for role, character in _dict(pack_snapshot.get("characters")).items():
        if role not in ("A", "B"):
            continue
        ref = _dict(_dict(character).get("referenceImage"))
        if ref:
            inputs.append({"artifactId": _text(ref.get("artifactId")), "fileHash": _text(ref.get("fileHash")),
                           "kind": "characterReference", "role": role})
    for clip in pack_snapshot.get("clips", [])[:100] if isinstance(pack_snapshot.get("clips"), list) else []:
        if isinstance(clip, dict):
            ref = _dict(clip.get("media"))
            inputs.append({"artifactId": _text(ref.get("artifactId")), "fileHash": _text(ref.get("fileHash")),
                           "role": clip.get("role") if clip.get("role") in ("A", "B") else None,
                           "native": _native({"width": clip.get("nativeWidth"), "height": clip.get("nativeHeight"),
                                              "fps": clip.get("fps"), "frames": clip.get("frameCount")})})
    outputs = []
    for artifact_id in result.get("artifactIds", [])[:20] if isinstance(result.get("artifactIds"), list) else []:
        if not isinstance(artifact_id, str):
            continue
        entry = artifacts.by_id(artifact_id)
        params = _dict(entry.get("paramsSnapshot")) if entry else {}
        outputs.append({"artifactId": _text(artifact_id), "registered": entry is not None,
                        "fileHash": _text(entry.get("fileHash")) if entry else None,
                        "workflowVersion": _text(entry.get("workflowVersion")) if entry else None,
                        "native": _native(params.get("native") or candidate.get("native")),
                        "generator": params.get("generator") if params.get("generator") == "ComfyUI/WanVideoImageToVideoEncode" else _text(params.get("generator")),
                        "workflowHash": _text(params.get("workflowHash")),
                        "packVersion": _text(params.get("packVersion"))})
    size = re.fullmatch(r"([1-9][0-9]{0,4})x([1-9][0-9]{0,4})", str(meta.get("resolution", "")))
    return {"jobId": job.id, "kind": job.kind, "status": job.status.value, "classification": classification,
            "ownerType": job.owner_type, "ownerId": _text(job.owner_id or job.project_id),
            "pack": {"id": _text(pin.get("id")), "version": _text(pin.get("version"))},
            "shotStyle": project.get("shotStyle") if project.get("shotStyle") in ("twoShot", "overShoulder", "mixed") else None,
            "engineWallSeconds": _number(metrics.get("wallSeconds")), "executionElapsedSeconds": _elapsed(job),
            "stageTimingsSeconds": timings, "cache": cache, "peakCudaGB": _number(metrics.get("peakCudaGB")),
            "cameraNative": [{"cameraId": _text(camera_id),
                              "width": _count(camera.get("nativeSize", [None, None])[0]),
                              "height": _count(camera.get("nativeSize", [None, None])[1])}
                             for camera_id, camera in _dict(metrics.get("cameras")).items()
                             if isinstance(camera, dict) and isinstance(camera.get("nativeSize"), list) and len(camera["nativeSize"]) == 2],
            "cost": {"amount": None, "currency": None, "reason": "not_recorded"},
            "inputs": inputs, "outputs": outputs,
            "outputSpec": {"width": int(size[1]) if size else None, "height": int(size[2]) if size else None,
                           "durationSec": _number(meta.get("durationSec")), "fps": _number(meta.get("fps")),
                           "frames": _count(metrics.get("frames"))},
            "qualityStatus": meta.get("qualityStatus") if meta.get("qualityStatus") in ("passed", "failed", "needs_review") else None,
            "evidenceNotice": "仅展示已有记录，每任务最多100机位/100片段/20输出；未重新验证文件、推理效果、五官可见性或人工采用"}


def build_motion_diagnostics(manager, artifacts, limit=50, motion_store=None):
    relevant = [job for job in manager.list() if _relevant(job)]
    records = [_job_record(job, artifacts, motion_store) for job in relevant[:limit]]
    # Global counts use all relevant jobs; detailed records and metric aggregates
    # are explicitly limited to the returned window.
    counts = {category: Counter() for category in ("real", "simulated", "unverified")}
    for job in relevant:
        counts[_classification(job)][job.status.value] += 1
    real = [record for record in records if record["classification"] == "real"]
    aggregate = {}
    for key in CACHE_KEYS:
        values = [record["cache"][key] for record in real if record["cache"][key] is not None]
        aggregate[key] = {"value": sum(values) if values else None, "measuredJobs": len(values)}
    stages = {}
    for key in TIMING_KEYS:
        values = [record["stageTimingsSeconds"][key] for record in real if record["stageTimingsSeconds"][key] is not None]
        stages[key] = {"totalSeconds": sum(values) if values else None,
                       "meanSeconds": sum(values) / len(values) if values else None, "measuredJobs": len(values)}
    return {"schemaVersion": 1, "createdAt": datetime.now(timezone.utc).isoformat(),
            "totalRelevantJobs": len(relevant), "returnedJobs": len(records), "truncated": len(relevant) > len(records),
            "counts": {key: dict(value) for key, value in counts.items()},
            "realMetricsWindow": {"jobs": len(real), "cache": aggregate, "stageTimings": stages}, "jobs": records,
            "limitations": ["费用未接账单，未知值为null", "缓存命中只按实际计数；缺记录不推断为0", "没有冷/热重复实测时不能据此宣称提速", "真实引擎记录不等于质量验收完成"]}


@router.get("/motion-workflow")
def motion_workflow(request: Request, limit: int = Query(50, ge=1, le=200)):
    return build_motion_diagnostics(request.app.state.job_manager, request.app.state.artifacts, limit,
                                    getattr(request.app.state, "motion_pack_store", None))
