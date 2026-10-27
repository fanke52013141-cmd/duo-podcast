"""Real FFmpeg evidence plus simulated engine protocol; never model acceptance."""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import wave

import httpx
import pytest

from duocast.adapters.expert_motion_provider import ExpertMotionVideoProvider
from duocast.adapters.expert_motion_video import build_expert_motion_workflow
from duocast.core.eventbus import EventBus
from duocast.jobs.manager import JobManager
from duocast.jobs.recovery import recover_on_startup
from duocast.storage.artifacts import ArtifactStore
from test_expert_motion import proposed, template, names


@pytest.fixture
def setup(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Real FFmpeg required for output integrity controls')
    artifacts = ArtifactStore(tmp_path / 'artifacts')
    manager = JobManager(tmp_path / 'jobs', EventBus(tmp_path / 'events.jsonl'))
    snapshot = {'projectSnapshot': {'id': 'episode', 'motionPack': {'id': 'pack', 'version': 'v1'}}}
    job = manager.submit('renders', 'episode', 'gpu', 'click', snapshot)
    provider = ExpertMotionVideoProvider(artifacts, None, base_url='http://engine.invalid',
        input_dir=tmp_path / 'engine-input', fast_runtime_root=tmp_path / 'runtime',
        job_manager=manager, poll_interval=.001, timeout_sec=.03, engine_fingerprint='c' * 64)
    segment = proposed()['segments'][0]
    folder = tmp_path / 'preparation'
    folder.mkdir()
    owned = names()
    files = {'anchorImage': owned['anchorImage'], 'referenceVideo': owned['referenceVideo'],
             'masterAudio': owned['masterAudio'], **{'audio' + r: n for r, n in owned['roleAudio'].items()},
             **{'mask' + r: n for r, n in owned['roleMasks'].items()}}
    for kind, name in list(files.items()):
        path = folder / name
        path.write_bytes(b'explicit protocol-only fixture, not model input')
        files[kind] = {'path': str(path), 'fileHash': artifacts._hash_file(str(path))}
    prepared = {'inputNames': owned, 'files': files}
    raw = tmp_path / 'model-output.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        f'color=c=blue:s=128x72:r=25:d={segment["modelFrames"]/25}', '-an', '-c:v', 'libx264',
        '-pix_fmt', 'yuv420p', str(raw)], check=True, capture_output=True, timeout=20)
    return provider, manager, job, segment, prepared, artifacts.root / 'video_expert_motion' / ('a' * 64), raw


def engine(setup, *, fail_post=False, pending=False, bad_output=False, terminal=False, corrupt=False):
    provider, manager, job, segment, prepared, folder, raw = setup
    wf = build_expert_motion_workflow(template(), segment, prepared['inputNames'], output_prefix=segment['dependencyHash'])['workflow']
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/object_info':
            return httpx.Response(200, json={n['class_type']: {} for n in wf.values()})
        if request.url.path == '/prompt':
            if fail_post:
                raise httpx.ReadTimeout('Lost response after engine received request', request=request)
            return httpx.Response(200, json={'prompt_id': 'known-task', 'node_errors': {}})
        if request.url.path == '/history/known-task':
            if pending:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={'known-task': {'status': {'status_str': 'error' if terminal else 'success'},
                'outputs': {'229': {'gifs': [{'filename': '../escape.mp4' if bad_output else 'model.mp4',
                                           'subfolder': 'expert_motion', 'type': 'output'}]}}}})
        if request.url.path == '/view':
            return httpx.Response(200, content=b'broken model media' if corrupt else raw.read_bytes())
        raise AssertionError(request.url.path)

    return httpx.MockTransport(handler), calls


async def run_segment(setup, transport):
    provider, manager, job, segment, prepared, folder, _ = setup
    async with httpx.AsyncClient(base_url='http://engine.invalid', transport=transport) as client:
        return await provider._segment(client, job, 'a' * 64, segment, prepared, folder, template())


