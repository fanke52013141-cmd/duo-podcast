"""Isolated expert benchmark. Default plan is local-only and performs no inference."""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from run_motion_api_acceptance import NoRedirect, atomic_json, json_bytes, state_lock
from run_motion_acceptance import inspect_media

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'apps/server'))
sys.path.insert(0, str(REPO / 'scripts/digital-human'))
from duocast.services.owned_process import spawn_owned
from attention_addon import describe_package

VARIANTS = {'baseline': ('1', 38), 'async': ('0', 38),
            'baseline-swap36': ('1', 36), 'baseline-swap32': ('1', 32),
            'async-swap36': ('0', 36), 'async-swap32': ('0', 32),
            'async-nonblocking': ('0', 38), 'async-prefetch1': ('0', 38),
            'async-sage': ('0', 38), 'async-sage-textcache': ('0', 38)}
CONFIG_KEYS = {'sourcePlan', 'enginePython', 'comfyRoot', 'outputRoot'}
MAX_JSON = 32 * 1024 * 1024


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_JSON:
        raise ValueError('Oversized local benchmark JSON')
    return json.loads(path.read_text(encoding='utf-8'))


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def configure(path, variant, indices, run_id, profile, max_windows=5):
    config = read_json(path)
    if (set(config) not in (CONFIG_KEYS, CONFIG_KEYS | {'attentionSite'}) or variant not in VARIANTS):
        raise ValueError('Use explicit whitelisted benchmark configuration')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', run_id):
        raise ValueError('Invalid immutable run id')
    for key, value in config.items():
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('Configuration paths must be absolute')
    source = read_json(config['sourcePlan'])
    addon = describe_package(config['attentionSite']) if 'attentionSite' in config else None
    if variant in {'async-sage', 'async-sage-textcache'} and addon is None:
        raise ValueError('Sage variant requires an isolated hash-bound attentionSite')
    if max_windows not in {5, 16, 50}:
        raise ValueError('Use a declared screening, thirty-second or two-minute resource bound')
    if not indices or len(indices) > max_windows or len(set(indices)) != len(indices):
        raise ValueError(f'Select one to {max_windows} distinct source segments')
    if max_windows > 5:
        rate, count = source.get('sampleRate'), source.get('sampleCount')
        if (type(rate) is not int or type(count) is not int or not 1 <= rate <= 384000
                or not 1 <= count <= rate*({16:30,50:120}[max_windows])):
            raise ValueError('Long benchmark exceeds its declared authoritative audio duration')
    selected, inputs = [], {}
    for index in indices:
        if type(index) is not int or not 0 <= index < len(source['segments']):
            raise ValueError('Unknown source segment')
        item = deepcopy(source['segments'][index])
        built = read_json(item['file'])
        graph = deepcopy(built['workflow'])
        native = item.get('nativeSize', [864, 480])
        if (not isinstance(native, list) or len(native) != 2
                or any(type(v) is not int or v % 16 for v in native)
                or not 256 <= native[0] <= 864 or not 256 <= native[1] <= 480):
            raise ValueError('Invalid native model dimensions')
        if native != [864, 480]:
            if not source.get('roi') or source['roi'].get('spatialResize') is not False:
                raise ValueError('Smaller dimensions require explicit native-pixel ROI provenance')
            with Path(item['inputs']['anchorImage']['path']).open('rb') as stream:
                header = stream.read(24)
            if (header[:8] != b'\x89PNG\r\n\x1a\n' or len(header) != 24
                    or list(struct.unpack('>II', header[16:24])) != native
                    or graph['213']['inputs']['scale_to_length'] != native[1]):
                raise ValueError('ROI anchor or graph differs from native geometry')
        if graph['134']['inputs']['blocks_to_swap'] != 38:
            raise ValueError('Source is not the frozen 38-block baseline')
        graph['134']['inputs']['blocks_to_swap'] = VARIANTS[variant][1]
        if variant == 'async-nonblocking':
            graph['134']['inputs']['use_non_blocking'] = True
        if variant == 'async-prefetch1':
            graph['134']['inputs']['prefetch_blocks'] = 1
        if variant in {'async-sage', 'async-sage-textcache'}:
            if graph['122']['inputs']['attention_mode'] != 'sdpa':
                raise ValueError('Source attention backend is not SDPA')
            graph['122']['inputs']['attention_mode'] = 'sageattn'
        graph['229']['inputs']['filename_prefix'] = f'expert_optimization/{run_id}_{index}'
        for entry in item['inputs'].values():
            source_file = Path(entry['path']).resolve(strict=True)
            if file_hash(source_file) != entry['fileHash']:
                raise ValueError('Prepared reference/audio changed')
            previous = inputs.get(source_file.name)
            if previous and previous['fileHash'] != entry['fileHash']:
                raise ValueError('Input basename collision')
            inputs[source_file.name] = {'path': str(source_file), 'fileHash': entry['fileHash']}
        selected.append({'index': index, 'workflow': graph, 'modelFrames': item['modelFrames'],
                         'coreFrames': item['coreFrames'], 'trimStartFrame': item['trimStartFrame'],
                         'trimEndFrame': item['trimEndFrame'], 'nativeSize': native})
    code = {p.relative_to(REPO).as_posix(): file_hash(p) for p in (
        Path(__file__), REPO/'scripts/digital-human/start_guarded.py',
        REPO/'scripts/digital-human/benchmark_probe.py',
        REPO/'scripts/digital-human/attention_addon.py',
        REPO/'scripts/digital-human/text_condition_cache.py',
        REPO/'apps/server/duocast/services/owned_process.py')}
    return {'config': config, 'variant': variant, 'runId': run_id,
            'cudaBlocking': VARIANTS[variant][0], 'blocksToSwap': VARIANTS[variant][1],
            'profileNodes': profile, 'segments': selected, 'inputs': inputs,
            'sourcePlanSha256': file_hash(config['sourcePlan']), 'code': code,
            'minimumWholeDeviceFreeMiB': 1536, 'minimumStartupFreeMiB': 12288,
            'attentionAddon': addon, 'textConditionCache': variant == 'async-sage-textcache',
            'maximumSelectedWindows': max_windows,
            'maximumRunSeconds': 4200 if max_windows==50 else 1800}


