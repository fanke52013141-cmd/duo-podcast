"""Benchmark safety: planning, frozen input, unknown submissions and startup defaults."""
import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO/'scripts/acceptance'))
sys.path.insert(0, str(REPO/'scripts/digital-human'))
bench = importlib.import_module('benchmark_expert')
probe = importlib.import_module('benchmark_probe')
report = importlib.import_module('report_expert_benchmark')
addon = importlib.import_module('attention_addon')


@pytest.fixture
def prepared(tmp_path):
    audio = tmp_path/'audio.wav'
    audio.write_bytes(b'frozen-test-input')
    workflow = tmp_path/'workflow.json'
    workflow.write_text(json.dumps({'workflow': {
        '134': {'inputs': {'blocks_to_swap': 38}, 'class_type': 'WanVideoBlockSwap'},
        '199': {'inputs': {'seed': 42, 'steps': 4, 'start_step': 2}, 'class_type': 'WanVideoSampler'},
        '229': {'inputs': {'filename_prefix': 'original'}, 'class_type': 'VHS_VideoCombine'}}}), encoding='utf-8')
    source = tmp_path/'plan.json'
    source.write_text(json.dumps({'segments': [{'file': str(workflow), 'modelFrames': 81,
        'coreFrames': 64, 'trimStartFrame': 8, 'trimEndFrame': 72,
        'inputs': {'audio': {'path': str(audio), 'fileHash': bench.file_hash(audio)}}}]}), encoding='utf-8')
    path = tmp_path/'config.json'
    path.write_text(json.dumps({'sourcePlan': str(source), 'enginePython': sys.executable,
                               'comfyRoot': str(tmp_path), 'outputRoot': str(tmp_path/'runs')}), encoding='utf-8')
    return path, audio, workflow


def test_plan_performs_no_network_process_or_inference(prepared, monkeypatch, capsys):
    path, _, _ = prepared
    forbidden = Mock(side_effect=AssertionError('Planning must remain local'))
    monkeypatch.setattr(bench.asyncio, 'run', forbidden)
    monkeypatch.setattr(bench.LocalEngine, 'get', forbidden)
    monkeypatch.setattr(bench.socket, 'socket', forbidden)
    bench.main(['--config', str(path), '--run-id', 'plan-test', '--indices', '0'])
    assert 'no HTTP or model calls' in capsys.readouterr().out
    forbidden.assert_not_called()


def test_only_declared_graph_parameters_change(prepared):
    path, _, _ = prepared
    baseline = bench.configure(path, 'baseline', [0], 'same-id', False)
    candidate = bench.configure(path, 'async-swap36', [0], 'same-id', False)
    expected = deepcopy(baseline)
    expected.update(variant='async-swap36', cudaBlocking='0', blocksToSwap=36)
    expected['segments'][0]['workflow']['134']['inputs']['blocks_to_swap'] = 36
    assert candidate == expected
    assert candidate['segments'][0]['workflow']['199']['inputs'] == {'seed': 42, 'steps': 4, 'start_step': 2}
    assert 'scripts/digital-human/start_guarded.py' in baseline['code']


def test_changed_prepared_input_rejected(prepared):
    path, audio, _ = prepared
    audio.write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        bench.configure(path, 'baseline', [0], 'test', False)


@pytest.mark.parametrize('indices,run_id', [([0, 0], 'test'), ([99], 'test'), ([0], '../escape')])
def test_invalid_selection_or_run_id(prepared, indices, run_id):
    with pytest.raises(ValueError):
        bench.configure(prepared[0], 'baseline', indices, run_id, False)


def test_unknown_config_fields_rejected(prepared):
    path = prepared[0]
    data = json.loads(path.read_text(encoding='utf-8'))
    data['arbitraryShell'] = 'never-execute'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='whitelisted'):
        bench.configure(path, 'baseline', [0], 'test', False)


def test_unknown_submission_is_not_posted_twice(tmp_path):
    client = bench.LocalEngine(8192)
    client.opener = Mock()
    client.opener.open.side_effect = TimeoutError('response lost')
    path = tmp_path/'receipt.json'
    with pytest.raises(TimeoutError):
        client.submit_once(path, {'frozen': 1}, {'localExperiment': True})
    assert json.loads(path.read_text())['submissionIntent'] is True
    with pytest.raises(ValueError, match='never repeat'):
        client.submit_once(path, {'frozen': 1}, {'localExperiment': True})
    assert client.opener.open.call_count == 1
    with pytest.raises(ValueError, match='changed'):
        client.submit_once(path, {'frozen': 2}, {'localExperiment': True})


