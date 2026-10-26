"""Atomic motion-pack heads and immutable, pin-addressable state snapshots."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from ..domain.motion_pack import (
    MotionCheckRecord, MotionClip, MotionMediaRef, MotionPack, MotionPackDraft,
    MotionQualityReport, MotionReviewRecord, ShotStyle, clip_fingerprint,
    content_hash, native_report_errors, readiness,
)


class MotionPackConflict(ValueError):
    pass


class InvalidMotionPack(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value, re.I):
        raise InvalidMotionPack("invalid motion pack identifier")
    return value


_ROOT_LOCKS: dict[str, threading.RLock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


class MotionPackStore:
    def __init__(self, root: Path, artifact_lookup: Callable[[str], dict | None] | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifact_lookup = artifact_lookup
        with _ROOT_LOCKS_GUARD:
            self._lock = _ROOT_LOCKS.setdefault(os.path.normcase(str(self.root)), threading.RLock())

    @contextmanager
    def _transaction(self):
        # The shared thread lock coordinates instances; the OS byte/file lock
        # keeps revision comparison and head publication atomic across workers.
        with self._lock:
            path = self.root / ".motion-store.lock"
            if path.resolve() != path:
                raise InvalidMotionPack("motion store lock path is linked")
            with path.open("a+b") as stream:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _path(self, pack_id: str, version: str | None = None) -> Path:
        directory = self.root / _identifier(pack_id)
        candidate = directory / "head.json" if version is None else directory / "versions" / (_identifier(version) + ".json")
        try:
            if candidate.resolve() != candidate or not candidate.resolve().is_relative_to(self.root):
                raise InvalidMotionPack("motion pack path is linked or outside root")
        except (OSError, RuntimeError) as exc:
            raise InvalidMotionPack("invalid motion pack path") from exc
        return candidate

    @staticmethod
    def _write(path: Path, value: MotionPack):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".motion-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(value.model_dump(mode="json", by_alias=True), file, ensure_ascii=False, indent=2, allow_nan=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _save(self, pack: MotionPack) -> MotionPack:
        version = self._path(pack.id, pack.version)
        if version.exists():
            raise MotionPackConflict("immutable motion pack version already exists")
        self._write(version, pack)
        self._write(self._path(pack.id), pack)
        return pack.model_copy(deep=True)

    def _load(self, pack_id: str, version: str | None = None) -> MotionPack | None:
        path = self._path(pack_id, version)
        if not path.exists():
            return None
        try:
            pack = MotionPack.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, UnicodeError) as exc:
            raise InvalidMotionPack("motion pack metadata is invalid") from exc
        if pack.id != pack_id or version is not None and pack.version != version:
            raise InvalidMotionPack("motion pack storage identity mismatch")
        return pack

    def load(self, pack_id: str) -> MotionPack | None:
        with self._transaction():
            return self._load(pack_id)

    def load_version(self, pack_id: str, version: str) -> MotionPack | None:
        with self._transaction():
            return self._load(pack_id, version)

    def list(self) -> list[MotionPack]:
        with self._transaction():
            result = []
            for path in sorted(self.root.iterdir()):
                try:
                    pack = self._load(path.name)
                except InvalidMotionPack:
                    continue
                if pack:
                    result.append(pack)
            return result

    @staticmethod
    def _draft(value: MotionPackDraft | dict) -> MotionPackDraft:
        # Revalidate model dumps too: model_construct/model_copy are not validation boundaries.
        return MotionPackDraft.model_validate(value.model_dump(mode="json", by_alias=True) if isinstance(value, MotionPackDraft) else value)

    @staticmethod
    def _version(revision: int, draft: MotionPackDraft) -> str:
        # Unique suffix prevents a crash between snapshot and head publication
        # from making an orphan snapshot block the next legitimate retry.
        return f"v{revision:06d}-{content_hash(draft)[:16]}-{uuid.uuid4().hex[:8]}"

    def create(self, draft: MotionPackDraft | dict, pack_id: str | None = None) -> MotionPack:
        with self._transaction():
            draft = self._draft(draft)
            pack_id = _identifier(pack_id or "MP-" + uuid.uuid4().hex[:16])
            if self._path(pack_id).parent.exists():
                raise MotionPackConflict("motion pack id already exists")
            now = _now()
            pack = MotionPack(**draft.model_dump(), id=pack_id, revision=1,
                              version=self._version(1, draft), created_at=now, updated_at=now)
            return self._save(pack)

    def _current(self, pack_id: str, expected_revision: int) -> MotionPack:
        if type(expected_revision) is not int or expected_revision < 1:
            raise InvalidMotionPack("expectedRevision must be a positive integer")
        pack = self._load(pack_id)
        if pack is None:
            raise KeyError(pack_id)
        if pack.revision != expected_revision:
            raise MotionPackConflict("MOTION_PACK_REVISION_CONFLICT")
        return pack

    def _next(self, current: MotionPack, draft: MotionPackDraft | None = None) -> MotionPack:
        draft = draft or MotionPackDraft.model_validate(current.model_dump(include=set(MotionPackDraft.model_fields)))
        revision = current.revision + 1
        return MotionPack(**draft.model_dump(), id=current.id, revision=revision,
                          version=self._version(revision, draft), created_at=current.created_at, updated_at=_now(),
                          checks=current.checks, reviews=current.reviews)

    def update(self, pack_id: str, draft: MotionPackDraft | dict, expected_revision: int) -> MotionPack:
        with self._transaction():
            current = self._current(pack_id, expected_revision)
            draft = self._draft(draft)
            updated = self._next(current, draft)
            old = {clip.id: clip_fingerprint(current, clip) for clip in current.clips}
            unchanged = {clip.id for clip in draft.clips if old.get(clip.id) == clip_fingerprint(draft, clip)}
            updated.checks = {id: record for id, record in current.checks.items() if id in unchanged}
            updated.reviews = {id: record for id, record in current.reviews.items() if id in unchanged}
            return self._save(updated)

    @staticmethod
    def _clip(pack: MotionPack, clip_id: str) -> MotionClip:
        clip = next((clip for clip in pack.clips if clip.id == clip_id), None)
        if clip is None:
            raise KeyError(clip_id)
        return clip

    def _verify_ref(self, ref: MotionMediaRef, kind: str | None = None):
        if self.artifact_lookup is None:
            raise InvalidMotionPack("artifact integrity verifier is unavailable")
        entry = self.artifact_lookup(ref.artifact_id)
        if not entry or kind is not None and entry.get("kind") != kind:
            raise InvalidMotionPack("referenced media is missing or has wrong kind")
        if not isinstance(entry.get("path"), str) or not entry["path"]:
            raise InvalidMotionPack("referenced media path is invalid")
        path = Path(entry["path"])
        if not path.is_file() or entry.get("fileHash") != ref.file_hash:
            raise InvalidMotionPack("referenced media hash changed")
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != ref.file_hash:
            raise InvalidMotionPack("referenced media content changed")

    def _verify_clip(self, pack: MotionPack, clip: MotionClip):
        self._verify_ref(clip.media, "video")
        if clip.face_coordinates:
            self._verify_ref(clip.face_coordinates)
        camera = next(camera for camera in pack.cameras if camera.id == clip.camera_id)
        if camera.master_image is None:
            raise InvalidMotionPack("camera image is missing")
        self._verify_ref(camera.master_image, "image")
        roles = ("A", "B") if camera.mode == "twoShot" else (camera.subject_speaker, camera.foreground_speaker)
        for role in roles:
            character = pack.characters.get(role)
            if character is None or character.reference_image is None:
                raise InvalidMotionPack("character reference image is missing")
            self._verify_ref(character.reference_image, "image")

    def record_checks(self, pack_id: str, clip_id: str, report: MotionQualityReport | dict, expected_revision: int) -> MotionPack:
        with self._transaction():
            current = self._current(pack_id, expected_revision)
            clip = self._clip(current, clip_id)
            report = MotionQualityReport.model_validate(report.model_dump(mode="json", by_alias=True) if isinstance(report, MotionQualityReport) else report)
            self._verify_clip(current, clip)
            if report.input_file_hash != clip.media.file_hash:
                raise InvalidMotionPack("quality report does not match media hash")
            native_errors = native_report_errors(clip, report)
            if native_errors:
                report = report.model_copy(update={"status": "failed", "hard_failures": sorted(set(report.hard_failures + native_errors))})
            updated = self._next(current)
            updated.checks[clip_id] = MotionCheckRecord(content_hash=clip_fingerprint(current, clip), checked_at=_now(), report=report)
            return self._save(updated)

    def review(self, pack_id: str, clip_id: str, accepted: bool, expected_revision: int, reason: str = "") -> MotionPack:
        with self._transaction():
            if type(accepted) is not bool or not isinstance(reason, str) or len(reason) > 2000:
                raise InvalidMotionPack("invalid review")
            current = self._current(pack_id, expected_revision)
            clip = self._clip(current, clip_id)
            fingerprint = clip_fingerprint(current, clip)
            if accepted:
                self._verify_clip(current, clip)
                check = current.checks.get(clip_id)
                if not check or check.content_hash != fingerprint or check.report.input_file_hash != clip.media.file_hash:
                    raise InvalidMotionPack("matching quality checks are required before adoption")
                if check.report.status == "failed" or check.report.hard_failures:
                    raise InvalidMotionPack("failed quality checks cannot be overridden")
                if native_report_errors(clip, check.report):
                    raise InvalidMotionPack("valid native media evidence is required")
                if check.report.status == "needs_review" and not reason.strip():
                    raise InvalidMotionPack("uncertain checks require an explicit review reason")
            updated = self._next(current)
            updated.reviews[clip_id] = MotionReviewRecord(content_hash=fingerprint, accepted=accepted, reviewed_at=_now(), reason=reason.strip())
            return self._save(updated)

    def readiness(self, pack_id: str, style: ShotStyle, version: str | None = None) -> dict:
        with self._transaction():
            pack = self._load(pack_id, version)
            if pack is None:
                raise KeyError(pack_id)
            result = readiness(pack, style)
            for clip_id in result["clipIds"]:
                try:
                    self._verify_clip(pack, self._clip(pack, clip_id))
                except (ValueError, OSError) as exc:
                    result["missing"].append(f"{clip_id}:media_integrity_failed")
            result["ready"] = not result["missing"]
            return result
