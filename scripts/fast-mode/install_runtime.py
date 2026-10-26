"""Transactional worker-only runtime installation and validated local rollback.

Models, assets, vendor code and venv directories are never modified. Their
read-only junctions are permitted. Stop runtime workers before installing.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
import warnings

FILES = ("worker.py", "render_geometry.py", "motion_worker.py", "motion_audio_cache.py", "resident_worker.py", "quality.py")
MANIFEST = "runtime_version.json"
BACKUPS = ".runtime-releases"
PENDING = ".runtime-install-pending.json"


def _linked(path):
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _safe(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if _linked(item):
            raise ValueError("Refuse linked write/source path: " + str(item))
    return path


def _runtime(target):
    target = _safe(target)
    if not target.is_dir() or not (target / "models").is_dir() or not (target / "assets").is_dir():
        raise ValueError("Target must be an existing runtime with models/ and assets/")
    for name in (*FILES, MANIFEST, BACKUPS, PENDING, ".runtime-install.lock"):
        path = _safe(target / name)
        if path.exists() and (not path.is_dir() if name == BACKUPS else not path.is_file()):
            raise ValueError("Invalid runtime destination: " + name)
    return target


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path, data, mode=None):
    path = _safe(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_write(path, data):
    _atomic_bytes(path, json.dumps(data, indent=2, sort_keys=True).encode())


@contextmanager
def _lock(target):
    path = _safe(target / ".runtime-install.lock")
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Another runtime installation/rollback is active") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_json(path):
    _safe(path)
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Missing or oversized runtime metadata")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid runtime metadata") from exc


def _backup(target, release_id):
    if not isinstance(release_id, str) or not re.fullmatch(r"[a-f0-9]{32}", release_id):
        raise ValueError("Invalid backup ID; use the exact releaseId, never a path")
    folder = _safe(target / BACKUPS / release_id)
    metadata = _read_json(folder / "snapshot.json")
    if (not isinstance(metadata, dict) or metadata.get("schemaVersion") != 1 or metadata.get("releaseId") != release_id
            or metadata.get("runtime") != str(target) or set(metadata.get("files", {})) != {*FILES, MANIFEST}):
        raise ValueError("Backup identity or file manifest differs")
    # Validate every backup before touching any runtime file.
    for name, record in metadata["files"].items():
        if not isinstance(record, dict) or type(record.get("existed")) is not bool:
            raise ValueError("Invalid backup file record")
        original = _safe(folder / name)
        if record["existed"]:
            if not original.is_file() or _hash(original.read_bytes()) != record.get("sha256") or type(record.get("mode")) is not int:
                raise ValueError("Backup checksum differs: " + name)
        elif original.exists() or record.get("sha256") is not None:
            raise ValueError("Unexpected backup file: " + name)
    return folder, metadata


def _restore(target, folder, metadata):
    for name, record in metadata["files"].items():
        destination = _safe(target / name)
        if record["existed"]:
            _atomic_bytes(destination, (folder / name).read_bytes(), record["mode"])
        elif destination.exists():
            destination.unlink()
    for name, record in metadata["files"].items():
        path = target / name
        if path.exists() != record["existed"] or record["existed"] and _hash(path.read_bytes()) != record["sha256"]:
            raise RuntimeError("Runtime rollback verification failed: " + name)


def install(source, target):
    target = _runtime(target)
    source = _safe(source)
    origins = {name: source / name for name in FILES if name != "quality.py"}
    origins["quality.py"] = source.parents[1] / "apps/server/duocast/services/fast_quality.py"
    data, modes = {}, {}
    # All sources and all destination paths are checked before snapshots or writes.
    for name, origin in origins.items():
        origin = _safe(origin)
        if not origin.is_file():
            raise ValueError("Missing source: " + str(origin))
        data[name], modes[name] = origin.read_bytes(), stat.S_IMODE(origin.stat().st_mode)
    with _lock(target):
        if (target / PENDING).exists():
            pending = _read_json(target / PENDING)
            raise ValueError("Incomplete installation; run --rollback " + str(pending.get("releaseId")))
        release_id = uuid.uuid4().hex
        folder = _safe(target / BACKUPS / release_id)
        folder.mkdir(parents=True, exist_ok=False)
        records = {}
        for name in (*FILES, MANIFEST):
            path = _safe(target / name)
            original = path.read_bytes() if path.exists() else None
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
            records[name] = {"existed": original is not None, "sha256": _hash(original) if original is not None else None, "mode": mode}
            if original is not None:
                _atomic_bytes(folder / name, original, mode)
        metadata = {"schemaVersion": 1, "runtime": str(target), "releaseId": release_id, "files": records,
                    "installedHashes": {name: _hash(content) for name, content in data.items()}}
        _json_write(folder / "snapshot.json", metadata)
        _backup(target, release_id)
        identity_changed = records["worker.py"]["existed"] and records["worker.py"]["sha256"] != _hash(data["worker.py"])
        notices = (["worker.py identity changed: existing fast-template pins/catalog reviews must be revalidated; no template identity was rewritten"] if identity_changed else [])
        version = {"schemaVersion": 2, "implementation": "motion-runtime-transactional-v1", "releaseId": release_id,
                   "files": {name: {"sha256": _hash(content), "bytes": len(content)} for name, content in data.items()},
                   "warnings": notices, "templateIdentityRewritten": False}
        _json_write(target / PENDING, {"releaseId": release_id})
        try:
            for name, content in data.items():
                _atomic_bytes(target / name, content, records[name]["mode"] if records[name]["existed"] else modes[name])
            _json_write(target / MANIFEST, version)
            for name, content in data.items():
                if _hash((target / name).read_bytes()) != _hash(content):
                    raise RuntimeError("Installed file verification failed: " + name)
            (target / PENDING).unlink()
        except BaseException:
            # Keep pending if rollback itself fails so the next installer refuses
            # to continue over an incomplete transaction.
            _restore(target, folder, metadata)
            (target / PENDING).unlink(missing_ok=True)
            raise
        for notice in notices:
            warnings.warn(notice, RuntimeWarning)
    return target


def rollback(target, release_id):
    target = _runtime(target)
    with _lock(target):
        folder, metadata = _backup(target, release_id)
        pending = _read_json(target / PENDING) if (target / PENDING).exists() else {}
        current = _read_json(target / MANIFEST) if (target / MANIFEST).exists() else {}
        already_restored = all((target / name).exists() == record["existed"] and
                              (not record["existed"] or _hash((target / name).read_bytes()) == record["sha256"])
                              for name, record in metadata["files"].items())
        if not already_restored and current.get("releaseId") != release_id and pending.get("releaseId") != release_id:
            raise ValueError("Backup is not the active release or pending transaction")
        _json_write(target / PENDING, {"releaseId": release_id, "operation": "rollback"})
        _restore(target, folder, metadata)
        (target / PENDING).unlink()
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--rollback", metavar="RELEASE_ID", help="Restore a verified snapshot from this runtime")
    args = parser.parse_args()
    destination = rollback(args.runtime, args.rollback) if args.rollback else install(Path(__file__).resolve().parent, args.runtime)
    print(destination)
    if (destination / MANIFEST).exists():
        print((destination / MANIFEST).read_text(encoding="utf-8"))
