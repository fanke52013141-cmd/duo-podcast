"""Actual CPU subprocess trees; delayed writers must never outlive their owner."""
import asyncio
import sys

import pytest

from duocast.services.owned_process import spawn_owned


def parent_program(marker, *, exits):
    child = f"import time;from pathlib import Path;time.sleep(.7);Path({str(marker)!r}).write_text('orphan writer')"
    return ("import subprocess,sys,time;"
            f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
            "print('child-started',flush=True);" + ("time.sleep(.05)" if exits else "time.sleep(30)"))


def test_cancel_stops_the_entire_actual_child_tree(tmp_path):
    async def scenario():
        marker = tmp_path / 'cancelled-child.txt'
        process = await spawn_owned(sys.executable, '-c', parent_program(marker, exits=False),
                                    stdout=asyncio.subprocess.PIPE)
        assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b'child-started'
        await process.stop()
        await process.stop()  # Idempotent cleanup, including closed Windows job.
        await asyncio.sleep(.9)
        assert not marker.exists()
        assert process.returncode is not None
    asyncio.run(scenario())


def test_parent_eof_cleans_descendants_before_a_fallback_can_write(tmp_path):
    async def scenario():
        marker = tmp_path / 'output.txt'
        process = await spawn_owned(sys.executable, '-c', parent_program(marker, exits=True),
                                    stdout=asyncio.subprocess.PIPE)
        await asyncio.wait_for(process.stdout.readline(), 5)
        assert await asyncio.wait_for(process.wait(), 5) == 0
        marker.write_text('new fallback output')
        await asyncio.sleep(.9)
        assert marker.read_text() == 'new fallback output'
    asyncio.run(scenario())


def test_cancelling_a_wait_then_cleanup_leaves_no_delayed_writer(tmp_path):
    async def scenario():
        marker = tmp_path / 'timed-out-child.txt'
        process = await spawn_owned(sys.executable, '-c', parent_program(marker, exits=False),
                                    stdout=asyncio.subprocess.PIPE)
        await asyncio.wait_for(process.stdout.readline(), 5)
        waiter = asyncio.create_task(process.wait())
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        await process.stop()
        await asyncio.sleep(.9)
        assert not marker.exists()
    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Job Object admission failure')
def test_windows_assignment_failure_never_runs_uncontained_code(tmp_path, monkeypatch):
    from duocast.services import owned_process
    marker = tmp_path / 'must-not-execute.txt'

    def reject(self, pid):
        raise OSError('simulated job admission denial')

    monkeypatch.setattr(owned_process._WindowsJob, 'assign_and_resume', reject)

    async def scenario():
        code = f"from pathlib import Path;Path({str(marker)!r}).write_text('unsafe')"
        with pytest.raises(OSError, match='admission'):
            await spawn_owned(sys.executable, '-c', code)
        assert not marker.exists()

    asyncio.run(scenario())