def test_real_video_decode_core_trim_and_completed_cache_never_resubmit(setup):
    transport, calls = engine(setup)
    core, record = asyncio.run(run_segment(setup, transport))
    assert core.is_file() and record['state'] == 'complete'
    assert record['actualMotionRetentionVerified'] is False
    assert record['actualGraphCompleted'] is True
    assert record['promptId'] == 'known-task'
    assert record['cacheHit'] is False
    second, replayed = asyncio.run(run_segment(setup, transport))
    assert second == core and replayed['cacheHit'] is True
    assert calls.count(('POST', '/prompt')) == 1
    assert calls.count(('GET', '/view')) == 1
    assert calls.count(('GET', '/history/known-task')) == 1
    assert len(setup[0].artifacts._manifest()['artifacts']) == 2
    info = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_entries',
        'stream=nb_read_frames', '-of', 'json', str(core)], check=True, capture_output=True).stdout)
    assert int(info['streams'][0]['nb_read_frames']) == 64


def test_lost_post_response_persists_intent_and_restart_refuses_resampling(setup):
    transport, calls = engine(setup, fail_post=True)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(run_segment(setup, transport))
    provider, manager, job, segment, prepared, folder, raw = setup
    restored = JobManager(manager.root, EventBus(manager.root.parent / 'restored-events.jsonl'))
    restored.restore(recover_on_startup(manager.root))
    provider.manager = restored
    recovered = restored.get(job.id)
    recovered_setup = provider, restored, recovered, segment, prepared, folder, raw
    with pytest.raises(RuntimeError, match='SUBMISSION_UNKNOWN'):
        asyncio.run(run_segment(recovered_setup, transport))
    assert calls.count(('POST', '/prompt')) == 1
    facts = provider.inspect_recovery(recovered)
    assert len(facts['unknownSubmissions']) == 1 and facts['resubmissionAllowed'] is False


def test_known_pending_task_reconnects_after_timeout_without_second_post(setup):
    waiting, original_calls = engine(setup, pending=True)
    with pytest.raises(TimeoutError, match='EXPERT_PENDING'):
        asyncio.run(run_segment(setup, waiting))
    success, new_calls = engine(setup)
    core, record = asyncio.run(run_segment(setup, success))
    assert core.is_file() and record['promptId'] == 'known-task'
    assert original_calls.count(('POST', '/prompt')) == 1
    assert ('POST', '/prompt') not in new_calls


def test_unknown_engine_identity_blocks_new_inference_but_frozen_known_task_can_query(setup):
    provider = setup[0]
    provider.engine_fingerprint = None
    transport, calls = engine(setup)
    with pytest.raises(ValueError, match='fingerprint'):
        asyncio.run(run_segment(setup, transport))
    assert ('POST', '/prompt') not in calls
    provider.engine_fingerprint = 'c' * 64
    waiting, calls = engine(setup, pending=True)
    with pytest.raises(TimeoutError):
        asyncio.run(run_segment(setup, waiting))
    provider.engine_fingerprint = None
    success, resumed_calls = engine(setup)
    core, record = asyncio.run(run_segment(setup, success))
    assert core.is_file() and record['engineFingerprint'] == 'c' * 64
    assert ('POST', '/prompt') not in resumed_calls


def test_terminal_external_failure_cannot_be_automatically_retried(setup):
    transport, calls = engine(setup, terminal=True)
    with pytest.raises(RuntimeError, match='ENGINE_FAILED'):
        asyncio.run(run_segment(setup, transport))
    with pytest.raises(RuntimeError, match='already failed'):
        asyncio.run(run_segment(setup, transport))
    assert calls.count(('POST', '/prompt')) == 1
    assert setup[0].inspect_recovery(setup[2])['canResumeQuery'] is False


@pytest.mark.parametrize('bad_output,corrupt', [(True, False), (False, True)])
def test_unsafe_or_incomplete_engine_output_is_never_registered(setup, bad_output, corrupt):
    transport, calls = engine(setup, bad_output=bad_output, corrupt=corrupt)
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(run_segment(setup, transport))
    assert setup[0].artifacts._manifest()['artifacts'] == []
    assert list(setup[5].rglob('download.partial.mp4')) == []
    assert calls.count(('POST', '/prompt')) == 1