def test_completed_media_integrity_checked_before_reuse(prepared):
    plan = bench.configure(prepared[0], 'baseline', [0], 'reuse-test', False)
    output = Path(plan['config']['outputRoot'])/'reuse-test'
    output.mkdir(parents=True)
    media = output/'raw.mp4'
    media.write_bytes(b'original')
    saved = {'completed': True, 'planSha256': bench.hashlib.sha256(bench.json_bytes(plan)).hexdigest(),
             'results': [{'file': 'raw.mp4', 'fileHash': bench.file_hash(media)}]}
    bench.atomic_json(output/'state.json', saved)
    assert asyncio.run(bench.execute(plan, 8192)) == saved
    media.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='changed'):
        asyncio.run(bench.execute(plan, 8192))


def test_compatibility_defaults_are_preserved():
    assert probe.launch_options({}) == (8191, '1')
    assert probe.launch_options({'CUDA_LAUNCH_BLOCKING': '0'}) == (8191, '1')
    assert probe.launch_options({'DUOCAST_ENGINE_PORT': '8192', 'DUOCAST_CUDA_BLOCKING': '0'}) == (8192, '0')


def test_loaded_gpu_rejected_before_engine_launch(prepared, monkeypatch):
    plan = bench.configure(prepared[0], 'async', [0], 'preflight-test', False)
    monkeypatch.setattr(bench, 'gpu_observation', lambda: {'csv': '12249,16303,1'})
    forbidden = Mock(side_effect=AssertionError('No engine on loaded GPU'))
    monkeypatch.setattr(bench, 'spawn_owned', forbidden)
    with pytest.raises(RuntimeError, match='12288'):
        asyncio.run(bench.execute(plan, 8192))
    forbidden.assert_not_called()
    state = bench.read_json(Path(plan['config']['outputRoot'])/'preflight-test/state.json')
    assert state['engineNeverStarted'] and state['errorType'] == 'GPUStartupPreflight'
    assert state['results'] == [] and not state['completed']


@pytest.mark.parametrize('csv', ['nan,16303,1', '1000,inf,1', '-1,16303,1', '17000,16303,1'])
def test_invalid_gpu_reading_cannot_bypass_guard(csv):
    with pytest.raises(RuntimeError, match='unobservable'):
        bench.enforce_memory_margin({'csv': csv}, 1536)


def test_transient_nvml_query_retried_once(monkeypatch):
    query = Mock(side_effect=[bench.subprocess.TimeoutExpired('nvidia-smi', 5),
                              Mock(stdout='1000,16303,1')])
    monkeypatch.setattr(bench.subprocess, 'run', query)
    result = bench.gpu_observation()
    assert result['csv'] == '1000,16303,1' and result['queryAttempts'] == 2
    assert query.call_count == 2


def test_persistent_nvml_failure_stays_bounded(monkeypatch):
    query = Mock(side_effect=OSError('unavailable'))
    monkeypatch.setattr(bench.subprocess, 'run', query)
    result = bench.gpu_observation()
    assert result['csv'] is None and query.call_count == 2
    with pytest.raises(RuntimeError, match='unobservable'):
        bench.enforce_memory_margin(result, 1536)


def test_sage_requires_explicit_isolated_package(prepared):
    with pytest.raises(ValueError, match='attentionSite'):
        bench.configure(prepared[0], 'async-sage', [0], 'sage-test', False)


def test_sage_changes_only_attention_backend_and_records_package(prepared, tmp_path):
    config_path, _, workflow_path = prepared
    built = bench.read_json(workflow_path)
    built['workflow']['122'] = {'inputs': {'attention_mode': 'sdpa'}, 'class_type': 'WanVideoModelLoader'}
    workflow_path.write_text(json.dumps(built), encoding='utf-8')
    site = tmp_path/'site'; (site/'sageattention').mkdir(parents=True)
    (site/'sageattention/__init__.py').write_text('def sageattn(): return 1', encoding='utf-8')
    config = bench.read_json(config_path); config['attentionSite'] = str(site)
    config_path.write_text(json.dumps(config), encoding='utf-8')
    control = bench.configure(config_path, 'async', [0], 'same-id', False)
    candidate = bench.configure(config_path, 'async-sage', [0], 'same-id', False)
    expected = deepcopy(control); expected['variant'] = 'async-sage'
    expected['segments'][0]['workflow']['122']['inputs']['attention_mode'] = 'sageattn'
    assert candidate == expected
    cached = bench.configure(config_path, 'async-sage-textcache', [0], 'same-id', False)
    expected_cache = deepcopy(candidate)
    expected_cache.update(variant='async-sage-textcache', textConditionCache=True)
    assert cached == expected_cache  # No sampling/model/graph parameters change.
    initial = addon.describe_package(site)
    (site/'sageattention/__init__.py').write_text('def sageattn(): return 2', encoding='utf-8')
    assert addon.describe_package(site)['treeHash'] != initial['treeHash']
    with pytest.raises(ValueError, match='changed'):
        addon.load_package({'DUOCAST_ATTENTION_ADDON': json.dumps(initial)})


def test_addon_default_does_not_import_sage():
    assert addon.load_package({}) is None


