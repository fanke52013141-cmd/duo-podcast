"""Canonical content dependencies shared by previews and final output."""
import hashlib
import json
from fastapi import HTTPException
from .voice_svc import build_units, _effective_bindings, _unit_fingerprint

def dependency_hash(project):
    data = project.model_dump(mode="json", by_alias=True) if hasattr(project, "model_dump") else project
    revision = next((r for r in data["scriptRevisions"] if r["id"] == data["currentDraftRevision"]), None)
    return hashlib.sha256(json.dumps({"script": revision, "audio": data.get("audioTimeline"),
        "visual": data.get("visualVariants"), "bindings": data.get("voiceBindings"),
        "pronunciation": data.get("pronunciationDict"), "selectedVisual": data.get("selectedVisualVariantId"), "cameraMode": data.get("cameraMode"),
        **({"performanceMode": data["performanceMode"]} if data.get("performanceMode", "legacy") != "legacy" else {}),
        **({"shotCameraOverrides":data["shotCameraOverrides"]} if data.get("shotCameraOverrides") else {})}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def resolve_visual_id(project, payload):
    selected = payload.get("visualVariantId") or project.selected_visual_variant_id
    if selected:
        return selected
    return next((v.id for v in reversed(project.visual_variants)
                 if v.mode == "twoShot" and v.aspect == project.aspect), None)


def resolve_camera_mode(project, payload):
    visual_id = resolve_visual_id(project, payload)
    variant = next((v for v in project.visual_variants if v.id == visual_id), None)
    return "speaker" if variant and variant.mode == "overShoulder" else payload.get("cameraMode", project.camera_mode)


def audio_inputs_match(project, pronunciation=None):
    timeline = project.audio_timeline
    revision = next((r for r in project.script_revisions if r.id == project.current_draft_revision), None)
    if not timeline or not revision or timeline.revision_id != revision.id:
        return False
    dictionary = pronunciation if pronunciation is not None else [p.model_dump() for p in project.pronunciation_dict]
    if [t.id for t in revision.turns] != [u.turn_id for u in timeline.units]:
        return False
    if project.voice_bindings:
        expected = build_units(project, revision, _effective_bindings(project, {}), dictionary)
        return [u.pronunciation_revision for u in expected] == [u.pronunciation_revision for u in timeline.units]
    # Historical units still carry the effective voice profile and input fingerprint.
    for turn, unit in zip(revision.turns, timeline.units):
        canonical = unit.model_copy(update={"speed_ratio":turn.speed_ratio,"emotion":{"label":turn.tone or "自然"}})
        if not unit.pronunciation_revision or unit.pronunciation_revision != _unit_fingerprint(turn, canonical, dictionary):
            return False
    return True


def validate_motion_source(project, payload, artifacts):
    source_id = payload.get("reuseMotionArtifactId")
    if not source_id:
        return
    source = artifacts.by_id(source_id)
    frozen = (source or {}).get("paramsSnapshot", {})
    original = frozen.get("projectSnapshot", {})
    if not source or frozen.get("projectId") != project.id or original.get("audioTimeline") != project.model_dump(by_alias=True)["audioTimeline"]:
        raise HTTPException(409, "原动画不属于当前工程或音轨已改变")
    revision_id = project.current_draft_revision
    before = next((r for r in original.get("scriptRevisions", []) if r["id"] == revision_id), None)
    now = next((r.model_dump(by_alias=True) for r in project.script_revisions if r.id == revision_id), None)
    if before != now:
        raise HTTPException(409, "台词已改变，不能复用原动画")
    visual_id = resolve_visual_id(project, payload)
    current = next((v.model_dump(by_alias=True) for v in project.visual_variants if v.id == visual_id), None)
    old_id = frozen.get("visualVariantId") or original.get("selectedVisualVariantId")
    old = next((v for v in original.get("visualVariants", []) if v["id"] == old_id), None)
    if current != old or frozen.get("seed", 1) != payload.get("seed", 1):
        raise HTTPException(409, "原动画的角色资产或种子与当前请求不一致")
    mode = payload.get("performanceMode", project.performance_mode)
    if frozen.get("performanceMode", "legacy") != mode and payload.get("redoFromSec") is None:
        raise HTTPException(409, "改变表演模式需要重新生成或指定局部重做起点")
    if frozen.get("generationProfile", "accepted") != payload.get("generationProfile", "accepted") and payload.get("redoFromSec") is None and payload.get("generationProfile") != "dialogue":
        raise HTTPException(409, "改变生成配置需要重新生成或指定局部重做起点")

def validate_render_options(project, payload, pronunciation=None, real_video=True):
    if payload.get("generationProfile", "accepted") not in {"accepted", "matched", "reference", "listener", "dialogue"}:
        raise HTTPException(422, "未知生成配置")
    if payload.get("performanceMode", project.performance_mode) not in {"legacy", "adaptive"}:
        raise HTTPException(422, "表演模式必须为 legacy 或 adaptive")
    if type(payload.get("listeningSeed", 1)) is not int or not 0 <= payload.get("listeningSeed", 1) <= 10000:
        raise HTTPException(422, "动作变体必须为 0–10000 的整数")
    redo = payload.get("redoFromSec")
    if redo is not None:
        import math
        if type(redo) not in (int, float) or not math.isfinite(redo) or redo < 0 or redo % 5 != 0 or not project.audio_timeline or redo >= project.audio_timeline.sample_count / project.audio_timeline.sample_rate:
            raise HTTPException(422, "局部重做起点必须为音轨范围内的5秒片段边界")
        if not payload.get("reuseMotionArtifactId"):
            raise HTTPException(422, "局部重做需要指定原动画产物")
    if payload.get("cameraMode", "speaker") not in {"speaker", "twoShot"}:
        raise HTTPException(422, "镜头模式必须为 speaker 或 twoShot")
    if type(payload.get("seed", 1)) is not int or not 0 <= payload.get("seed", 1) <= 2**63-1:
        raise HTTPException(422, "seed 必须为有效非负整数")
    if payload.get("visualVariantId") and not any(v.id == payload["visualVariantId"] for v in project.visual_variants):
        raise HTTPException(422, "所选画面资产不存在")
    if payload.get("interpolate") or payload.get("burnSubtitle"):
        raise HTTPException(422, "当前引擎未开放补帧或烧录字幕")
    if payload.get("resolution", "1920x1080") not in {"854x480", "1280x720", "1920x1080"} or payload.get("fps",25) not in {25,30}:
        raise HTTPException(422, "不支持的导出分辨率或帧率")
    if real_video:
        if payload.get("aspect", project.aspect) != "landscape":
            raise HTTPException(422, "当前数字人引擎仅开放横屏同框；竖屏布局尚未验证")
        variant_id = payload.get("visualVariantId") or project.selected_visual_variant_id
        variant = next((v for v in project.visual_variants if v.id == variant_id), None) if variant_id else next((v for v in reversed(project.visual_variants) if v.mode == "twoShot" and v.aspect == "landscape"), None)
        if variant is None:
            raise HTTPException(422, "请先建立有效的横屏双人场景资产")
        if variant and variant.aspect != "landscape":
            raise HTTPException(422, "当前视频引擎仅开放横屏资产")
        if variant.mode == "overShoulder":
            if payload.get("cameraMode", "speaker") != "speaker":
                raise HTTPException(422, "过肩机位按发言人正反打，请选择发言人镜头模式")
            from .over_shoulder import camera_pair, shot_plan
            try:
                camera_pair(variant.model_dump(by_alias=True))
                if project.audio_timeline:
                    shot_plan(project.model_dump(by_alias=True), variant.model_dump(by_alias=True))
            except (ValueError, KeyError, StopIteration) as exc:
                raise HTTPException(422, str(exc)) from exc
            if payload.get("generationProfile", "accepted") != "accepted" or payload.get("reuseMotionArtifactId") or redo is not None:
                raise HTTPException(422, "过肩镜头请使用标准生成配置；机位缓存会自动复用")
            if payload.get("performanceMode", project.performance_mode) != "legacy":
                raise HTTPException(422, "过肩机位使用主体表演，请选择已验证表演方式")
    timeline = project.audio_timeline
    if timeline and not audio_inputs_match(project, pronunciation):
        raise HTTPException(409, "台词或声线已改变，请重新生成配音")
