"""Real lossless media controls: body motion must not mean lips, LEDs or camera."""
import hashlib
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from duocast.services.motion_quality import inspect_motion_clip


def rect(x, y, width, height):
    return {"x": x, "y": y, "width": width, "height": height}


BODY = [rect(0, 0, .5, 1)]
FACE = rect(12 / 96, 8 / 54, 16 / 96, 14 / 54)
LED = rect(30 / 96, 26 / 54, 10 / 96, 8 / 54)
BACKGROUND = rect(.6, 0, .4, 1)
OPTIONS = {"body_regions": BODY, "exclusion_regions": [FACE, LED], "background_region": BACKGROUND}


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real FFmpeg and FFprobe are required for media acceptance controls")
    root = tmp_path_factory.mktemp("motion_quality")
    rng = random.Random(1026)
    texture = bytes(rng.randrange(30, 220) for _ in range(128 * 54))
    paths = {}
    for kind in ("static", "face", "led", "camera", "object", "camera_and_object", "pause_then_object"):
        frames = []
        for index in range(20):
            offset = index if kind in {"camera", "camera_and_object"} else 0
            image = bytearray(b"".join(texture[y * 128 + offset:y * 128 + offset + 96] for y in range(54)))
            if kind == "face":
                for y in range(8, 22):
                    image[y * 96 + 12:y * 96 + 28] = bytes([50 if index % 2 else 230]) * 16
            if kind == "led":
                for y in range(26, 34):
                    image[y * 96 + 30:y * 96 + 40] = bytes([20 if index % 2 else 250]) * 10
            if kind in {"object", "camera_and_object", "pause_then_object"}:
                x = 3 + (max(0, index - 12) if kind == "pause_then_object" else index)
                for y in range(35, 48):
                    image[y * 96 + x:y * 96 + x + 12] = bytes([250]) * 12
            frames.append(bytes(image))
        target = root / (kind + ".mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pixel_format", "gray",
                        "-video_size", "96x54", "-framerate", "10", "-i", "pipe:0", "-an",
                        "-c:v", "ffv1", "-pix_fmt", "gray", str(target)],
                       input=b"".join(frames), check=True, capture_output=True)
        paths[kind] = target
    return paths


@pytest.mark.parametrize("kind,code", [("static", "STATIC_SOURCE"), ("face", "NO_STRUCTURAL_MOTION"),
                                      ("led", "NO_STRUCTURAL_MOTION"), ("camera", "NO_STRUCTURAL_MOTION")])
def test_rejects_real_negative_media_controls(videos, kind, code):
    report = inspect_motion_clip(videos[kind], **OPTIONS)
    assert report["status"] == "failed", report
    assert code in report["hardFailures"], report
    assert report["native"]["frames"] == 20
    assert report["evidence"]["annotations"] == {
        "bodyRegions": BODY, "exclusionRegions": [FACE, LED], "backgroundRegion": BACKGROUND}
    assert report["inputFileHash"] == hashlib.sha256(videos[kind].read_bytes()).hexdigest()
    assert all(0 <= issue["startFrame"] <= issue["endFrame"] <= 20 for issue in report["issues"])


@pytest.mark.parametrize("kind", ["object", "camera_and_object"])
def test_actual_structural_motion_remains_candidate_for_human_review(videos, kind):
    report = inspect_motion_clip(videos[kind], expected_frames=20, expected_fps=10, **OPTIONS)
    assert report["hardFailures"] == [], report
    assert report["status"] == "needs_review"
    assert report["evidence"]["structuralMotionDetected"] is True
    assert report["evidence"]["differenceUnits"] == "mean_absolute_gray_level_0_255"
    assert report["evidence"]["faceDetection"] == {"performed": False, "coverage": None}
    assert report["evidence"]["fullFeatureVisibilityVerified"] is False
    assert report["evidence"]["humanApproval"] is False
    assert "VISUAL_REVIEW_REQUIRED" in [issue["code"] for issue in report["issues"]]
    if kind == "camera_and_object":
        assert report["metrics"]["translatedSamplePairs"] > 0


def test_missing_regions_never_claims_structural_or_face_pass(videos):
    report = inspect_motion_clip(videos["object"])
    assert report["status"] == "needs_review"
    assert "REGIONS_REQUIRED" in [i["code"] for i in report["issues"]]
    assert "structuralMotionDetected" not in report["evidence"]


def test_quiet_intervals_have_native_frame_locations_and_do_not_reject_listening(videos):
    report = inspect_motion_clip(videos["pause_then_object"], **OPTIONS)
    assert report["hardFailures"] == [], report
    quiet = [i for i in report["issues"] if i["code"] == "QUIET_STRUCTURAL_INTERVAL"]
    assert len(quiet) == 1
    assert quiet[0]["startFrame"] == 0
    assert 12 <= quiet[0]["endFrame"] <= 14
    assert quiet[0]["severity"] == "review"


@pytest.mark.parametrize("region", [rect(-.1, 0, .5, 1), rect(0, 0, 0, 1), rect(0, 0, 2, 1),
                                    rect(True, 0, .5, 1), rect(float("nan"), 0, .5, 1), {"x": 0}])
def test_rejects_invalid_roi_without_running_media_tools(videos, monkeypatch, region):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid ROI must be rejected before starting a tool")
    monkeypatch.setattr("duocast.services.motion_quality.subprocess.run", unexpected)
    result = inspect_motion_clip(videos["object"], body_regions=[region])
    assert result["status"] == "failed"
    assert "INVALID_INPUT" in result["hardFailures"]


