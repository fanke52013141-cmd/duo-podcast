"""Opt-in isolated resident lip-sync processes; legacy one-shot remains available."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from .owned_process import spawn_owned

PROTOCOL = "duocast-fast-resident-v1"


class _ProcessGate:
    """Cancellation-safe process gate, usable across app event-loop lifetimes."""
    def __init__(self):
        self.lock = threading.Lock()

    async def __aenter__(self):
        while not self.lock.acquire(blocking=False):
            await asyncio.sleep(.01)

    async def __aexit__(self, *exc):
        self.lock.release()


GPU_LOCK = _ProcessGate()
_MANAGERS = weakref.WeakSet()
_HASH_CACHE = {}


@asynccontextmanager
async def runtime_lease(root):
    """Share the installer's OS lock so running code cannot be half-updated."""
    root = Path(root).resolve()
    lock_path = root / '.runtime-install.lock'
    if lock_path.is_symlink() or getattr(lock_path, 'is_junction', lambda: False)():
        raise ValueError('Runtime execution lock must not be linked')
    with lock_path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        locked = False
        try:
            deadline = time.monotonic() + 180
            while not locked:
                stream.seek(0)
                try:
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Runtime installation/execution lock timeout')
                    await asyncio.sleep(.05)
            if (root / '.runtime-install-pending.json').exists():
                raise ValueError('Incomplete runtime installation; restore its recorded release first')
            yield
        finally:
            if locked:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def runtime_identity(root: Path) -> str:
    """Version by executable code + actual model content, isolated by root too.

    Cache hashing by filesystem identity/size/mtime for repeated requests; a new
    model or code installation changes the digest and requires a fresh process.
    """
    root = root.resolve()
    names = ["worker.py", "resident_worker.py", "motion_worker.py", "quality.py", "render_geometry.py",
             "assets/parsing_bisenet.pth"]
    names += [path.name for path in root.glob("*.py")]
    names += [name for name in ("runtime_version.json", "venv/pyvenv.cfg", "venv/Scripts/python.exe")
              if (root / name).is_file()]
    site_packages = root / "venv/Lib/site-packages"
    for package in ("torch", "transformers", "diffusers", "numpy", "opencv_python", "librosa", "soxr", "scipy", "soundfile", "einops"):
        names += [str(path.relative_to(root)).replace("\\", "/")
                  for path in site_packages.glob(package + "-*.dist-info/METADATA")]
    for folder in (root / "models", root / "vendor/MuseTalk"):
        if not folder.is_dir():
            raise ValueError("Resident runtime models/vendor missing")
        names += [str(path.relative_to(root)).replace("\\", "/") for path in folder.rglob("*")
                  if path.is_file() and (folder.name == "models" or path.suffix == ".py")]
    hashes = {}
    for name in sorted(set(names)):
        path = root / name
        stat = path.stat()
        stamp = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if stamp not in _HASH_CACHE:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            _HASH_CACHE[stamp] = digest.hexdigest()
        hashes[name] = _HASH_CACHE[stamp]
    if not any(name.startswith("models/") for name in hashes):
        raise ValueError("Resident model identity is empty")
    return hashlib.sha256(json.dumps({"root": os.path.normcase(str(root)), "files": hashes}, sort_keys=True).encode()).hexdigest()


class ResidentUnavailable(RuntimeError):
    pass


class ResidentRenderError(RuntimeError):
    pass


def _read_request(path):
    with path.open("rb") as stream:
        raw = stream.read(32*1024*1024+1)
    if len(raw) > 32*1024*1024:
        raise ValueError("Worker request exceeds limit")
    return raw


