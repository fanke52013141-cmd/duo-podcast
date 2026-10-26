"""Typed, artifact-referenced motion packs and readiness policy.

Content and review are separate: a detector report can only nominate a clip for
human review. Physical media integrity is verified by MotionPackStore.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, Field, JsonValue, field_validator, model_validator

from .base import dc_config

Role = Literal["A", "B"]
ShotStyle = Literal["twoShot", "overShoulder", "mixed"]
MotionState = Literal["speaking", "listening", "responding"]


class MotionModel(BaseModel):
    model_config = dc_config(extra="forbid", allow_inf_nan=False)


class MotionMediaRef(MotionModel):
    artifact_id: str = Field(min_length=1, max_length=255)
    file_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class MotionRegion(MotionModel):
    x: float = Field(default=0, ge=0, le=1)
    y: float = Field(default=0, ge=0, le=1)
    width: float = Field(default=1, gt=0, le=1)
    height: float = Field(default=1, gt=0, le=1)

    @model_validator(mode="after")
    def inside_frame(self):
        if self.x + self.width > 1.000000001 or self.y + self.height > 1.000000001:
            raise ValueError("region exceeds frame")
        return self


class MotionCharacter(MotionModel):
    character_id: str = Field(min_length=1, max_length=255)
    name: str = Field(default="", max_length=128)
    object_type: Literal["human", "rigid_object", "articulated_object", "other"] = "human"
    reference_image: MotionMediaRef | None = None
    motion_notes: str = Field(default="", max_length=2000)


class MotionPackCamera(MotionModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    mode: Literal["twoShot", "overShoulder"] = "twoShot"
    master_image: MotionMediaRef | None = None
    subject_speaker: Role | None = None
    foreground_speaker: Role | None = None
    subject_region: MotionRegion = Field(default_factory=MotionRegion)

    @model_validator(mode="after")
    def validate_roles(self):
        if self.mode == "overShoulder":
            if self.subject_speaker is None or self.foreground_speaker is None or self.subject_speaker == self.foreground_speaker:
                raise ValueError("overShoulder requires distinct subject and foreground roles")
        elif self.subject_speaker is not None or self.foreground_speaker is not None:
            raise ValueError("twoShot has both subjects, not a single foreground role")
        return self


class MotionProvenance(MotionModel):
    source_media: MotionMediaRef | None = None
    generator: str = Field(default="imported", max_length=128)
    model_version: str = Field(default="", max_length=255)
    prompt_id: str = Field(default="", max_length=255)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    processing_recipe: list[dict[str, JsonValue]] = Field(default_factory=list)


class MotionClip(MotionModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    role: Role
    camera_id: str = Field(min_length=1, max_length=128)
    state: MotionState
    media: MotionMediaRef
    face_coordinates: MotionMediaRef | None = None
    native_width: int = Field(gt=0, strict=True)
    native_height: int = Field(gt=0, strict=True)
    fps: int = Field(default=25, gt=0, le=120, strict=True)
    frame_count: int = Field(gt=0, strict=True)
    usable_start_frame: int = Field(default=0, ge=0, strict=True)
    usable_end_frame: int = Field(gt=0, strict=True)
    body_region: MotionRegion = Field(default_factory=MotionRegion)
    provenance: MotionProvenance = Field(default_factory=MotionProvenance)

    @model_validator(mode="after")
    def validate_range(self):
        if not self.usable_start_frame < self.usable_end_frame <= self.frame_count:
            raise ValueError("usable frame range must be nonempty and inside the source")
        return self


class MotionPackDraft(MotionModel):
    """Only editable content; status/checks/reviews can never enter through this DTO."""
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    characters: dict[Role, MotionCharacter] = Field(default_factory=dict)
    cameras: list[MotionPackCamera] = Field(default_factory=list)
    clips: list[MotionClip] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def validate_bindings(self):
        camera_ids = {c.id for c in self.cameras}
        if len(camera_ids) != len(self.cameras) or len({c.id for c in self.clips}) != len(self.clips):
            raise ValueError("duplicate camera or clip id")
        cameras = {c.id: c for c in self.cameras}
        for clip in self.clips:
            if clip.role not in self.characters or clip.camera_id not in camera_ids:
                raise ValueError("clip references missing role or camera")
            camera = cameras[clip.camera_id]
            if camera.mode == "overShoulder" and clip.role != camera.subject_speaker:
                raise ValueError("overShoulder clip must belong to the visible subject")
        return self


class MotionQualityReport(MotionModel):
    format_version: int = Field(default=1, ge=1)
    input_file_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    checks_version: str = Field(min_length=1, max_length=128)
    status: Literal["passed", "failed", "needs_review"]
    hard_failures: list[str] = Field(default_factory=list)
    issues: list[dict[str, JsonValue]] = Field(default_factory=list)
    metrics: dict[str, JsonValue] = Field(default_factory=dict)
    native: dict[str, JsonValue] = Field(default_factory=dict)
    evidence: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent_status(self):
        if self.hard_failures and self.status != "failed":
            raise ValueError("hard failures require failed status")
        return self


class MotionCheckRecord(MotionModel):
    content_hash: str
    checked_at: str
    report: MotionQualityReport


class MotionReviewRecord(MotionModel):
    content_hash: str
    accepted: bool
    reviewed_at: str
    reason: str = Field(default="", max_length=2000)


class MotionPack(MotionPackDraft):
    schema_version: Literal[2] = 2
    id: str
    revision: int = Field(ge=1)
    version: str
    created_at: str
    updated_at: str
    checks: dict[str, MotionCheckRecord] = Field(default_factory=dict)
    reviews: dict[str, MotionReviewRecord] = Field(default_factory=dict)


def content_hash(value: MotionModel | dict) -> str:
    data = value.model_dump(mode="json", by_alias=True) if isinstance(value, BaseModel) else value
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def clip_fingerprint(pack: MotionPackDraft, clip: MotionClip) -> str:
    camera = next(c for c in pack.cameras if c.id == clip.camera_id)
    roles = ("A", "B") if camera.mode == "twoShot" else (camera.subject_speaker, camera.foreground_speaker)
    return content_hash({"clip": clip.model_dump(mode="json", by_alias=True),
                         "camera": camera.model_dump(mode="json", by_alias=True),
                         "characters": {s: pack.characters[s].model_dump(mode="json", by_alias=True) for s in roles if s in pack.characters}})


def clip_readiness(pack: MotionPack, clip: MotionClip) -> list[str]:
    fingerprint = clip_fingerprint(pack, clip)
    check, review = pack.checks.get(clip.id), pack.reviews.get(clip.id)
    if not check or check.content_hash != fingerprint or check.report.input_file_hash != clip.media.file_hash:
        return ["missing_or_stale_checks"]
    if check.report.status == "failed" or check.report.hard_failures:
        return ["quality_check_failed"]
    if native_report_errors(clip, check.report):
        return ["native_media_evidence_invalid"]
    if not review or review.content_hash != fingerprint or not review.accepted:
        return ["human_review_required"]
    if check.report.status == "needs_review" and not review.reason.strip():
        return ["review_reason_required"]
    return []


def native_report_errors(clip: MotionClip, report: MotionQualityReport) -> list[str]:
    native = report.native
    if not all(key in native for key in ("width", "height", "fps", "frames", "durationSec")):
        return ["NATIVE_EVIDENCE_MISSING"]
    expected = {"width": clip.native_width, "height": clip.native_height, "frames": clip.frame_count}
    errors = ["NATIVE_SPEC_MISMATCH"] if any(type(native[key]) is not int or native[key] != value for key, value in expected.items()) else []
    for key, expected_value in (("fps", clip.fps), ("durationSec", clip.frame_count / clip.fps)):
        actual = native[key]
        tolerance = 1e-6 if key == "fps" else 1 / clip.fps + .001
        if type(actual) not in (int, float) or not math.isfinite(actual) or abs(actual - expected_value) > tolerance:
            errors.append("NATIVE_SPEC_MISMATCH")
    evidence = report.evidence
    for key, expected_value in (("completeSourceFramesDecoded", clip.frame_count),
                                ("analyzedStartFrame", clip.usable_start_frame),
                                ("analyzedEndFrame", clip.usable_end_frame)):
        if type(evidence.get(key)) is not int or evidence[key] != expected_value:
            errors.append("ANALYSIS_RANGE_MISMATCH")
    return sorted(set(errors))


def readiness(pack: MotionPack, style: ShotStyle) -> dict:
    """Trusted-record readiness; callers must additionally verify physical media."""
    if style not in {"twoShot", "overShoulder", "mixed"}:
        raise ValueError("invalid shot style")
    common = [f"character_{s}_reference_missing" for s in ("A", "B")
              if s not in pack.characters or pack.characters[s].reference_image is None]

    def camera_status(camera):
        missing = [] if camera.master_image else [f"{camera.id}:master_image_missing"]
        clips = []
        roles = ("A", "B") if camera.mode == "twoShot" else (camera.subject_speaker,)
        for role in roles:
            for state in ("speaking", "listening"):
                candidates = [c for c in pack.clips if c.camera_id == camera.id and c.role == role and c.state == state]
                eligible = next((c for c in candidates if not clip_readiness(pack, c)), None)
                if eligible:
                    clips.append(eligible.id)
                else:
                    missing.append(f"{camera.id}:{role}:{state}:" + (clip_readiness(pack, candidates[0])[0] if candidates else "clip_missing"))
        return {"cameraId": camera.id, "clipIds": clips, "missing": missing}

    selected = []
    required = [("twoShot", None)] if style == "twoShot" else [("overShoulder", "A"), ("overShoulder", "B")]
    if style == "mixed":
        required.insert(0, ("twoShot", None))
    for mode, role in required:
        candidates = [camera_status(c) for c in pack.cameras if c.mode == mode and (role is None or c.subject_speaker == role)]
        if candidates:
            selected.append(min(candidates, key=lambda c: (len(c["missing"]), c["cameraId"])))
        else:
            common.append(f"{mode}:{role or 'AB'}:camera_missing")
    missing = common + [m for c in selected for m in c["missing"]]
    return {"ready": not missing, "missing": missing, "cameraIds": [c["cameraId"] for c in selected],
            "clipIds": [id for c in selected for id in c["clipIds"]]}
