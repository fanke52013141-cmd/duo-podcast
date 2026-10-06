"""Explicit experimental profiles; the accepted generator stays unchanged."""
from pathlib import Path
import math
from fastapi import HTTPException
from .render_inputs import resolve_visual_id

PROFILES = {"accepted", "matched", "reference", "listener", "dialogue"}


def validate_video_stream(stream, duration, fps):
    if abs(float(stream.get("duration", 0))-duration) > 1/fps+0.001 or int(stream.get("nb_frames", 0)) != round(duration*fps):
        raise ValueError("成片画面时长或帧数不完整")


def validate_motion_reference(project, payload, artifacts):
    profile = payload.get("generationProfile", "accepted")
    reference_id = payload.get("motionReferenceArtifactId")
    if profile not in {"reference", "listener"}:
        if reference_id:
            raise HTTPException(422, "动作参考需要选择 reference 生成配置")
        return
    reference = artifacts.by_id(reference_id) if isinstance(reference_id, str) else None
    params = (reference or {}).get("paramsSnapshot", {})
    if not reference or reference.get("kind") != "video" or params.get("purpose") != "motionReference" or params.get("projectId") != project.id:
        raise HTTPException(422, "请选择本工程登记的动作参考视频")
    if not Path(reference["path"]).is_file() or artifacts._hash_file(reference["path"]) != reference.get("fileHash"):
        raise HTTPException(409, "动作参考文件缺失或已改变")
    visual_id = resolve_visual_id(project, payload)
    visual = next((v for v in project.visual_variants if v.id == visual_id), None)
    if not visual or visual.master_image.artifact_id != params.get("visualArtifactId"):
        raise HTTPException(409, "动作参考与当前角色画面不一致")
    start = payload.get("redoFromSec")
    timeline = project.audio_timeline
    if not timeline or timeline.sample_rate <= 0:
        raise HTTPException(422, "请先建立有效音轨")
    total = timeline.sample_count / timeline.sample_rate
    reference_end = params.get("rangeEndSec")
    if type(reference_end) not in (int, float) or not math.isfinite(reference_end) or start is None or start != params.get("rangeStartSec") or abs(total-reference_end) > 0.001 or not 0 < total-start <= 5:
        raise HTTPException(422, "当前动作参考仅用于匹配的末段，须指定对应重做起点")
    if profile == "listener":
        if not payload.get("reuseMotionArtifactId"):
            raise HTTPException(422, "分层倾听需要保留原说话者动画")
        listener = params.get("listenerSpeaker")
        revision = next(r for r in project.script_revisions if r.id == timeline.revision_id)
        speakers = {t.id:t.speaker for t in revision.turns}
        active = {speakers.get(u.turn_id) for u in timeline.units
                  if timeline.unit_offsets[u.id]/timeline.sample_rate < total
                  and (timeline.unit_offsets[u.id]+u.sample_count)/timeline.sample_rate > start}
        if listener not in {"A", "B"} or active != ({"B"} if listener == "A" else {"A"}):
            raise HTTPException(422, "该片段存在倾听者发言或角色交接，不能覆盖其画面")
        try:
            listener_layer_filter(listener, params.get("blendBand"),
                                  visual.person_regions)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "动作参考缺少有效的角色合成区域")


def listener_layer_filter(listener, band, regions):
    if not isinstance(band, list) or len(band) != 2 or any(type(v) not in (float,int) or not math.isfinite(v) for v in band):
        raise ValueError("无效合成过渡区域")
    lo, hi = band
    if not 0 < lo < hi < 1 or hi-lo > 0.1:
        raise ValueError("合成区域越界")
    def center(s): return regions[s]["x"] + regions[s]["width"]/2
    other = 'B' if listener == 'A' else 'A'
    left = center(listener) < center(other)
    if not min(center(listener),center(other)) < lo < hi < max(center(listener),center(other)):
        raise ValueError("合成区域穿过人物中心")
    reference, original = ("A","B") if left else ("B","A")
    # X/W is required: chroma planes have half the luma width in YUV420.
    expression = (f"if(lt(X/W,{lo:.10f}),{reference},if(lt(X/W,{hi:.10f}),"
                  f"{reference}*({hi:.10f}-X/W)/{hi-lo:.10f}+"
                  f"{original}*(X/W-{lo:.10f})/{hi-lo:.10f},{original}))")
    return "[0:v]fps=25,setpts=PTS-STARTPTS[r];[1:v]setpts=PTS-STARTPTS[b];[r][b]blend=shortest=1:all_expr='"+expression+"'[v]"


def configure_generation_profile(workflow, profile, reference_filename=None):
    if profile not in PROFILES:
        raise ValueError("未知生成配置")
    if profile in {"listener", "dialogue"}:
        raise ValueError("倾听合成配置不直接使用说话模型采样")
    if profile == "accepted":
        return
    workflow["192"]["inputs"]["mode"] = "auto"
    if profile != "reference":
        return
    if not reference_filename or Path(reference_filename).name != reference_filename:
        raise ValueError("缺少安全的动作参考输入文件名")
    workflow["motion_reference"] = {"class_type": "VHS_LoadVideo", "inputs": {
        "video": reference_filename, "force_rate": 25, "custom_width": ["213", 3],
        "custom_height": ["213", 4], "frame_load_cap": ["223", 0],
        "skip_first_frames": 0, "select_every_nth": 1, "format": "None"}}
    workflow["motion_latents"] = {"class_type": "WanVideoEncode", "inputs": {
        "vae": ["129", 0], "image": ["motion_reference", 0], "enable_vae_tiling": True,
        "tile_x": 272, "tile_y": 272, "tile_stride_x": 144, "tile_stride_y": 128,
        "noise_aug_strength": 0.0, "latent_strength": 1.0}}
    workflow["199"]["inputs"].update({"samples": ["motion_latents", 0],
        "start_step": 2, "add_noise_to_samples": True})
