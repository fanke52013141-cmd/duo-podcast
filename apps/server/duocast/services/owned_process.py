"""Server-owned subprocess trees: no orphan encoders after cancel or fallback."""
from __future__ import annotations

import asyncio
import os
import signal
import time


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("processTime", ctypes.c_longlong), ("jobTime", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("minimumWorkingSet", ctypes.c_size_t),
                    ("maximumWorkingSet", ctypes.c_size_t), ("activeLimit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("readOperations", "writeOperations", "otherOperations", "readBytes", "writeBytes", "otherBytes")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", _BasicLimits), ("io", _IoCounters),
                    ("processMemory", ctypes.c_size_t), ("jobMemory", ctypes.c_size_t),
                    ("peakProcessMemory", ctypes.c_size_t), ("peakJobMemory", ctypes.c_size_t)]

    class _Accounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in ("userTime", "kernelTime", "periodUser", "periodKernel")] + [
            (name, wintypes.DWORD) for name in ("pageFaults", "totalProcesses", "activeProcesses", "terminatedProcesses")]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("threadId", wintypes.DWORD), ("ownerPid", wintypes.DWORD),
                    ("basePriority", wintypes.LONG), ("deltaPriority", wintypes.LONG), ("flags", wintypes.DWORD)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        ("Thread32First", [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
        ("Thread32Next", [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
        ("OpenThread", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        ("ResumeThread", [wintypes.HANDLE], wintypes.DWORD),
    ):
        function = getattr(_kernel, name)
        function.argtypes, function.restype = args, result

    class _WindowsJob:
        def __init__(self):
            self.assigned = False
            self.handle = _kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimits()
            limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                self.close()
                raise ctypes.WinError(ctypes.get_last_error())

        def assign_and_resume(self, pid):
            handle = _kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not _kernel.AssignProcessToJobObject(self.handle, handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.assigned = True
            finally:
                _kernel.CloseHandle(handle)
            # CREATE_SUSPENDED prevents even the venv launcher from spawning
            # children before it belongs to our kill-on-close job.
            snapshot = _kernel.CreateToolhelp32Snapshot(0x00000004, 0)
            if snapshot == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            resumed = 0
            try:
                entry = _ThreadEntry()
                entry.size = ctypes.sizeof(entry)
                present = _kernel.Thread32First(snapshot, ctypes.byref(entry))
                while present:
                    if entry.ownerPid == pid:
                        thread = _kernel.OpenThread(0x0002, False, entry.threadId)
                        if not thread:
                            raise ctypes.WinError(ctypes.get_last_error())
                        try:
                            if _kernel.ResumeThread(thread) == 0xFFFFFFFF:
                                raise ctypes.WinError(ctypes.get_last_error())
                            resumed += 1
                        finally:
                            _kernel.CloseHandle(thread)
                    present = _kernel.Thread32Next(snapshot, ctypes.byref(entry))
            finally:
                _kernel.CloseHandle(snapshot)
            if not resumed:
                raise RuntimeError("Could not resume the owned worker")

        def terminate(self):
            if self.handle and not _kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())

        def active(self):
            record = _Accounting()
            if not _kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(record), ctypes.sizeof(record), None):
                raise ctypes.WinError(ctypes.get_last_error())
            return record.activeProcesses

        def close(self):
            if self.handle:
                _kernel.CloseHandle(self.handle)
                self.handle = None


class OwnedProcess:
    def __init__(self, process, job=None):
        self._process, self._job, self._finished = process, job, False
        self._cleanup_task = None

    def __getattr__(self, name):
        return getattr(self._process, name)

    async def _finish_tree(self):
        if self._finished:
            return
        if self._job:
            self._job.terminate()
            deadline = time.monotonic() + 30
            try:
                while self._job.active():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Owned worker descendants did not terminate")
                    await asyncio.sleep(.02)
            finally:
                self._job.close()
        else:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 30
            while _posix_group_alive(self.pid):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Owned worker descendants did not terminate")
                await asyncio.sleep(.02)
        self._finished = True

    async def wait(self):
        result = await self._process.wait()
        await self.stop()
        return result

    async def _stop_tree(self):
        if self._job:
            self._job.terminate()
        else:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await self._process.wait()
        await self._finish_tree()

    async def stop(self):
        # A second cancellation must not release the caller's execution lease
        # while descendant processes can still write output files.
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._stop_tree())
        cancelled = False
        while not self._cleanup_task.done():
            try:
                await asyncio.shield(self._cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        self._cleanup_task.result()
        if cancelled:
            raise asyncio.CancelledError


def _posix_group_alive(pgid):
    from pathlib import Path
    proc = Path('/proc')
    if proc.is_dir():
        # Reparented zombies cannot execute/write and may await a container's
        # init reaper indefinitely. Only live group members hold the lease.
        for path in proc.iterdir():
            if not path.name.isdigit():
                continue
            try:
                fields = (path / 'stat').read_text().rpartition(')')[2].split()
                if int(fields[2]) == pgid and fields[0] != 'Z':
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


async def spawn_owned(*args, **kwargs):
    job = _WindowsJob() if os.name == "nt" else None
    if job:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x00000004  # CREATE_SUSPENDED
    else:
        kwargs["start_new_session"] = True
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        if job:
            job.assign_and_resume(process.pid)
        return OwnedProcess(process, job)
    except BaseException:
        if process is not None:
            if job and job.assigned:
                await OwnedProcess(process, job).stop()
                job = None
            else:
                if os.name != 'nt':
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.returncode is None:
                    process.kill()  # Still suspended: no unowned children exist.
                await process.wait()
        if job:
            job.close()
        raise
