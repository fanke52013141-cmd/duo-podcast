"""Real subprocess protocol tests with explicit CPU-only fake model runtime."""
import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

from duocast.services.fast_runtime import FastRuntimeManager, ResidentRenderError, runtime_identity

ENTRY = Path(__file__).parents[3] / "scripts/fast-mode/resident_worker.py"


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    shutil.copyfile(ENTRY, root / "resident_worker.py")
    for name in ("motion_worker.py", "quality.py", "render_geometry.py", "models/model.bin", "assets/parsing_bisenet.pth", "vendor/MuseTalk/model.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"# CPU protocol fixture\n")
    (root / "worker.py").write_text('''
import json, os, sys, time
from pathlib import Path
ROOT=Path(__file__).parent
def initialize_runtime():
    print('model initialization log must go stderr')
    with (ROOT/'initializations.txt').open('a') as stream: stream.write('init\\n')
    return {'calls': 0}
def run_with_runtime(request, runtime):
    runtime['calls'] += 1
    if request.get('crash'): os._exit(7)
    if request.get('fail'): raise ValueError('invalid render geometry')
    if request.get('slow'): time.sleep(20)
    marker=ROOT/'active-render.marker'
    with marker.open('x') as stream: stream.write('active')
    with (ROOT/'activity.txt').open('a') as stream: stream.write('start '+str(runtime['calls'])+'\\n')
    time.sleep(.03)
    target=Path(request['outputDir']); target.mkdir(exist_ok=True)
    (target/'fixture_result.json').write_text(json.dumps({'calls':runtime['calls'],'origin':'resident'}))
    with (ROOT/'activity.txt').open('a') as stream: stream.write('end '+str(runtime['calls'])+'\\n')
    marker.unlink()
if __name__=='__main__':
    request=json.loads(Path(sys.argv[1]).read_text())
    with (ROOT/'fallbacks.txt').open('a') as stream: stream.write('fallback\\n')
    target=Path(request['outputDir']); target.mkdir(exist_ok=True)
    (target/'fixture_result.json').write_text(json.dumps({'origin':'one-shot'}))
''', encoding="utf-8")
    return root


def request_file(root, name, **extra):
    output = root.parent / name
    output.mkdir()
    path = output / "request.json"
    path.write_text(json.dumps({"motionAssets": {"packId": "test"}, "outputDir": str(output), **extra}), encoding="utf-8")
    return path


def test_two_requests_share_one_real_subprocess_initialization(runtime):
    async def scenario():
        manager = FastRuntimeManager(runtime, python=sys.executable, startup_timeout=5)
        try:
            first = await manager.render(request_file(runtime, "first"))
            process = manager.process.pid
            second = await manager.render(request_file(runtime, "second"))
            assert manager.process.pid == process
            assert first["initializedThisRequest"] and not second["initializedThisRequest"]
            assert second["requestsCompleted"] == 2
            assert first["origin"] == second["origin"] == "resident"
            assert (runtime / "initializations.txt").read_text().splitlines() == ["init"]
            assert not (runtime / "fallbacks.txt").exists()
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_model_identity_change_restarts_process(runtime):
    async def scenario():
        manager = FastRuntimeManager(runtime, python=sys.executable, startup_timeout=5)
        try:
            first = await manager.render(request_file(runtime, "old"))
            old_pid = manager.process.pid
            (runtime / "models/model.bin").write_bytes(b"different model revision")
            second = await manager.render(request_file(runtime, "new"))
            assert first["runtimeIdentity"] != second["runtimeIdentity"]
            assert manager.process.pid != old_pid and second["initializedThisRequest"]
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_crash_falls_back_once_local_only_and_input_error_never_retries(runtime):
    async def scenario():
        manager = FastRuntimeManager(runtime, python=sys.executable, startup_timeout=5)
        try:
            result = await manager.render(request_file(runtime, "crash", crash=True))
            assert result["origin"] == "one-shot-fallback" and result["fallbackCount"] == 1
            assert (runtime / "fallbacks.txt").read_text().splitlines() == ["fallback"]
            with pytest.raises(ResidentRenderError, match="invalid render geometry"):
                await manager.render(request_file(runtime, "invalid", fail=True))
            assert (runtime / "fallbacks.txt").read_text().splitlines() == ["fallback"]
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_eof_fallback_waits_for_actual_descendant_writers_to_terminate(runtime):
    worker_path = runtime / 'worker.py'
    code = worker_path.read_text(encoding='utf-8')
    child = "import time,sys;from pathlib import Path;time.sleep(.8);Path(sys.argv[1]).write_text('orphan writer')"
    replacement = ("if request.get('crash'):\n"
                   "        import subprocess\n"
                   f"        subprocess.Popen([sys.executable,'-c',{child!r},str(Path(request['outputDir'])/'fixture_result.json')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                   "        os._exit(7)")
    worker_path.write_text(code.replace("if request.get('crash'): os._exit(7)", replacement), encoding='utf-8')

    async def scenario():
        manager = FastRuntimeManager(runtime, python=sys.executable, startup_timeout=5)
        request = request_file(runtime, 'descendant-crash', crash=True)
        try:
            result = await manager.render(request)
            assert result['origin'] == 'one-shot-fallback' and result['fallbackCount'] == 1
            await asyncio.sleep(1)
            output = json.loads((request.parent / 'fixture_result.json').read_text())
            assert output['origin'] == 'one-shot'
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_cancel_stops_resident_without_fallback(runtime):
    async def scenario():
        manager = FastRuntimeManager(runtime, python=sys.executable, startup_timeout=5)
        task = asyncio.create_task(manager.render(request_file(runtime, "slow", slow=True)))
        try:
            for _ in range(200):
                if manager.identity:
                    break
                await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert manager.process is None
            assert not (runtime / "fallbacks.txt").exists()
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_cross_runtime_exclusion_and_global_release(runtime, tmp_path):
    other = tmp_path / "second-runtime"
    shutil.copytree(runtime, other)
    async def scenario():
        first = FastRuntimeManager(runtime, python=sys.executable, startup_timeout=5)
        second = FastRuntimeManager(other, python=sys.executable, startup_timeout=5)
        try:
            await first.render(request_file(runtime, "a"))
            old = first.process
            await second.render(request_file(other, "b"))
            assert first.process is None and old.returncode is not None
            await first.release_and_wait()
            assert second.process is None
        finally:
            await first.close()
            await second.close()
    asyncio.run(scenario())


def test_legacy_or_non_lipsync_request_rejected_before_spawn(runtime):
    async def scenario():
        manager = FastRuntimeManager(runtime, python=sys.executable)
        path = request_file(runtime, "legacy", motionAssets=None)
        with pytest.raises(ValueError, match="opt-in"):
            await manager.render(path)
        assert manager.process is None
    asyncio.run(scenario())


def test_pure_protocol_rejects_wrong_hash_and_oversized_lines(runtime):
    spec = importlib.util.spec_from_file_location("resident_protocol_test", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="changed"):
        module.load_request(request_file(runtime, "hash"), "0"*64)
    with pytest.raises(ValueError, match="limit"):
        module.parse_message("x"*65537)
    with pytest.raises(ValueError, match="Unsupported"):
        module.parse_message('{"protocol":"other"}')


def test_old_worker_capability_falls_back_without_changing_legacy_entry(runtime):
    code = (runtime / "worker.py").read_text(encoding="utf-8")
    code = code.replace("def initialize_runtime():", "def old_initialize_runtime():")
    (runtime / "worker.py").write_text(code, encoding="utf-8")
    async def scenario():
        manager = FastRuntimeManager(runtime, python=sys.executable, startup_timeout=5)
        try:
            result = await manager.render(request_file(runtime, "unsupported"))
            assert result["origin"] == "one-shot-fallback"
            assert not (runtime / "initializations.txt").exists()
            assert (runtime / "fallbacks.txt").read_text().splitlines() == ["fallback"]
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_concurrent_requests_are_serialized_in_same_process(runtime):
    async def scenario():
        manager = FastRuntimeManager(runtime, python=sys.executable, startup_timeout=5)
        try:
            first, second = await asyncio.gather(manager.render(request_file(runtime, "serial-one")),
                                                  manager.render(request_file(runtime, "serial-two")))
            assert sorted([first["requestsCompleted"], second["requestsCompleted"]]) == [1, 2]
            assert (runtime / "initializations.txt").read_text().splitlines() == ["init"]
            assert (runtime / "activity.txt").read_text().splitlines() == ["start 1", "end 1", "start 2", "end 2"]
        finally:
            await manager.close()
    asyncio.run(scenario())