def test_live_http_thread_cannot_hide_dead_prompt_worker(monkeypatch):
    from types import SimpleNamespace
    worker=SimpleNamespace(name='Thread-7 (prompt_worker)',is_alive=lambda:False)
    server=SimpleNamespace(name='HTTP server',is_alive=lambda:True)
    monkeypatch.setattr(probe.threading,'enumerate',lambda:[worker,server])
    assert not probe.prompt_worker_alive()
    worker.is_alive=lambda:True
    assert probe.prompt_worker_alive()


def test_long_timelines_require_explicit_bounded_selection(prepared):
    config=bench.read_json(prepared[0]);path=Path(config['sourcePlan'])
    source=bench.read_json(path);source['segments']*=6
    source.update(sampleRate=48000,sampleCount=120*48000)
    path.write_text(json.dumps(source),encoding='utf-8')
    with pytest.raises(ValueError,match='five|5'):
        bench.configure(prepared[0],'async',list(range(6)),'long',False)
    result=bench.configure(prepared[0],'async',list(range(6)),'long',False,max_windows=50)
    assert result['maximumSelectedWindows']==50 and result['maximumRunSeconds']==4200
    with pytest.raises(ValueError,match='duration'):
        bench.configure(prepared[0],'async',list(range(6)),'long',False,max_windows=16)
    source['sampleCount']+=1;path.write_text(json.dumps(source),encoding='utf-8')
    with pytest.raises(ValueError,match='duration'):
        bench.configure(prepared[0],'async',[0],'long',False,max_windows=50)


@pytest.mark.parametrize('values', [{'DUOCAST_ENGINE_PORT': '80'}, {'DUOCAST_CUDA_BLOCKING': 'unsafe'}])
def test_invalid_launch_settings_rejected(values):
    with pytest.raises(ValueError):
        probe.launch_options(values)


@pytest.mark.parametrize('field,value', [('profileNodes', False), ('sourcePlanSha256', 'other'),
                                        ('code', {'different': 'code'}), ('completed', False)])
def test_report_rejects_incomparable_runs(field, value):
    baseline = {'runId': 'base', 'completed': True, 'profileNodes': True,
                'sourcePlanSha256': 'same', 'code': {'same': 'code'}, 'engineExecutionSumSeconds': 10,
                'segments': [{'index': 0, 'engineSeconds': 10, 'videoHash': 'v', 'audioHash': 'a'}]}
    candidate = deepcopy(baseline)
    candidate[field] = value
    with pytest.raises(ValueError, match='differ'):
        report.compare(baseline, candidate)


def test_pixel_equality_does_not_create_quality_or_statistical_approval():
    baseline = {'runId': 'base', 'completed': True, 'profileNodes': True,
                'sourcePlanSha256': 'same', 'code': {'same': 'code'}, 'engineExecutionSumSeconds': 10,
                'segments': [{'index': 0, 'engineSeconds': 10, 'videoHash': 'v', 'audioHash': 'a'}]}
    candidate = deepcopy(baseline)
    candidate.update(runId='candidate', engineExecutionSumSeconds=5)
    candidate['segments'][0]['engineSeconds'] = 5
    result = report.compare(baseline, candidate)
    assert result['observedExecutionSpeedup'] == 2
    assert result['pairedSegments'][0]['decodedVideoEqual']
    assert result['qualityAccepted'] is False
    assert result['statisticalConclusion'] is False


def test_gpu_margin_prevents_unsafe_or_unobserved_runs():
    assert bench.enforce_memory_margin({'csv': '13022,16303,99'}, 1536) == 3281
    with pytest.raises(RuntimeError, match='margin'):
        bench.enforce_memory_margin({'csv': '15613,16303,99'}, 1536)
    with pytest.raises(RuntimeError, match='unobservable'):
        bench.enforce_memory_margin({'csv': None}, 1536)


def test_report_rejects_background_gpu_load_change():
    baseline = {'runId': 'base', 'completed': True, 'profileNodes': True,
                'sourcePlanSha256': 'same', 'code': {'same': 'code'}, 'engineExecutionSumSeconds': 10,
                'idleWholeDeviceGPU': {'csv': '1000,16303,1'},
                'segments': [{'index': 0, 'engineSeconds': 10, 'videoHash': 'v', 'audioHash': 'a'}]}
    candidate = deepcopy(baseline)
    candidate['idleWholeDeviceGPU'] = {'csv': '2000,16303,1'}
    with pytest.raises(ValueError, match='background workload'):
        report.compare(baseline, candidate)


@pytest.mark.parametrize('variant,field,value', [('async-nonblocking','use_non_blocking',True),
                                                ('async-prefetch1','prefetch_blocks',1)])
def test_transfer_experiments_change_only_one_graph_field(prepared,variant,field,value):
    parent=bench.configure(prepared[0],'async',[0],'same-id',False)
    candidate=bench.configure(prepared[0],variant,[0],'same-id',False)
    expected=deepcopy(parent)
    expected['variant']=variant
    expected['segments'][0]['workflow']['134']['inputs'][field]=value
    assert candidate==expected
