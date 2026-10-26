"""Build worker inputs exclusively from pinned, registered motion assets."""
import json
from pathlib import Path

from ..domain.motion_pack import MotionPack
from .motion_schedule import build_motion_schedule


def verified_artifact(artifacts, reference, kind=None):
    entry = artifacts.by_id(reference.artifact_id)
    if (not entry or kind is not None and entry.get("kind") != kind
            or entry.get("fileHash") != reference.file_hash):
        raise ValueError("动作素材登记缺失或哈希已改变")
    path = Path(entry["path"])
    if not path.is_file() or artifacts._hash_file(str(path)) != reference.file_hash:
        raise ValueError("动作素材文件缺失或内容已改变")
    return path


def coordinates_for_clip(artifacts, clip):
    if clip.face_coordinates is None:
        raise ValueError(f"片段{clip.id}缺少逐帧人脸坐标，不能自动驱动未经确认的脸")
    path = verified_artifact(artifacts, clip.face_coordinates)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("人脸坐标文件超过限制")
    data = json.loads(path.read_text(encoding="utf-8"))
    # Coordinates bind the exact video and role. A bare face detector result
    # cannot silently stand in for a confirmed subject binding.
    if (not isinstance(data, dict) or data.get("videoFileHash") != clip.media.file_hash
            or data.get("role") != clip.role):
        raise ValueError("人脸坐标与视频或角色不匹配")
    boxes = data.get("boxes")
    if not isinstance(boxes, list) or len(boxes) != clip.frame_count:
        raise ValueError("逐帧人脸坐标不完整")
    for frame in range(clip.usable_start_frame, clip.usable_end_frame):
        box = boxes[frame]
        if (not isinstance(box, list) or len(box) != 4
                or any(type(value) is not int for value in box)):
            raise ValueError("可用区间存在缺失或非法人脸框")
        x1, y1, x2, y2 = box
        if not (0 <= x1 < x2 <= clip.native_width and 0 <= y1 < y2 <= clip.native_height):
            raise ValueError("人脸坐标超出原生画面")
    return boxes


def build_motion_worker_inputs(store, artifacts, pack_id, version, style, turns, *,
                               sample_rate, total_samples, seed="0"):
    ready = store.readiness(pack_id, style, version)
    if not ready["ready"]:
        raise ValueError("动作包未就绪：" + ", ".join(ready["missing"]))
    pack = store.load_version(pack_id, version)
    if not isinstance(pack, MotionPack):
        raise ValueError("固定动作包版本不存在")
    plan = build_motion_schedule(pack, style, turns, sample_rate=sample_rate,
                                 total_samples=total_samples, output_fps=25, seed=str(seed))
    needed = {segment["clipId"] for camera in plan["cameras"]
              for segments in camera["roles"].values() for segment in segments}
    clips = {}
    for clip in pack.clips:
        if clip.id not in needed:
            continue
        clips[clip.id] = {"path": str(verified_artifact(artifacts, clip.media, "video")),
                          "fileHash": clip.media.file_hash,
                          "width": clip.native_width, "height": clip.native_height,
                          "frames": clip.frame_count, "fps": clip.fps,
                          "role": clip.role, "cameraId": clip.camera_id, "state": clip.state,
                          "usableStartFrame": clip.usable_start_frame, "usableEndFrame": clip.usable_end_frame,
                          "bodyRegion": clip.body_region.model_dump(),
                          "coordinates": coordinates_for_clip(artifacts, clip)}
    cameras = {camera.id: {"mode": camera.mode, "subjectSpeaker": camera.subject_speaker,
                           "foregroundSpeaker": camera.foreground_speaker,
                           "subjectRegion": camera.subject_region.model_dump()}
               for camera in pack.cameras if camera.id in ready["cameraIds"]}
    return {"formatVersion": 1, "packId": pack.id, "packVersion": pack.version,
            "style": style, "plan": plan, "clips": clips, "cameras": cameras}


def camera_edit_plan(assets, turns, sample_rate):
    """Choose actual cameras on the same integer sample clock as motion."""
    count, fps = assets["plan"]["totalFrames"], assets["plan"]["fps"]
    cameras, style = assets["cameras"], assets["style"]
    dual = next((id for id, c in cameras.items() if c["mode"] == "twoShot"), None)
    reverse = {c["subjectSpeaker"]: id for id, c in cameras.items() if c["mode"] == "overShoulder"}
    if style == "twoShot":
        if dual is None:
            raise ValueError("同框机位缺失")
        return [{"cameraId": dual, "startFrame": 0, "endFrame": count}]
    if set(reverse) != {"A", "B"} or style == "mixed" and dual is None:
        raise ValueError("真实反打机位不完整")
    # Silence retains the previous view. Mixed uses the shared opening view
    # for two seconds, then switches to the visible speaker's real camera.
    selected = [reverse["A"]] * count
    for turn in turns:
        first = (turn["startSample"] * fps + sample_rate - 1) // sample_rate
        selected[first:] = [reverse[turn["speaker"]]] * (count-first)
    if style == "mixed":
        selected[:min(count, fps * 2)] = [dual] * min(count, fps * 2)
    shots = []
    for frame, camera_id in enumerate(selected):
        if shots and shots[-1]["cameraId"] == camera_id:
            shots[-1]["endFrame"] = frame + 1
        else:
            shots.append({"cameraId": camera_id, "startFrame": frame, "endFrame": frame+1})
    return shots