def test_hash_mismatch_blocks_analysis(videos, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Tampered input must not be decoded")
    monkeypatch.setattr("duocast.services.motion_quality.subprocess.run", unexpected)
    result = inspect_motion_clip(videos["object"], expected_file_hash="0" * 64, **OPTIONS)
    assert result["hardFailures"] == ["HASH_MISMATCH"]


def test_expected_timeline_is_verified_against_decoded_media(videos):
    report = inspect_motion_clip(videos["object"], expected_frames=19, expected_fps=25, **OPTIONS)
    assert {"FRAME_COUNT_MISMATCH", "FPS_MISMATCH"}.issubset(report["hardFailures"])
    assert report["status"] == "failed"


def test_background_and_body_need_independent_pixels(videos):
    result = inspect_motion_clip(videos["object"], body_regions=[rect(0, 0, 1, 1)],
                                 background_region=BACKGROUND)
    assert "INVALID_BACKGROUND_REGION" in result["hardFailures"]
    result = inspect_motion_clip(videos["object"], body_regions=BODY,
                                 exclusion_regions=[rect(0, 0, .5, 1)], background_region=BACKGROUND)
    assert "INVALID_BODY_REGION" in result["hardFailures"]


@pytest.mark.parametrize("error,code", [(FileNotFoundError(), "ANALYZER_UNAVAILABLE"),
                                       (subprocess.TimeoutExpired("ffprobe", 120), "ANALYSIS_TIMEOUT")])
def test_unavailable_analyzer_does_not_fabricate_success(videos, monkeypatch, error, code):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr("duocast.services.motion_quality.subprocess.run", fail)
    result = inspect_motion_clip(videos["object"], **OPTIONS)
    assert result["status"] == "failed"
    assert result["hardFailures"] == [code]
    assert code in [issue["code"] for issue in result["issues"]]
    assert result["evidence"]["faceDetection"]["coverage"] is None


def test_corrupt_video_is_a_hard_failure(tmp_path):
    path = tmp_path / "corrupt.mp4"
    path.write_bytes(b"not a video")
    assert "DECODE_FAILED" in inspect_motion_clip(path)["hardFailures"]


def test_selected_static_range_cannot_borrow_later_motion(videos):
    report = inspect_motion_clip(videos["pause_then_object"], **OPTIONS,
                                 usable_start_frame=0, usable_end_frame=10)
    assert report["status"] == "failed"
    assert "STATIC_SOURCE" in report["hardFailures"]
    assert report["native"]["frames"] == 20
    assert report["evidence"]["analyzedEndFrame"] == 10
    assert all(issue["endFrame"] <= 10 for issue in report["issues"])


def test_selected_moving_range_preserves_source_coordinates(videos):
    report = inspect_motion_clip(videos["pause_then_object"], **OPTIONS,
                                 usable_start_frame=12, usable_end_frame=20)
    assert report["hardFailures"] == [], report
    assert report["evidence"]["structuralMotionDetected"]
    assert report["evidence"]["analyzedStartFrame"] == 12
    assert all(12 <= issue["startFrame"] <= issue["endFrame"] <= 20 for issue in report["issues"])


def test_decode_timeout_after_probe_still_blocks_adoption(videos, monkeypatch):
    original = subprocess.run
    def fail_decode(command, **kwargs):
        if command[0] == "ffmpeg":
            raise subprocess.TimeoutExpired(command, 120)
        return original(command, **kwargs)
    monkeypatch.setattr("duocast.services.motion_quality.subprocess.run", fail_decode)
    result = inspect_motion_clip(videos["object"], **OPTIONS)
    assert result["native"]["frames"] == 20
    assert result["status"] == "failed"
    assert "ANALYSIS_TIMEOUT" in result["hardFailures"]
    assert "completeSourceFramesDecoded" not in result["evidence"]


@pytest.mark.parametrize("start,end", [(False, 10), (-1, 10), (10, 10), (0, 21)])
def test_invalid_usable_ranges_block(videos, start, end):
    result = inspect_motion_clip(videos["object"], **OPTIONS,
                                 usable_start_frame=start, usable_end_frame=end)
    assert "INVALID_INPUT" in result["hardFailures"]


def test_unicode_source_and_no_exclusion_claim(videos, tmp_path):
    source = tmp_path / "台灯.mkv"
    shutil.copy2(videos["object"], source)
    result = inspect_motion_clip(source, body_regions=BODY, background_region=BACKGROUND)
    assert result["hardFailures"] == []
    assert result["status"] == "needs_review"
    assert "EXCLUSIONS_UNVERIFIED" in [issue["code"] for issue in result["issues"]]


def test_cli_outputs_a_real_failed_control_report(videos, tmp_path):
    import sys
    script = Path(__file__).resolve().parents[3] / "scripts/fast-mode/check_motion.py"
    destination = tmp_path / "report.json"
    process = subprocess.run([sys.executable, str(script), str(videos["static"]), "--out", str(destination)],
                             capture_output=True)
    assert process.returncode == 2, process.stderr.decode("utf-8", "replace")
    report = json.loads(destination.read_text(encoding="utf-8"))
    assert "STATIC_SOURCE" in report["hardFailures"]
    assert json.loads(process.stdout)["inputFileHash"] == report["inputFileHash"]


def test_source_changes_during_decode_discard_the_report(videos, tmp_path, monkeypatch):
    source = tmp_path / "changing.mkv"
    shutil.copy2(videos["object"], source)
    original_run = subprocess.run
    def change_after_decode(args, **kwargs):
        result = original_run(args, **kwargs)
        if args[0] == "ffmpeg":
            with source.open("ab") as writer:
                writer.write(b"changed while analyzing")
        return result
    monkeypatch.setattr("duocast.services.motion_quality.subprocess.run", change_after_decode)
    report = inspect_motion_clip(source, **OPTIONS)
    assert report["status"] == "failed"
    assert "MEDIA_CHANGED_DURING_ANALYSIS" in report["hardFailures"]
