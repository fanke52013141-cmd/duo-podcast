"""Bounded, CPU-only motion evidence. This never substitutes for human review.

Frame differences are grayscale levels (0..255), not displacement pixels. Body
regions must exclude faces, lamps and overlays. Background alignment removes a
small common translation before structural changes are measured.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

CHECKS_VERSION = "motion-quality-v1"
WIDTH, HEIGHT = 96, 54
MAX_ANALYZED_FRAMES = 600
VIDEO_SUFFIXES = frozenset({".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"})
FORMATS = "mov,matroska,webm,avi"


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _region(value: dict[str, Any]) -> tuple[float, float, float, float]:
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        raise ValueError("Regions require x, y, width and height")
    parts = tuple(value[key] for key in ("x", "y", "width", "height"))
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in parts):
        raise ValueError("Region coordinates must be finite numbers")
    x, y, width, height = parts
    if not (0 <= x < x + width <= 1 and 0 <= y < y + height <= 1):
        raise ValueError("Region lies outside the image")
    return parts


def _pixels(rect: tuple[float, float, float, float]) -> set[int]:
    x, y, width, height = rect
    return {row * WIDTH + col
            for row in range(math.floor(y * HEIGHT), min(HEIGHT, math.ceil((y + height) * HEIGHT)))
            for col in range(math.floor(x * WIDTH), min(WIDTH, math.ceil((x + width) * WIDTH)))}


def _difference(previous: bytes, current: bytes, pixels: list[int], shift=(0, 0)) -> float:
    dx, dy = shift
    total, count = 0, 0
    for index in pixels:
        row, col = divmod(index, WIDTH)
        if 0 <= col + dx < WIDTH and 0 <= row + dy < HEIGHT:
            total += abs(current[index] - previous[index + dy * WIDTH + dx])
            count += 1
    return total / count if count else 0.0


def _translation(previous: bytes, current: bytes, pixels: list[int]) -> tuple[int, int]:
    # At most ~200 background pixels * 81 candidates per sampled frame pair.
    sampled = pixels[::max(1, len(pixels) // 200)]
    candidates = ((dx, dy) for dy in range(-4, 5) for dx in range(-4, 5))
    return min(candidates, key=lambda shift: (
        _difference(previous, current, sampled, shift), abs(shift[0]) + abs(shift[1])))


def _percentile(values: list[float], proportion: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.floor(proportion * (len(ordered) - 1)))]


def inspect_motion_clip(
    path: Path, *, body_regions: list[dict] | None = None,
    exclusion_regions: list[dict] | None = None, background_region: dict | None = None,
    expected_file_hash: str | None = None, expected_frames: int | None = None,
    expected_fps: float | None = None, sample_fps: float = 10,
    max_duration_sec: float = 300,
    usable_start_frame: int = 0, usable_end_frame: int | None = None,
) -> dict[str, Any]:
    """Inspect an actual local video with bounded FFmpeg decoding and no ML deps.

    A positive report is ``needs_review``: eye/nose/mouth visibility, object
    identity, structural plausibility and naturalness remain unverified. Region
    annotations are caller-provided, never represented as detected face boxes.
    Missing tools or incomplete decoding blocks adoption.
    """
    report: dict[str, Any] = {
        "formatVersion": 1, "inputFileHash": "", "checksVersion": CHECKS_VERSION,
        "status": "needs_review", "hardFailures": [], "issues": [],
        "metrics": {}, "native": {}, "evidence": {
            "method": "sampled_gray_difference_background_translation_v1",
            "differenceUnits": "mean_absolute_gray_level_0_255",
            "analysisSize": [WIDTH, HEIGHT], "regionsSource": "caller_annotation",
            "faceDetection": {"performed": False, "coverage": None},
            "fullFeatureVisibilityVerified": False, "identityVerified": False,
            "humanApproval": False,
        },
    }
    source_frames = 0
    range_start, range_end = 0, None

    def issue(code: str, message: str, hard=False, start=None, end=None):
        report["issues"].append({"code": code, "severity": "error" if hard else "review",
                                 "startFrame": range_start if start is None else start,
                                 "endFrame": (range_end or source_frames) if end is None else end,
                                 "message": message})
        if hard:
            report["hardFailures"].append(code)
            report["status"] = "failed"

    try:
        if (type(sample_fps) not in (int, float) or not math.isfinite(sample_fps) or not 0 < sample_fps <= 25
                or type(max_duration_sec) not in (int, float) or not math.isfinite(max_duration_sec)
                or not 0 < max_duration_sec <= 300):
            raise ValueError("Sample fps must be 0..25 and duration limit 0..300")
        if expected_frames is not None and (type(expected_frames) is not int or expected_frames <= 0):
            raise ValueError("Expected frames must be a positive integer")
        if expected_fps is not None and (type(expected_fps) not in (int, float)
                                         or not math.isfinite(expected_fps) or expected_fps <= 0):
            raise ValueError("Expected fps must be a positive finite number")
        bodies = [_region(r) for r in (body_regions or [])]
        exclusions = [_region(r) for r in (exclusion_regions or [])]
        background = _region(background_region) if background_region is not None else None
        report["evidence"]["annotations"] = {
            "bodyRegions": body_regions or [], "exclusionRegions": exclusion_regions or [],
            "backgroundRegion": background_region}
        source = Path(path).resolve()
        if source.suffix.lower() not in VIDEO_SUFFIXES or not source.is_file():
            issue("INVALID_MEDIA", "A supported, existing local video is required", True)
            return report
        report["inputFileHash"] = _hash(source)
        if expected_file_hash is not None and expected_file_hash != report["inputFileHash"]:
            issue("HASH_MISMATCH", "The media content changed after registration", True)
            return report
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", FORMATS, "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=width,height,avg_frame_rate,nb_frames,nb_read_frames,duration:format=duration",
            "-of", "json", str(source),
        ], capture_output=True, check=True, timeout=120)
        data = json.loads(probe.stdout)
        video = data.get("streams", [])[0]
        duration = float(video.get("duration") or data["format"]["duration"])
        fps = float(Fraction(video["avg_frame_rate"]))
        source_frames = int(video["nb_read_frames"])
        if not math.isfinite(duration) or not 0 < duration <= max_duration_sec or fps <= 0 or source_frames <= 0:
            issue("INVALID_TIMELINE", "Video duration, frame rate or frame count is outside limits", True)
            return report
        report["native"] = {"width": int(video["width"]), "height": int(video["height"]),
                            "fps": fps, "frames": source_frames, "durationSec": duration}
        range_end = source_frames if usable_end_frame is None else usable_end_frame
        if (type(usable_start_frame) is not int or type(range_end) is not int
                or not 0 <= usable_start_frame < range_end <= source_frames):
            raise ValueError("Usable range must be nonempty and inside decoded source frames")
        range_start = usable_start_frame
        range_duration = (range_end - range_start) / fps
        declared = video.get("nb_frames")
        if declared not in (None, "N/A") and int(declared) != source_frames:
            issue("INCOMPLETE_FRAME_COUNT", "Declared frame count differs from decoded frame count", True)
        if expected_frames is not None and source_frames != expected_frames:
            issue("FRAME_COUNT_MISMATCH", "Decoded frame count differs from the expected source", True)
        if expected_fps is not None and abs(fps - expected_fps) > .001:
            issue("FPS_MISMATCH", "Video frame rate differs from the expected source", True)
        effective_fps = min(float(sample_fps), (MAX_ANALYZED_FRAMES - 1) / range_duration)
        decoded = subprocess.run([
            "ffmpeg", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", FORMATS, "-i", str(source), "-map", "0:v:0", "-an",
            "-vf", f"trim=start_frame={range_start}:end_frame={range_end},setpts=PTS-STARTPTS,fps={effective_fps},scale={WIDTH}:{HEIGHT},format=gray",
            "-frames:v", str(MAX_ANALYZED_FRAMES), "-f", "rawvideo", "pipe:1",
        ], capture_output=True, check=True, timeout=120).stdout
        size = WIDTH * HEIGHT
        if len(decoded) % size or len(decoded) // size < 2:
            issue("INSUFFICIENT_FRAMES", "Fewer than two complete analysis frames were decoded", True)
            return report
        frames = [decoded[i:i + size] for i in range(0, len(decoded), size)]
        report["evidence"].update({"sampleFps": effective_fps, "sampleFrames": len(frames),
                                  "completeSourceFramesDecoded": source_frames,
                                  "analyzedStartFrame": range_start, "analyzedEndFrame": range_end})
        all_pixels = list(range(size))
        full_differences = [_difference(a, b, all_pixels) for a, b in zip(frames, frames[1:])]
        metrics = report["metrics"]
        metrics.update({"uniqueSampleFrames": len(set(frames)),
                        "fullFrameMaxDifference": max(full_differences),
                        "fullFrameP90Difference": _percentile(full_differences, .9)})
        if max(full_differences) <= .02:
            issue("STATIC_SOURCE", "Sampled frames are repeated stills; this is not an object-motion template", True)
        excluded = set().union(*(_pixels(r) for r in exclusions)) if exclusions else set()
        body = set().union(*(_pixels(r) for r in bodies)) if bodies else set()
        body_pixels = sorted(body - excluded)
        bg_pixels = sorted(_pixels(background) - body - excluded) if background else []
        if bodies and len(body_pixels) < 16:
            issue("INVALID_BODY_REGION", "Body regions contain fewer than 16 unmasked analysis pixels", True)
        if background and len(bg_pixels) < 16:
            issue("INVALID_BACKGROUND_REGION", "Background control has fewer than 16 independent pixels", True)
        if not bodies or not background or len(body_pixels) < 16 or len(bg_pixels) < 16:
            issue("REGIONS_REQUIRED", "Structural body, face/LED exclusion and independent background annotations require review")
        else:
            structural, bg_residuals, shifts = [], [], []
            for previous, current in zip(frames, frames[1:]):
                shift = _translation(previous, current, bg_pixels)
                shifts.append(shift)
                structural.append(_difference(previous, current, body_pixels, shift))
                bg_residuals.append(_difference(previous, current, bg_pixels, shift))
            # Percentiles of active pairs preserve a short action in a mostly quiet clip.
            body_score = _percentile([v for v in structural if v > .02], .9)
            bg_score = _percentile([v for v in bg_residuals if v > .02], .9)
            threshold = max(.5, 3 * bg_score)
            metrics.update({"structuralMotionScore": body_score, "backgroundResidualScore": bg_score,
                            "structuralThreshold": threshold, "structuralPixels": len(body_pixels),
                            "backgroundPixels": len(bg_pixels),
                            "translatedSamplePairs": sum(shift != (0, 0) for shift in shifts),
                            "bodyDifferenceMax": max(structural)})
            if body_score <= .02:
                issue("NO_STRUCTURAL_MOTION", "No structural object movement remains after exclusions and background alignment", True)
            elif body_score <= threshold:
                issue("WEAK_STRUCTURAL_EVIDENCE", "Structural movement is weak or not separable from background changes; inspect the clip")
            else:
                report["evidence"]["structuralMotionDetected"] = True
                quiet_start = None
                for index, value in enumerate(structural + [float("inf")]):
                    if value <= .02 and quiet_start is None:
                        quiet_start = index
                    elif value > .02 and quiet_start is not None:
                        if (index - quiet_start) / effective_fps >= 1:
                            issue("QUIET_STRUCTURAL_INTERVAL", "No structural change for at least one second; review whether this is intentional quiet listening",
                                  start=range_start + round(quiet_start / effective_fps * fps),
                                  end=min(range_end, range_start + round((index + 1) / effective_fps * fps)))
                        quiet_start = None
            if not exclusions:
                issue("EXCLUSIONS_UNVERIFIED", "No face/LED/overlay exclusions were supplied; structural attribution is unverified")
        issue("VISUAL_REVIEW_REQUIRED", "Inspect full facial-feature visibility, identity, rigid shape and naturalness; no human approval is inferred")
        if _hash(source) != report["inputFileHash"]:
            issue("MEDIA_CHANGED_DURING_ANALYSIS", "Media changed while it was being inspected; discard the report", True)
    except FileNotFoundError:
        issue("ANALYZER_UNAVAILABLE", "FFmpeg or FFprobe is unavailable; no complete media check was performed", True)
    except subprocess.TimeoutExpired:
        issue("ANALYSIS_TIMEOUT", "Analysis resource limit reached; media remains unverified", True)
    except subprocess.CalledProcessError:
        issue("DECODE_FAILED", "Video probing or strict decoding failed", True)
    except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError, OSError) as exc:
        issue("INVALID_INPUT", str(exc) if isinstance(exc, ValueError) else "Media or analysis input is invalid", True)
    return report
