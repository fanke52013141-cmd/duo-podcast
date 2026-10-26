"""CPU-only worker contract and real FFmpeg camera editing; no GPU mocking."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

MODULE = Path(__file__).parents[3] / "scripts/fast-mode/motion_worker.py"
spec = importlib.util.spec_from_file_location("motion_worker_cpu_test", MODULE)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def fixture_request():
    clips, cameras, planned = {}, {}, []
    for camera_id, roles in (("dual", "AB"), ("otsA", "A")):
        cameras[camera_id] = {"mode": "twoShot" if camera_id == "dual" else "overShoulder", "subjectSpeaker": "A"}
        role_plans = {}
        for role in roles:
            clip_id = camera_id + role
            x = 2 if role == "A" else 62
            clips[clip_id] = {"path": "server-owned.mp4", "fileHash": "a"*64, "width": 100, "height": 60,
                "role": role, "cameraId": camera_id, "state": "speaking", "usableStartFrame": 0, "usableEndFrame": 10,
                "frames": 10, "fps": 25, "coordinates": [[x, 2, x+20, 24] for _ in range(10)],
                "bodyRegion": {"x": 0 if role == "A" else .6, "y": 0, "width": .4, "height": 1}}
            role_plans[role] = [{"outputStartFrame": 0, "outputEndFrame": 10, "direction": 1,
                "clipId": clip_id, "fileHash": "a"*64, "sourceStartFrame": 0, "sourceEndFrame": 10,
                "sourceFrames": list(range(10)), "state": "speaking"}]
        planned.append({"cameraId": camera_id, "roles": role_plans})
    return {"sampleCount": 6400, "sampleRate": 16000,
        "motionAssets": {"formatVersion": 1, "packId": "pack", "packVersion": "v1", "clips": clips, "cameras": cameras,
            "plan": {"fps": 25, "totalFrames": 10, "sampleRate": 16000, "totalSamples": 6400, "cameras": planned}},
        "cameraEditPlan": [{"cameraId": "dual", "startFrame": 0, "endFrame": 5},
                           {"cameraId": "otsA", "startFrame": 5, "endFrame": 10}]}


def test_native_full_scene_geometry_and_visible_ots_subject():
    request = fixture_request()
    expanded, geometry = worker.validate_request(request)
    assert set(expanded["dual"]) == {"A", "B"}
    assert set(expanded["otsA"]) == {"A"}
    assert geometry["dual"]["native"] == (100, 60, 25)
    assert geometry["dual"]["bodyBoxes"]["B"] == (60, 0, 100, 60)


@pytest.mark.parametrize("mutation", [
    lambda r: r["motionAssets"]["clips"]["dualB"].update(bodyRegion={"x": .3, "y": 0, "width": .7, "height": 1}),
    lambda r: r["motionAssets"]["clips"]["dualA"].pop("bodyRegion"),
    lambda r: r["motionAssets"]["clips"]["dualB"].update(width=120),
    lambda r: r["motionAssets"]["clips"]["dualB"].update(fps=30),
    lambda r: r["motionAssets"]["clips"]["dualA"].update(role="B"),
    lambda r: r["motionAssets"]["clips"]["dualA"].update(usableStartFrame=1),
    lambda r: r["motionAssets"]["clips"]["dualA"]["coordinates"].__setitem__(0, [50, 0, 70, 20]),
    lambda r: r["motionAssets"]["plan"]["cameras"][0]["roles"]["A"][0]["sourceFrames"].__setitem__(0, True),
    lambda r: r["motionAssets"]["plan"]["cameras"][0]["roles"]["A"][0]["sourceFrames"].__setitem__(0, 10),
    lambda r: r["cameraEditPlan"][1].update(startFrame=6),
    lambda r: r["cameraEditPlan"][1].update(startFrame=4),
    lambda r: r["cameraEditPlan"][1].update(cameraId="missing"),
    lambda r: r["motionAssets"]["plan"].update(totalFrames=11),
    lambda r: r["motionAssets"]["plan"]["cameras"][1]["roles"].update(B=[]),
])
def test_invalid_or_ambiguous_motion_inputs_fail_before_gpu(mutation):
    request = fixture_request()
    mutation(request)
    with pytest.raises((ValueError, KeyError)):
        worker.validate_request(request)


def test_two_shot_composes_body_patch_without_doubling_scene():
    np = pytest.importorskip("numpy")
    a = np.zeros((60, 100, 3), dtype=np.uint8)
    b = np.full_like(a, 200)
    frame = worker.compose_frame({"A": a, "B": b}, {"A": (0, 0, 40, 60), "B": (60, 0, 100, 60)})
    assert frame.shape == a.shape
    assert not frame[:, :60].any() and (frame[:, 60:] == 200).all()
    assert not a.any()


def test_real_ffmpeg_camera_edit_uses_full_frame_sources(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real FFmpeg tools required")
    sources = {}
    for camera_id, color in (("dual", "red"), ("otsA", "blue")):
        target = tmp_path / (camera_id + ".mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:size=100x60:rate=25",
            "-t", "0.4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)], check=True)
        sources[camera_id] = target
    audio = tmp_path / "master.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "0.4", str(audio)], check=True)
    request = fixture_request()
    request.update(master=str(audio), resolution="854x480", fps=25)
    worker._edit(request, sources, tmp_path, {"crf": 18, "filter": ""})
    data = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,nb_read_frames", "-of", "json", str(tmp_path / "final.mp4")]))["streams"][0]
    assert (data["width"], data["height"], int(data["nb_read_frames"])) == (854, 480, 10)
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(tmp_path / "final.mp4"), "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
    assert raw[0] > raw[2]  # first camera is red
    assert raw[-1] > raw[-3]  # last camera is blue
