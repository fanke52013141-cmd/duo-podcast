"""Static expert V2V graph construction; no provider or fake completion path."""
from __future__ import annotations

from copy import deepcopy
import re


def _owned_name(value, extensions):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}", value)
            or ".." in value or value.rsplit(".", 1)[-1].lower() not in extensions):
        raise ValueError("Expected server-owned input basename with supported extension")
    return value


def _prune(workflow, output):
    required, active = set(), set()

    def visit(id):
        if id in active:
            raise ValueError("Expert workflow contains a cycle")
        if id in required:
            return
        if id not in workflow:
            raise ValueError("Expert workflow references a missing node")
        active.add(id)
        for value in workflow[id]["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int:
                visit(value[0])
        active.remove(id)
        required.add(id)

    visit(output)
    return {id: node for id, node in workflow.items() if id in required}


def build_expert_motion_workflow(template, segment, input_names, *, output_prefix,
                                  steps=4, start_step=2, prompt_notes=""):
    """Connect real reference latents in the non-long-loop sampler path.

    The installed long MultiTalk loop bypasses sampler.samples. Changing its
    mode to 'auto' does not fix that: this builder replaces the image-embed node
    type and keeps the independent audio conditioning connection instead.
    Runtime/model compatibility and motion retention remain explicitly unknown.
    """
    if type(steps) is not int or not 2 <= steps <= 60 or type(start_step) is not int or not 0 < start_step < steps:
        raise ValueError("Expert V2V needs a bounded partial-denoise step interval")
    if not isinstance(prompt_notes, str) or len(prompt_notes) > 6000:
        raise ValueError("Expert prompt notes exceed limit")
    if not isinstance(output_prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", output_prefix):
        raise ValueError("Expected safe server output prefix")
    if not isinstance(input_names, dict) or set(input_names) != {"anchorImage", "referenceVideo", "masterAudio", "roleAudio", "roleMasks"}:
        raise ValueError("Explicit owned input names required")
    geometry = segment["geometry"]
    roles = set(geometry["visibleRoles"])
    if geometry["mode"] == "twoShot":
        if roles != {"A", "B"}:
            raise ValueError("Two-shot expert audio must bind both subjects")
    elif geometry["mode"] != "overShoulder" or len(roles) != 1 or not roles <= {"A", "B"}:
        raise ValueError("Over-shoulder expert audio must bind exactly one visible subject")
    if set(input_names["roleAudio"]) != roles or set(input_names["roleMasks"]) != roles:
        raise ValueError("Audio/masks must bind only the actual visible roles")
    anchor = _owned_name(input_names["anchorImage"], {"png", "jpg", "jpeg"})
    reference = _owned_name(input_names["referenceVideo"], {"mp4"})
    master = _owned_name(input_names["masterAudio"], {"wav"})
    audio = {role: _owned_name(name, {"wav"}) for role, name in input_names["roleAudio"].items()}
    masks = {role: _owned_name(name, {"png"}) for role, name in input_names["roleMasks"].items()}
    model_frames = segment["modelFrames"]
    if type(model_frames) is not int or not 1 <= model_frames <= 81 or (model_frames - 1) % 4:
        raise ValueError("Expert model window must be 4n+1 and at most 81 frames")
    seed = segment["seed"]
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("Invalid frozen expert seed")
    workflow = deepcopy(template)
    expected = {"122": "WanVideoModelLoader", "129": "WanVideoVAELoader", "133": "LoadImage",
                "135": "WanVideoTextEncodeCached", "198": "MultiTalkWav2VecEmbeds",
                "199": "WanVideoSampler", "201": "WanVideoDecode", "229": "VHS_VideoCombine"}
    if any(workflow.get(id, {}).get("class_type") != type for id, type in expected.items()):
        raise ValueError("Unsupported expert workflow template")
    if not workflow["122"]["inputs"].get("multitalk_model"):
        raise ValueError("Expert video requires the installed audio-conditioned model")
    workflow["133"]["inputs"]["image"] = anchor
    workflow["192"] = {"class_type": "WanVideoImageToVideoEncode", "inputs": {
        "vae": ["129", 0], "width": ["213", 3], "height": ["213", 4],
        "num_frames": model_frames, "noise_aug_strength": 0.0,
        "start_latent_strength": 1.0, "end_latent_strength": 1.0,
        "force_offload": True, "start_image": ["213", 0], "clip_embeds": ["193", 0],
        "fun_or_fl2v_model": False, "tiled_vae": True}}
    workflow["expert_reference"] = {"class_type": "VHS_LoadVideo", "inputs": {
        "video": reference, "force_rate": 25, "custom_width": ["213", 3],
        "custom_height": ["213", 4], "frame_load_cap": model_frames,
        "skip_first_frames": 0, "select_every_nth": 1, "format": "None"}}
    workflow["expert_reference_latents"] = {"class_type": "WanVideoEncode", "inputs": {
        "vae": ["129", 0], "image": ["expert_reference", 0], "enable_vae_tiling": True,
        "tile_x": 272, "tile_y": 272, "tile_stride_x": 144, "tile_stride_y": 128,
        "noise_aug_strength": 0.0, "latent_strength": 1.0}}
    workflow["199"]["inputs"].update({"samples": ["expert_reference_latents", 0],
        "image_embeds": ["192", 0], "multitalk_embeds": ["198", 0],
        "steps": steps, "start_step": start_step, "end_step": -1,
        "denoise_strength": 1.0, "add_noise_to_samples": True, "seed": seed,
        "force_offload": True})
    workflow["198"]["inputs"].update({"num_frames": model_frames, "fps": 25})
    workflow["223"] = {"class_type": "MathExpression|pysssss", "inputs": {"expression": str(model_frames)}}
    for role in roles:
        workflow["218" if role == "A" else "241"]["inputs"]["audio"] = audio[role]
        workflow["250" if role == "A" else "252"]["inputs"]["image"] = masks[role]
    if len(roles) == 1:
        role = next(iter(roles))
        workflow["198"]["inputs"]["audio_1"] = ["218" if role == "A" else "241", 0]
        workflow["198"]["inputs"].pop("audio_2", None)
        workflow["198"]["inputs"]["ref_target_masks"] = ["251" if role == "A" else "253", 0]
    else:
        workflow["198"]["inputs"].update({"audio_1": ["218", 0], "audio_2": ["241", 0],
                                             "ref_target_masks": ["256", 0]})
    workflow["242"]["inputs"]["audio"] = master
    workflow["201"]["inputs"]["samples"] = ["199", 0]
    workflow["229"]["inputs"].update({"images": ["201", 0], "audio": ["242", 0],
        "filename_prefix": "expert_motion/" + output_prefix, "frame_rate": 25, "trim_to_audio": False})
    workflow["135"]["inputs"]["positive_prompt"] = (
        "Fixed-camera podcast shot. Follow the supplied reference video's structural movements and timing. "
        "Preserve exact reference subjects, object construction, identities, visible eyes, nose and mouth, "
        "microphones and background. Apply synchronized speech only to the audio-bound visible subjects. "
        "Do not invent bodies, limbs, clothing, new faces, camera movement or cuts. "
        + ("Preserve the foreground subject as a back view, its face hidden. " if len(roles) == 1 else "Keep both subjects in frame. ")
        + prompt_notes)
    workflow = _prune(workflow, "229")
    trace = {"basis": "static_workflow_graph_only", "packId": segment["packId"],
        "packVersion": segment["packVersion"], "cameraId": segment["cameraId"],
        "segmentDependencyHash": segment["dependencyHash"], "visibleAudioRoles": sorted(roles),
        "imageEmbedNodeType": "WanVideoImageToVideoEncode", "longLoopEnabled": False,
        "referenceLatentPath": ["expert_reference", "expert_reference_latents", "199.samples"],
        "audioConditioningPath": ["198", "199.multitalk_embeds"],
        "decodedVideoPath": ["199", "201", "229"], "modelFrames": model_frames,
        "trimStartFrame": segment["trimStartFrame"], "trimEndFrame": segment["trimEndFrame"],
        "referenceRequiresSafeAudioTrack": True, "runtimeCompatibilityVerified": False,
        "referenceMotionRetentionVerified": False, "lipSyncVerified": False}
    return {"workflow": workflow, "controlTrace": trace}