class FastRuntimeManager:
    def __init__(self, root: Path, *, python=None, startup_timeout=180, request_timeout=1800):
        self.root = Path(root).resolve()
        self.python = str(python or self.root / "venv/Scripts/python.exe")
        self.startup_timeout, self.request_timeout = startup_timeout, request_timeout
        self.process, self.identity, self.log = None, None, None
        self.initialization_seconds = 0
        self.requests_completed = 0
        _MANAGERS.add(self)

    async def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            await process.stop()
        if self.log:
            self.log.close()
            self.log = None
        self.identity = None

    async def close(self):
        async with GPU_LOCK:
            await self._stop()

    async def release_and_wait(self):
        # Global release also closes another runtime/version's resident models.
        await release_all()

    async def _message(self, timeout):
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
            if not line or len(line) > 65536:
                raise ResidentUnavailable("Resident process exited or protocol line too large")
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
                raise ResidentUnavailable("Invalid resident protocol response")
            return value
        except (ValueError, asyncio.TimeoutError) as exc:
            raise ResidentUnavailable("Resident protocol timeout or malformed response") from exc

    async def _ensure(self, identity, output):
        if self.process is not None and self.process.returncode is None and self.identity == identity:
            return False
        await self._stop()
        for manager in list(_MANAGERS):
            if manager is not self:
                await manager._stop()
        self.log = (output / "resident.log").open("ab")
        try:
            self.process = await spawn_owned(self.python, "-B", str(self.root / "resident_worker.py"),
                "--identity", identity, cwd=str(self.root), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=self.log, limit=65537)
            response = await self._message(self.startup_timeout)
            if response.get("type") != "ready" or response.get("runtimeIdentity") != identity:
                raise ResidentUnavailable("Runtime does not support resident model reuse")
            self.identity = identity
            self.initialization_seconds = response.get("initializationSeconds", 0)
            self.requests_completed = 0
            return True
        except (OSError, ResidentUnavailable) as exc:
            await self._stop()
            raise ResidentUnavailable(str(exc)) from exc

    async def render(self, request_file: Path):
        """Return protocol stats; fallback only repeats local MuseTalk work once.

        This module cannot invoke ComfyUI or submit I2V requests. Inputs without
        pinned motionAssets are rejected before spawning any process.
        """
        request_file = Path(request_file).resolve()
        if request_file.stat().st_size > 32*1024*1024:
            raise ValueError("Worker request exceeds limit")
        raw = await asyncio.to_thread(_read_request, request_file)
        request = json.loads(raw)
        if not isinstance(request, dict) or not isinstance(request.get("motionAssets"), dict) or not request["motionAssets"]:
            raise ValueError("Resident execution is opt-in for pinned motion lip sync only")
        output = Path(request["outputDir"]).resolve()
        output.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(raw).hexdigest()
        started = time.monotonic()
        async with GPU_LOCK, runtime_lease(self.root):
            identity = await asyncio.to_thread(runtime_identity, self.root)
            if request.get('expectedRuntimeIdentity') is not None and request['expectedRuntimeIdentity'] != identity:
                raise ValueError('Frozen motion runtime changed while the task was queued')
            try:
                initialized = await self._ensure(identity, output)
                request_id = uuid.uuid4().hex
                packet = {"protocol": PROTOCOL, "type": "render_motion", "requestId": request_id,
                          "runtimeIdentity": identity, "requestPath": str(request_file), "requestHash": digest}
                self.process.stdin.write((json.dumps(packet)+"\n").encode())
                await self.process.stdin.drain()
                response = await self._message(self.request_timeout)
                if response.get("requestId") != request_id or response.get("runtimeIdentity") != identity:
                    raise ResidentUnavailable("Resident response identity mismatch")
                if response.get("type") == "error":
                    raise ResidentRenderError(response.get("message", "Resident render failed"))
                if response.get("type") != "done":
                    raise ResidentUnavailable("Unexpected resident response")
                self.requests_completed += 1
                stats = {"origin": "resident", "runtimeIdentity": identity, "initializedThisRequest": initialized,
                         "initializationSeconds": self.initialization_seconds,
                         "requestWallSeconds": response.get("requestWallSeconds"),
                         "totalWallSeconds": time.monotonic()-started, "requestsCompleted": self.requests_completed}
            except asyncio.CancelledError:
                await self._stop()
                raise
            except ResidentRenderError:
                await self._stop()
                raise
            except (ResidentUnavailable, BrokenPipeError, ConnectionError) as exc:
                await self._stop()
                # Recheck exact frozen input before any local-only fallback.
                if hashlib.sha256(_read_request(request_file)).hexdigest() != digest:
                    raise ValueError("Request changed before fallback") from exc
                await self._one_shot(request_file, output)
                stats = {"origin": "one-shot-fallback", "runtimeIdentity": identity,
                         "fallbackReason": str(exc), "fallbackCount": 1,
                         "totalWallSeconds": time.monotonic()-started}
        (output / "runtime_stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
        return stats

    async def _one_shot(self, request_file, output):
        with (output / "fallback_worker.log").open("ab") as log:
            process = await spawn_owned(self.python, "-B", str(self.root / "worker.py"), str(request_file),
                cwd=str(self.root), stdout=log, stderr=log)
            try:
                await asyncio.wait_for(process.wait(), self.request_timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                await process.stop()
                raise
            if process.returncode:
                raise RuntimeError("Local one-shot fallback failed; see fallback_worker.log")


async def release_all():
    """Wait for active local render, then release every resident GPU process."""
    async with GPU_LOCK:
        for manager in list(_MANAGERS):
            await manager._stop()
