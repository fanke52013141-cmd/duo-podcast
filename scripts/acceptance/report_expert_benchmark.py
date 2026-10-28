"""Read benchmark evidence without contacting an engine or asserting quality."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import subprocess


def decoded_hash(path, stream):
    command = ['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-map', '0:'+stream+':0']
    command += ['-pix_fmt', 'rgb24'] if stream == 'v' else ['-ac', '1', '-ar', '48000']
    command += ['-f', 'hash', '-hash', 'sha256', '-']
    return subprocess.check_output(command, timeout=120).decode().strip()


def summarize(path):
    state = json.loads(Path(path).read_text(encoding='utf-8'))
    root = Path(path).resolve().parent
    nodes = root/'engine/node-timings.jsonl'
    rows = [json.loads(line) for line in nodes.read_text(encoding='utf-8').splitlines()] if nodes.exists() else []
    phases = defaultdict(float)
    for row in rows:
        phases[row['nodeId'] + ':' + row['className']] += row['wallSeconds']
    results = []
    for record in state['results']:
        media = root/record['file']
        if hashlib.sha256(media.read_bytes()).hexdigest() != record['fileHash']:
            raise ValueError('Benchmark output changed')
        results.append({'index': record['index'], 'engineSeconds': record['engineSeconds'],
                        'successfulSageCalls': record.get('successfulSageCalls'),
                        'videoHash': decoded_hash(media, 'v'), 'audioHash': decoded_hash(media, 'a')})
    gpu = []
    for record in state['results']:
        for sample in record['sampledWholeDeviceGPU']:
            if sample.get('csv') and '\n' not in sample['csv']:
                try:
                    used, total, utilization = map(float, sample['csv'].split(','))
                    gpu.append({'usedMiB': used, 'totalMiB': total, 'utilizationPercent': utilization})
                except ValueError:
                    pass  # Unsupported NVML readings remain unknown, never zero usage.
    minimum_free = min((x['totalMiB']-x['usedMiB'] for x in gpu), default=None)
    threshold = state['plan'].get('minimumWholeDeviceFreeMiB', 1536)
    return {'runId': state['plan']['runId'], 'completed': state['completed'],
            'variant': state['plan']['variant'], 'profileNodes': state['plan']['profileNodes'],
            'sourcePlanSha256': state['plan']['sourcePlanSha256'], 'code': state['plan']['code'],
            'attentionAddon': state['plan'].get('attentionAddon'),
            'startupSeconds': state.get('startupSeconds'), 'segments': results,
            'totalWallSeconds': state.get('totalWallSeconds'),
            'idleWholeDeviceGPU': state.get('idleWholeDeviceGPU'),
            'engineExecutionSumSeconds': sum(r['engineSeconds'] for r in results),
            'nodeWallSeconds': dict(sorted(phases.items(), key=lambda x: -x[1])),
            'sampledWholeDeviceMaxUsedMiB': max((x['usedMiB'] for x in gpu), default=None),
            'sampledWholeDeviceMinFreeMiB': minimum_free,
            'memoryMarginThresholdMiB': threshold,
            'sampledMemoryMarginPassed': minimum_free >= threshold if minimum_free is not None else None,
            'peakAllocatedBytesSinceEngineStart': max((x.get('peakAllocatedBytesSinceEngineStart', 0) for x in rows), default=None),
            'qualityAccepted': False, 'limitations': state['limitations']}


def compare(first, second):
    if (not first['completed'] or not second['completed']
            or first['sourcePlanSha256'] != second['sourcePlanSha256']
            or first['profileNodes'] != second['profileNodes'] or first['code'] != second['code']
            or first.get('attentionAddon') != second.get('attentionAddon')
            or [r['index'] for r in first['segments']] != [r['index'] for r in second['segments']]):
        raise ValueError('Runs differ in completeness, source, instrumentation, code or segment selection')
    idle = [r.get('idleWholeDeviceGPU') for r in (first, second)]
    if all(value and value.get('csv') for value in idle):
        if abs(float(idle[0]['csv'].split(',')[0])-float(idle[1]['csv'].split(',')[0])) > 512:
            raise ValueError('Idle GPU memory differs by over 512 MiB; background workload is not comparable')
    if any(r['engineSeconds'] <= 0 for run in (first, second) for r in run['segments']):
        raise ValueError('Inference timing must be positive')
    return {'baseline': first['runId'], 'candidate': second['runId'],
            'observedExecutionSpeedup': first['engineExecutionSumSeconds']/second['engineExecutionSumSeconds'],
            'pairedSegments': [{'index': a['index'], 'decodedVideoEqual': a['videoHash']==b['videoHash'],
                                'decodedAudioEqual': a['audioHash']==b['audioHash'],
                                'speedup': a['engineSeconds']/b['engineSeconds']}
                               for a, b in zip(first['segments'], second['segments'])],
            'statisticalConclusion': False, 'qualityAccepted': False,
            'candidateSampledMemoryMarginPassed': second.get('sampledMemoryMarginPassed'),
            'limitations': ['Single paired screening run, not repeated benchmark',
                           'Pixel equality applies only to these exact test windows',
                           'Profiling result does not establish unprofiled or two-minute speed']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    base = summarize(args.state)
    result = {'baseline': base}
    if args.candidate:
        result['candidate'] = summarize(args.candidate)
        result['comparison'] = compare(base, result['candidate'])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result.get('comparison', {'engineExecutionSumSeconds': base['engineExecutionSumSeconds'],
                                             'sampledWholeDeviceMinFreeMiB': base['sampledWholeDeviceMinFreeMiB']})))


if __name__ == '__main__':
    main()