class LocalEngine:
    def __init__(self, port):
        self.base = f'http://127.0.0.1:{port}'
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def get(self, path):
        with self.opener.open(self.base + path, timeout=30) as response:
            raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ValueError('Oversized engine JSON')
        return json.loads(raw)

    def submit_once(self, path, workflow, identity):
        digest = hashlib.sha256(json_bytes(workflow)).hexdigest()
        if path.exists():
            receipt = read_json(path)
            if receipt['workflowSha256'] != digest:
                raise ValueError('Frozen workflow changed')
            raise ValueError('Submission receipt already exists; never repeat a model POST')
        receipt = {'workflowSha256': digest, 'submissionIntent': True, 'engineIdentity': identity}
        atomic_json(path, receipt)
        request = Request(self.base + '/prompt', data=json_bytes({'prompt': workflow}),
                          headers={'Content-Type': 'application/json'}, method='POST')
        with self.opener.open(request, timeout=120) as response:
            result = json.loads(response.read(MAX_JSON))
        receipt.update(result)
        atomic_json(path, receipt)
        if result.get('node_errors') or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', result.get('prompt_id', '')):
            raise RuntimeError('Engine did not accept the frozen workflow')
        return receipt

    def download(self, item, path):
        if (item.get('type') != 'output' or not re.fullmatch(r'[A-Za-z0-9_.-]+\.mp4', item.get('filename', ''))
                or '..' in item.get('subfolder', '').replace('\\', '/').split('/')
                or ':' in item.get('subfolder', '') or item.get('subfolder', '').startswith(('/', '\\'))):
            raise ValueError('Invalid engine output reference')
        partial = path.with_suffix('.partial.mp4')
        count = 0
        try:
            with self.opener.open(self.base + '/view?' + urlencode(item), timeout=120) as response, partial.open('xb') as stream:
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > 256 * 1024 * 1024:
                        raise ValueError('Engine output exceeds benchmark limit')
                    stream.write(chunk)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)


