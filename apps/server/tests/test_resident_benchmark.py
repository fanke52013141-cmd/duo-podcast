"""Benchmark orchestration uses real FFmpeg media, with an explicitly fake model."""
import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SOURCE = Path(__file__).parents[3] / 'scripts/acceptance/run_resident_benchmark.py'
sys.path.insert(0, str(SOURCE.parent))
spec = importlib.util.spec_from_file_location('resident_benchmark_test', SOURCE)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_relative_report_paths_become_absolute_and_all_runs_are_checked(tmp_path, monkeypatch):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg required')
    monkeypatch.chdir(tmp_path)
    fixture = tmp_path / 'fixture.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=854x480:r=25:d=0.4',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.4', '-c:v', 'libx264',
                    '-c:a', 'aac', '-shortest', str(fixture)], check=True, capture_output=True, timeout=20)
    source = tmp_path / 'request.json'
    source.write_text(json.dumps({'motionAssets': {'explicitTestFixture': True}, 'sampleCount': 6400,
        'sampleRate': 16000, 'resolution': '854x480', 'fps': 25}))
    instances = []

    class FakeModelManager:
        def __init__(self, root):
            self.initialized = False
            instances.append(self)

        async def render(self, request_path):
            assert request_path.is_absolute()
            request = json.loads(request_path.read_bytes())
            output = Path(request['outputDir'])
            assert output.is_absolute()
            shutil.copyfile(fixture, output / 'final.mp4')
            (output / 'metrics.json').write_text('{"explicitFakeModel": true}')
            first = not self.initialized
            self.initialized = True
            return {'origin': 'resident', 'initializedThisRequest': first, 'runtimeIdentity': 'test-only'}

        async def close(self):
            self.initialized = False

    monkeypatch.setattr(benchmark, 'FastRuntimeManager', FakeModelManager)
    monkeypatch.setattr(benchmark, 'runtime_identity', lambda root: 'test-only')
    report = asyncio.run(benchmark.benchmark(tmp_path, source, Path('reports/result.json'), 1, 1))
    assert report['passed'] is True
    assert [(row['kind'], row['measured']) for row in report['runs']] == [('cold', True), ('primer', False), ('warm', True)]
    assert all(row['media']['passed'] for row in report['runs'])
    assert report['summary']['cold']['count'] == report['summary']['warm']['count'] == 1
    assert instances[0].initialized is False
    with pytest.raises(ValueError, match='overwrite'):
        asyncio.run(benchmark.benchmark(tmp_path, source, Path('reports/result.json'), 1, 1))


def test_benchmark_rejects_legacy_or_empty_groups_before_inference(tmp_path):
    source = tmp_path / 'request.json'
    source.write_text('{}')
    with pytest.raises(ValueError, match='1..20'):
        asyncio.run(benchmark.benchmark(tmp_path, source, tmp_path / 'report.json', 0, 1))
    with pytest.raises(ValueError, match='frozen'):
        asyncio.run(benchmark.benchmark(tmp_path, source, tmp_path / 'report.json', 1, 1))


def test_different_runtime_between_rounds_cannot_pass_same_configuration_gate():
    report = {'runtimeIdentity': 'version-A', 'runs': [
        {'runtime': {'runtimeIdentity': 'version-A'}}, {'runtime': {'runtimeIdentity': 'version-B'}}]}
    assert benchmark.same_runtime_verified(report) is False
    report['runs'][1]['runtime']['runtimeIdentity'] = 'version-A'
    assert benchmark.same_runtime_verified(report) is True
