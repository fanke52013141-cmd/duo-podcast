"""Measure actual model-cold/model-warm local motion renders, with media checks."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'apps/server'))
from duocast.services.fast_runtime import FastRuntimeManager, runtime_identity
from run_motion_acceptance import environment, input_record, inspect_media


def same_runtime_verified(report):
    return bool(report.get('runs')) and all(row.get('runtime', {}).get('runtimeIdentity') == report['runtimeIdentity']
        for row in report['runs'])


async def benchmark(runtime, source, output, cold_runs=5, warm_runs=5):
    runtime, source, output = runtime.resolve(), source.resolve(), output.resolve()
    if output.exists():
        raise ValueError('Benchmark report must not overwrite an existing file')
    if not 1 <= cold_runs <= 20 or not 1 <= warm_runs <= 20:
        raise ValueError('Each benchmark group needs 1..20 runs')
    request = json.loads(source.read_bytes())
    if not request.get('motionAssets'):
        raise ValueError('Only frozen local motion lip-sync requests are supported')
    workspace = output.parent / ('resident-runs-' + uuid.uuid4().hex)
    workspace.mkdir(parents=True)
    manager = FastRuntimeManager(runtime)
    report = {'formatVersion': 1, 'scope': 'raw_worker_performance_and_media_only',
              'source': input_record(source), 'runtimeIdentity': runtime_identity(runtime),
              'environment': environment(), 'runs': [], 'passed': False,
              'coldDefinition': 'Resident process and model allocation restarted before each measured run; OS file and disk feature caches retained',
              'warmDefinition': 'Same initialized resident process after one recorded unmeasured primer',
              'limitations': ['Not formal application/API acceptance', 'Not automatic visual naturalness or identity approval',
                              'Timing depends on this hardware, request and populated disk caches']}
    async def one(kind, index, measured=True):
        folder = workspace / f'{kind}-{index}'
        folder.mkdir()
        payload = {**request, 'outputDir': str(folder)}
        request_path = folder / 'request.json'
        request_path.write_text(json.dumps(payload), encoding='utf-8')
        start = time.monotonic()
        stats = await manager.render(request_path)
        elapsed = time.monotonic() - start
        row = {'kind': kind, 'index': index, 'measured': measured, 'wallSeconds': elapsed,
               'runtime': stats, 'media': {'passed': False, 'reason': 'not_checked'}}
        report['runs'].append(row)
        width, height = map(int, payload.get('resolution', '1920x1080').split('x'))
        media = await asyncio.to_thread(inspect_media, folder / 'final.mp4',
            duration=payload['sampleCount'] / payload['sampleRate'], fps=payload.get('fps', 25), width=width, height=height)
        metrics = json.loads((folder / 'metrics.json').read_text(encoding='utf-8'))
        row.update(metrics=metrics, media=media)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'kind': kind, 'index': index, 'seconds': elapsed,
                          'origin': stats['origin'], 'initialized': stats.get('initializedThisRequest'),
                          'mediaPassed': media['passed']}), flush=True)
    try:
        for index in range(cold_runs):
            await manager.close()
            await one('cold', index)
        await manager.close()
        await one('primer', 0, measured=False)
        for index in range(warm_runs):
            await one('warm', index)
        measured = [r for r in report['runs'] if r['measured']]
        report['runtimeIdentityConsistent'] = same_runtime_verified(report)
        report['passed'] = report['runtimeIdentityConsistent'] and all(r['media']['passed'] and r['runtime']['origin'] == 'resident'
            and r['runtime'].get('initializedThisRequest') == (r['kind'] == 'cold') for r in measured)
        report['summary'] = {kind: {'count': len(rows), 'medianSeconds': statistics.median(r['wallSeconds'] for r in rows),
            'maxSeconds': max(r['wallSeconds'] for r in rows)} for kind in ('cold', 'warm')
            for rows in [[r for r in measured if r['kind'] == kind]]}
    finally:
        await manager.close()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cold-runs', type=int, default=5)
    parser.add_argument('--warm-runs', type=int, default=5)
    args = parser.parse_args()
    report = asyncio.run(benchmark(args.runtime, args.request, args.output, args.cold_runs, args.warm_runs))
    print(json.dumps({'passed': report['passed'], 'summary': report.get('summary')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