def gpu_observation():
    # One transient NVML failure gets one bounded retry; never retry low-memory readings.
    for attempt in range(2):
        try:
            result = subprocess.run(['nvidia-smi', '--id=0', '--query-gpu=memory.used,memory.total,utilization.gpu',
                                     '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5, check=True)
            return {'sampledAtMonotonic': time.perf_counter(), 'csv': result.stdout.strip(),
                    'queryAttempts': attempt+1, 'hostAvailableBytes': host_available_bytes()}
        except (OSError, subprocess.SubprocessError):
            pass
    return {'sampledAtMonotonic': time.perf_counter(), 'csv': None, 'queryAttempts': 2,
            'hostAvailableBytes': host_available_bytes()}


def host_available_bytes():
    try:
        import psutil
        return psutil.virtual_memory().available
    except (ImportError, OSError):
        return None  # Host telemetry is advisory; absent is unknown, not zero.


def enforce_memory_margin(observation, minimum):
    try:
        used, total, utilization = map(float, observation['csv'].split(','))
        if (not all(math.isfinite(v) for v in (used, total, utilization))
                or not 0 <= used <= total or total <= 0 or not 0 <= utilization <= 100):
            raise ValueError('Invalid GPU observation')
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('GPU memory is unobservable; refusing unmonitored benchmark') from exc
    if total-used < minimum:
        raise RuntimeError(f'Whole-device GPU memory margin below {minimum} MiB')
    return total-used


async def execute(plan, port):
    output = Path(plan['config']['outputRoot']).resolve() / plan['runId']
    output.mkdir(parents=True, exist_ok=True)
    state_path = output/'state.json'
    digest = hashlib.sha256(json_bytes(plan)).hexdigest()
    if state_path.exists():
        saved = read_json(state_path)
        if saved['planSha256'] != digest:
            raise ValueError('Run id belongs to another immutable configuration')
        if saved.get('completed'):
            for item in saved['results']:
                if file_hash(output/item['file']) != item['fileHash']:
                    raise ValueError('Completed benchmark media changed')
            return saved
        raise ValueError('Incomplete run retained; inspect original receipts, never resubmit automatically')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', port))
    engine = output/'engine'
    for name in ['input', 'output', 'temp', 'user']:
        (engine/name).mkdir(parents=True, exist_ok=True)
    for name, item in plan['inputs'].items():
        if file_hash(item['path']) != item['fileHash']:
            raise ValueError('Input changed after planning')
        shutil.copyfile(item['path'], engine/'input'/name)
        if file_hash(engine/'input'/name) != item['fileHash']:
            raise ValueError('Copied engine input differs from frozen source')
    identity = {'runId': plan['runId'], 'launchToken': secrets.token_hex(16),
                'planSha256': digest, 'profileNodes': plan['profileNodes'],
                'textConditionCache': plan['textConditionCache'],
                'guardedLauncherSha256': plan['code']['scripts/digital-human/start_guarded.py']}
    state = {'planSha256': digest, 'plan': plan, 'completed': False, 'qualityAccepted': False,
             'results': [], 'limitations': ['Raw experimental route, not production engine attestation',
             'Node profiling adds synchronization overhead; final speed requires unprofiled confirmation',
             'GPU nvidia-smi observations are sampled whole-device usage, not exact process peaks']}
    state['idleWholeDeviceGPU'] = gpu_observation()
    try:
        enforce_memory_margin(state['idleWholeDeviceGPU'], plan['minimumStartupFreeMiB'])
    except RuntimeError:
        state.update(errorType='GPUStartupPreflight', engineNeverStarted=True)
        atomic_json(state_path, state)
        raise
    atomic_json(state_path, state)
    env = os.environ.copy()
    env.update(DUOCAST_ENGINE_HOME=str(engine), DUOCAST_COMFY_ROOT=plan['config']['comfyRoot'],
               DUOCAST_ENGINE_PORT=str(port), DUOCAST_CUDA_BLOCKING=plan['cudaBlocking'],
               DUOCAST_BENCH_IDENTITY=json.dumps(identity), PYTHONIOENCODING='utf-8', CUDA_VISIBLE_DEVICES='0')
    env.pop('PYTHONPATH', None)
    env.pop('DUOCAST_ATTENTION_ADDON', None)
    if plan['attentionAddon']:
        addon = describe_package(plan['attentionAddon']['path'])
        if addon != plan['attentionAddon']:
            raise ValueError('Attention package changed before engine launch')
        env['DUOCAST_ATTENTION_ADDON'] = json.dumps(addon)
    client = LocalEngine(port)
    process = None
    started = time.perf_counter()
    try:
        with (output/'stdout.log').open('wb') as stdout, (output/'stderr.log').open('wb') as stderr:
            process = await spawn_owned(plan['config']['enginePython'], '-u',
                str(REPO/'scripts/digital-human/start_guarded.py'), cwd=plan['config']['comfyRoot'],
                env=env, stdout=stdout, stderr=stderr,
                **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
            for _ in range(180):
                try:
                    observed = client.get('/duocast-benchmark/identity')
                    if (observed['launchToken'] != identity['launchToken']
                            or observed['cudaLaunchBlocking'] != plan['cudaBlocking']):
                        raise ValueError('Wrong engine process/configuration')
                    if plan['attentionAddon'] and (observed.get('attentionAddon') or {}).get('treeHash') != plan['attentionAddon']['treeHash']:
                        raise ValueError('Wrong isolated attention package identity')
                    break
                except (OSError, TimeoutError):
                    if process.returncode is not None:
                        raise RuntimeError('Isolated engine exited during startup')
                    await asyncio.sleep(1)
            else:
                raise TimeoutError('Engine startup timed out')
            state.update(engineIdentity=observed, startupSeconds=time.perf_counter()-started)
            atomic_json(state_path, state)
            nodes = client.get('/object_info')
            for item in plan['segments']:
                segment = output/f'segment-{item["index"]}'
                segment.mkdir()
                graph = item['workflow']
                if {n['class_type'] for n in graph.values()} - set(nodes):
                    raise RuntimeError('Frozen workflow nodes unavailable')
                for name, source in plan['inputs'].items():
                    if file_hash(engine/'input'/name) != source['fileHash']:
                        raise ValueError('Owned engine input changed before inference')
                enforce_memory_margin(gpu_observation(), plan['minimumWholeDeviceFreeMiB'])
                calls_before = client.get('/duocast-benchmark/identity').get('attentionAddon')
                calls_before = calls_before['calls'] if calls_before else 0
                receipt = client.submit_once(segment/'receipt.json', graph, observed)
                prompt_id = receipt['prompt_id']
                print(json.dumps({'running': item['index'], 'promptId': prompt_id}), flush=True)
                tick = time.perf_counter()
                samples = []
                while time.perf_counter()-tick < 1800:
                    if time.perf_counter()-started > plan['maximumRunSeconds']:
                        raise TimeoutError('Declared whole-run wall limit exceeded; receipts retained')
                    samples.append(gpu_observation())
                    atomic_json(segment/'gpu-observations.json', samples)
                    enforce_memory_margin(samples[-1], plan['minimumWholeDeviceFreeMiB'])
                    history = client.get('/history/'+prompt_id).get(prompt_id)
                    if history:
                        break
                    if client.get('/duocast-benchmark/identity').get('promptWorkerAlive') is False:
                        raise RuntimeError('Engine prompt worker exited without history; original receipt retained')
                    await asyncio.sleep(3)
                else:
                    raise TimeoutError('Original task retained; benchmark engine will be stopped')
                observed_seconds = time.perf_counter()-tick
                atomic_json(segment/'history.json', history)
                if history.get('status', {}).get('status_str') != 'success':
                    raise RuntimeError('Model execution failed; inspect retained history')
                messages = dict(history['status']['messages'])
                if '199' in messages.get('execution_cached', {}).get('nodes', []):
                    raise ValueError('Sampler was cached; cannot count as inference benchmark')
                videos = history['outputs']['229']['gifs']
                video = next(v for v in videos if v.get('filename', '').endswith('.mp4'))
                raw = segment/'raw.mp4'
                client.download({key: video.get(key, '') for key in ['filename', 'subfolder', 'type']}, raw)
                media = inspect_media(raw, fps=25, width=item['nativeSize'][0], height=item['nativeSize'][1], require_audio=False)
                if not media['passed'] or media['video']['decodedFrames'] != item['modelFrames']:
                    raise ValueError('Generated media does not match frozen model window')
                entry = {'index': item['index'], 'promptId': prompt_id,
                         'engineSeconds': (messages['execution_success']['timestamp']-messages['execution_start']['timestamp'])/1000,
                         'pollObservedSeconds': observed_seconds, 'cachedNodeIds': messages.get('execution_cached', {}).get('nodes', []),
                         'file': str(raw.relative_to(output)), 'fileHash': file_hash(raw),
                         'media': media, 'sampledWholeDeviceGPU': samples}
                addon_identity = client.get('/duocast-benchmark/identity').get('attentionAddon')
                calls_after = addon_identity['calls'] if addon_identity else 0
                entry['successfulSageCalls'] = calls_after-calls_before
                if plan['variant'] in {'async-sage', 'async-sage-textcache'} and entry['successfulSageCalls'] <= 0:
                    raise ValueError('Sage backend was not actually executed')
                entry['textCache'] = client.get('/duocast-benchmark/identity').get('textCache')
                state['results'].append(entry)
                atomic_json(state_path, state)
                print(json.dumps({'completed': item['index'], 'engineSeconds': entry['engineSeconds']}), flush=True)
        state.update(completed=True, totalWallSeconds=time.perf_counter()-started)
    except BaseException as exc:
        state.update(errorType=type(exc).__name__, engineStoppedOnExit=True)
        raise
    finally:
        try:
            if process is not None:
                await process.stop()
            state['engineStoppedOnExit'] = True
        finally:
            atomic_json(state_path, state)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--variant', choices=VARIANTS, default='baseline')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--indices', nargs='+', type=int, default=[0, 2])
    parser.add_argument('--profile-nodes', action='store_true')
    parser.add_argument('--stage', choices=['plan', 'run'], default='plan')
    parser.add_argument('--port', type=int, default=8192)
    parser.add_argument('--timeline-limit', type=int, choices=[5, 16, 50], default=5,
                        help='Explicit maximum windows: screening / <=30s / <=120s')
    args = parser.parse_args(argv)
    if not 8192 <= args.port <= 8199:
        parser.error('Use an isolated benchmark port 8192..8199')
    plan = configure(args.config, args.variant, args.indices, args.run_id, args.profile_nodes, args.timeline_limit)
    if args.stage == 'plan':
        print(json.dumps({'scope': 'local-only plan; no HTTP or model calls', 'variant': args.variant,
                          'runId': args.run_id, 'indices': args.indices, 'planSha256': hashlib.sha256(json_bytes(plan)).hexdigest()}))
        return
    with state_lock(Path(plan['config']['outputRoot'])/'gpu-benchmark.json'):
        result = asyncio.run(execute(plan, args.port))
    print(json.dumps({'completed': result['completed'], 'qualityAccepted': False,
                      'engineExecutionSeconds': [r['engineSeconds'] for r in result['results']]}))


if __name__ == '__main__':
    main()