def test_registration_before_final_checkpoint_is_recovered_without_overwrite(setup, monkeypatch):
    transport, calls = engine(setup)
    provider = setup[0]
    original = provider._remember
    failed = False

    def crash(job, request_key, segment_key=None, record=None):
        nonlocal failed
        if record and record.get('state') == 'complete' and not failed:
            failed = True
            raise RuntimeError('Simulated local crash after immutable media and complete marker')
        return original(job, request_key, segment_key, record)

    monkeypatch.setattr(provider, '_remember', crash)
    with pytest.raises(RuntimeError, match='local crash'):
        asyncio.run(run_segment(setup, transport))
    assert len(provider.artifacts._manifest()['artifacts']) == 2
    core, record = asyncio.run(run_segment(setup, transport))
    assert core.is_file() and record['cacheHit'] is True
    assert calls.count(('POST', '/prompt')) == calls.count(('GET', '/view')) == 1


def test_prompt_id_saved_before_job_checkpoint_is_visible_to_safe_recovery(setup, monkeypatch):
    transport, calls = engine(setup)
    provider, _, job, *_ = setup
    original = provider._remember
    failed = False

    def crash(job, request_key, segment_key=None, record=None):
        nonlocal failed
        if record and record.get('state') == 'pending' and not failed:
            failed = True
            raise RuntimeError('Crash after durable external task ID, before job checkpoint')
        return original(job, request_key, segment_key, record)

    monkeypatch.setattr(provider, '_remember', crash)
    with pytest.raises(RuntimeError, match='durable external'):
        asyncio.run(run_segment(setup, transport))
    facts = provider.inspect_recovery(job)
    assert facts['canResumeQuery'] is True and facts['unknownSubmissions'] == []
    assert facts['knownTasks'][0]['promptId'] == 'known-task'
    core, record = asyncio.run(run_segment(setup, transport))
    assert core.is_file() and record['state'] == 'complete'
    assert calls.count(('POST', '/prompt')) == 1


def test_server_job_owner_and_frozen_request_are_required(setup):
    provider, manager, job, *_ = setup
    request = {**job.input_snapshot, 'jobId': job.id}
    assert provider._job(request).id == job.id
    with pytest.raises(ValueError, match='own'):
        provider._job({**request, 'seed': 999})
    with pytest.raises(ValueError, match='owned'):
        provider._job(job.input_snapshot)
    provider._remember(job, 'a' * 64)
    with pytest.raises(ValueError, match='changed'):
        provider._remember(job, 'b' * 64)


def test_final_export_uses_same_master_pcm_and_changing_encoding_does_not_submit(setup, tmp_path):
    transport, calls = engine(setup)
    core, record = asyncio.run(run_segment(setup, transport))
    provider, manager, job, segment, prepared, folder, _ = setup
    rate, samples = 48000, 64 * 48000 // 25
    audio = folder / 'audio/master.wav'
    audio.parent.mkdir(parents=True)
    pcm = b'\x01\x00' * samples
    with wave.open(str(audio), 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(pcm)
    hashes = {'master': hashlib.sha256(pcm).hexdigest()}
    request = {'projectSnapshot': {'shotStyle': 'twoShot'}, 'resolution': '854x480', 'fps': 25}
    plan = {'totalFrames': 64, 'packId': 'pack', 'packVersion': 'v1'}
    result = asyncio.run(provider._finish(job, request, 'a' * 64, folder, [core], [record], plan, samples, rate, hashes))
    final = Path(result['meta']['path'])
    reference = tmp_path / 'reference.m4a'
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(audio), '-t', str(samples / rate), '-c:a', 'aac',
                    '-b:a', '192k', str(reference)], check=True, capture_output=True, timeout=20)
    def decoded(path):
        return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
            '-ac', '1', '-ar', '48000', '-f', 's16le', '-'], check=True, capture_output=True, timeout=20).stdout
    assert decoded(final) == decoded(reference)
    other = asyncio.run(provider._finish(job, {**request, 'resolution': '1280x720'}, 'a' * 64, folder,
        [core], [record], plan, samples, rate, hashes))
    assert other['artifactIds'] != result['artifactIds']
    assert decoded(Path(other['meta']['path'])) == decoded(final)
    assert calls.count(('POST', '/prompt')) == 1
    assert result['meta']['qualityStatus'] == 'needs_review'
    assert result['meta']['automaticAdoption'] is False
