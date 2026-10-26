"""Explicit API evidence stages; no model/network generation is mocked as real."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from duocast.api import motion_media, motion_packs, motion_coordinates
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.motion_packs import MotionPackStore

MODULE = Path(__file__).parents[3] / 'scripts/acceptance/run_motion_api_acceptance.py'
sys.path.insert(0, str(MODULE.parent))
spec = importlib.util.spec_from_file_location('motion_api_acceptance_test', MODULE)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class APIAdapter:
    def __init__(self, client):
        self.client, self.calls = client, []

    def request(self, method, path, *, payload=None, raw=None):
        self.calls.append((method, path))
        response = self.client.request(method, path, json=payload) if raw is None else self.client.request(method, path, content=raw)
        if response.status_code >= 400:
            raise ValueError('Rejected HTTP ' + str(response.status_code))
        return response.status_code, response.json()


@pytest.fixture
def local_api(tmp_path):
    app = FastAPI()
    artifacts = ArtifactStore(tmp_path / 'artifacts')
    app.state.artifacts = artifacts
    app.state.motion_pack_store = MotionPackStore(tmp_path / 'packs', artifacts.by_id)
    for router in (motion_media.router, motion_packs.router, motion_coordinates.router):
        app.include_router(router)
    with TestClient(app) as client:
        yield APIAdapter(client)


def config(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Actual FFmpeg needed')
    image, video = tmp_path / 'image.png', tmp_path / 'video.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=96x54', '-frames:v', '1', str(image)],
                   check=True, capture_output=True, timeout=20)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=96x54:r=25:d=1',
                    '-c:v', 'mpeg4', str(video)], check=True, capture_output=True, timeout=20)
    (tmp_path / 'boxes.json').write_text(json.dumps([[1, 1, 20, 20]] * 25))
    ref = {'referenceImageFile': 'image.png', 'characterId': 'test', 'objectType': 'articulated_object'}
    return {'name': 'Explicit test pack', 'characters': {'A': ref, 'B': ref},
            'cameras': [{'id': 'dual', 'mode': 'twoShot', 'masterImageFile': 'image.png'}],
            'clips': [{'id': 'A-talk', 'role': 'A', 'cameraId': 'dual', 'state': 'speaking', 'videoFile': 'video.mp4',
                'coordinatesFile': 'boxes.json', 'inspection': {'bodyRegions': [{'x': 0, 'y': 0, 'width': .5, 'height': 1}],
                    'exclusionRegions': [{'x': 0, 'y': 0, 'width': .2, 'height': .2}],
                    'backgroundRegion': {'x': .6, 'y': 0, 'width': .4, 'height': 1}}}]}


def test_plan_never_uses_http_and_mutations_are_replayed_without_duplicate_writes(tmp_path, local_api):
    value = config(tmp_path)
    run = tool.AcceptanceRun(value, tmp_path, tmp_path / 'state.json', 'http://127.0.0.1:8137', api=local_api)
    assert run.plan()['scope'] == 'plan_only_no_HTTP_or_model_calls'
    assert local_api.calls == []
    assert not run.path.exists()
    first = run.import_assets()
    assert first['adopted'] is False
    writes = sum(method != 'GET' for method, _ in local_api.calls)
    assert run.import_assets() == first
    assert sum(method != 'GET' for method, _ in local_api.calls) == writes
    run.coordinates()
    run.coordinates()  # Current revision has advanced, but no duplicate coordinate write.
    run.check()
    count = sum(method != 'GET' for method, _ in local_api.calls)
    run.check()
    assert sum(method != 'GET' for method, _ in local_api.calls) == count
    pack = run.pack()
    assert pack['checks']['A-talk']['report']['status'] == 'failed'
    assert pack['reviews'] == {}
    assert run.review({'clips': [{'clipId': 'A-talk', 'accepted': True, 'reason': 'Explicit review'}]})['pending']
    with pytest.raises(ValueError):
        run.review({'clips': [{'clipId': 'A-talk', 'accepted': True, 'reason': 'Explicit review'}]}, apply=True)
    assert run.state['fullAcceptancePassed'] is False


def test_lost_post_response_is_persisted_and_never_blindly_repeated(tmp_path):
    class LostResponse:
        calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError('Server may have accepted input')

    api = LostResponse()
    state = tmp_path / 'state.json'
    run = tool.AcceptanceRun({}, tmp_path, state, 'http://127.0.0.1:8137', api=api)
    with pytest.raises(TimeoutError):
        run.once('create', 'POST', '/api/motion-packs', {'name': 'test'})
    restored = tool.AcceptanceRun({}, tmp_path, state, 'http://127.0.0.1:8137', api=api)
    with pytest.raises(ValueError, match='unresolved'):
        restored.once('create', 'POST', '/api/motion-packs', {'name': 'test'})
    assert api.calls == 1


@pytest.mark.parametrize('url', ['https://127.0.0.1:8137', 'http://example.com', 'http://127.0.0.1.evil.test',
                                      'http://user:password@localhost', 'http://localhost/api', 'http://localhost?token=secret'])
def test_only_explicit_local_api_allowed(url):
    with pytest.raises(ValueError):
        tool.local_base(url)


def test_redirect_handler_refuses_local_and_external_redirects():
    with pytest.raises(ValueError, match='redirect'):
        tool.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com/upload')


def test_model_stages_need_explicit_flag_and_current_human_gate(tmp_path):
    class API:
        calls = []

        def request(self, method, path, **kwargs):
            self.calls.append(path)
            return 200, {'id': 'episode', 'currentDraftRevision': 'R1', 'approvals': []}

    api = API()
    run = tool.AcceptanceRun({}, tmp_path, tmp_path / 'state.json', 'http://localhost:8137', api=api)
    with pytest.raises(ValueError, match='allow-model-calls'):
        run.prepare()
    assert api.calls == []
    run.state['projectId'] = 'episode'
    assert run.generate('voice', allowed=True)['pending']
    assert api.calls == ['/api/projects/episode']
    assert run.state['jobs'] == {}


def test_simulated_media_cannot_be_verified_as_real(tmp_path):
    class API:
        def request(self, *args, **kwargs):
            return 200, {'id': 'job', 'status': 'succeeded', 'result': {'simulated': True, 'artifactIds': ['fake']}}

        def download(self, *args):
            pytest.fail('Simulated artifact must not be downloaded as real evidence')

    run = tool.AcceptanceRun({}, tmp_path, tmp_path / 'state.json', 'http://localhost:8137', api=API())
    run.state['jobs']['render'] = {'id': 'job'}
    assert run.verify()['realMediaChecked'] == []
    assert run.state['media']['render']['passed'] is False


def test_state_config_changes_are_not_silently_reused(tmp_path):
    path = tmp_path / 'state.json'
    run = tool.AcceptanceRun({'name': 'first'}, tmp_path, path, 'http://localhost:8137')
    run.save()
    with pytest.raises(ValueError, match='different frozen config'):
        tool.AcceptanceRun({'name': 'changed'}, tmp_path, path, 'http://localhost:8137')


def test_completed_submit_receipt_recovers_after_crash_before_job_pointer_save(tmp_path):
    class API:
        calls = []

        def request(self, method, path, **kwargs):
            self.calls.append((method, path))
            return 200, {'id': 'known-job', 'status': 'succeeded', 'result': {'simulated': False}}

    api = API()
    run = tool.AcceptanceRun({}, tmp_path, tmp_path / 'state.json', 'http://localhost:8137', api=api)
    run.state['operations']['submit-voice'] = {'status': 'done', 'response': {'jobId': 'known-job'}}
    job = run.submit_job('voice', '/api/projects/episode/voice/synthesize', {'revisionId': 'later-revision'})
    assert job['id'] == 'known-job'
    assert api.calls == [('GET', '/api/jobs/known-job')]


def test_lost_preparation_response_can_reconcile_only_original_token_owner_and_kind(tmp_path):
    class API:
        calls = []
        submitted = None

        def request(self, method, path, **kwargs):
            self.calls.append((method, path))
            if method == 'POST':
                self.submitted = kwargs['payload']
                raise TimeoutError('Accepted before connection was lost')
            return 200, [{'id': 'original-job', 'kind': 'motion.prepare', 'ownerType': 'motionPack',
                'ownerId': 'pack', 'clientToken': self.submitted['clientToken'], 'status': 'running',
                'providerTaskId': 'paid-task', 'inputSnapshot': {'request': self.submitted}},
                {'id': 'other-owner', 'kind': 'motion.prepare', 'ownerType': 'motionPack',
                 'ownerId': 'different-pack', 'clientToken': self.submitted['clientToken'], 'status': 'running'}]

    api = API()
    run = tool.AcceptanceRun({}, tmp_path, tmp_path / 'state.json', 'http://localhost:8137', api=api)
    run.state['packId'] = 'pack'
    with pytest.raises(TimeoutError):
        run.submit_job('prepare-0', '/api/motion-packs/pack/prepare', {'expectedRevision': 1, 'cameraId': 'dual'})
    result = run.reconcile('prepare-0')
    assert result == {'reconciledJobId': 'original-job', 'status': 'running', 'newSubmission': False}
    assert sum(method == 'POST' for method, _ in api.calls) == 1
    assert run.state['jobs']['prepare-0']['providerTaskId'] == 'paid-task'
    assert run.state['operations']['submit-prepare-0']['reconciledBy'].startswith('GET')


def test_registered_real_media_decode_and_pcm_comparison_never_approve_quality(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Actual FFmpeg needed')
    media = tmp_path / 'real-fixture.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=96x54:r=25:d=0.4',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.4', '-c:v', 'libx264', '-c:a', 'aac',
                    '-shortest', str(media)], check=True, capture_output=True, timeout=20)
    content_hash = tool.input_record(media)['sha256']

    class RecordedAPI:
        # These are explicitly fixture protocol replies, not model validation.
        def request(self, method, path, **kwargs):
            if path == '/api/artifacts':
                return 200, {'artifacts': [{'artifactId': 'fixture-video', 'kind': 'video', 'fileHash': content_hash}]}
            kind = 'sample.generate' if path.endswith('sample-job') else 'renders'
            return 200, {'id': path.split('/')[-1], 'projectId': 'episode', 'kind': kind,
                'inputSnapshot': {'projectSnapshot': {'motionPack': {'id': 'pack', 'version': 'v1'}, 'shotStyle': 'twoShot'}},
                'status': 'succeeded', 'result': {'simulated': False,
                'artifactIds': ['fixture-video'], 'meta': {'resolution': '96x54', 'fps': 25, 'durationSec': .4}}}

        def download(self, id, target):
            shutil.copyfile(media, target)
            return tool.input_record(target)

    run = tool.AcceptanceRun({}, tmp_path, tmp_path / 'state.json', 'http://localhost:8137', api=RecordedAPI())
    run.state.update(projectId='episode', pin={'id': 'pack', 'version': 'v1'}, shotStyle='twoShot')
    run.state['jobs'] = {'sample': {'id': 'sample-job'}, 'render': {'id': 'render-job'}}
    result = run.verify()
    assert result['realMediaChecked'] == ['sample', 'render']
    assert run.state['pairedAudio']['passed'] is True
    assert run.state['fullAcceptancePassed'] is False
    assert run.state['media']['render']['qualityApproved'] is False
    assert run.verify()['realMediaChecked'] == ['sample', 'render']
    Path(run.state['media']['render']['input']['path']).write_bytes(b'tampered')
    with pytest.raises(ValueError, match='changed'):
        run.verify()


def test_download_limit_cleans_partial_and_redirect_never_follows(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if 'redirect' in self.path:
                self.send_response(302)
                self.send_header('Location', 'http://example.com/private-input')
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'12345')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(tool, 'MAX_MEDIA', 4)
        api = tool.LocalAPI('http://127.0.0.1:' + str(server.server_port))
        target = tmp_path / 'download.mp4'
        with pytest.raises(ValueError, match='limit'):
            api.download('video', target)
        assert not target.exists()
        with pytest.raises(ValueError, match='redirect'):
            api.download('redirect', target)
        assert not target.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('crash', [False, True])
def test_cli_process_state_lock_rejects_concurrent_import_and_releases_after_exit(tmp_path, crash):
    import time
    config_path, state_path = tmp_path / 'config.json', tmp_path / 'state.json'
    value = {'name': 'frozen test'}
    config_path.write_text(json.dumps(value), encoding='utf-8')
    run = tool.AcceptanceRun(value, tmp_path, state_path, 'http://127.0.0.1:9')
    run.state['jobs']['original-token'] = {'id': 'known-task'}
    run.save()
    before = state_path.read_bytes()
    ready = tmp_path / 'holder-ready'
    code = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from run_motion_api_acceptance import state_lock
with state_lock(Path(sys.argv[2])):
    Path(sys.argv[3]).write_text("held")
    sys.stdin.readline()
'''
    holder = subprocess.Popen([sys.executable, '-c', code, str(MODULE.parent), str(state_path), str(ready)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    command = [sys.executable, str(MODULE), '--config', str(config_path), '--state', str(state_path),
               '--base-url', 'http://127.0.0.1:9']
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), holder.communicate(timeout=2)
        rejected = subprocess.run(command + ['--stage', 'import'], capture_output=True, text=True, timeout=10)
        assert rejected.returncode == 2
        assert 'Another acceptance CLI owns this state' in rejected.stderr
        assert state_path.read_bytes() == before
        if crash:
            holder.kill()
        else:
            holder.stdin.write('release\n')
            holder.stdin.flush()
        holder.communicate(timeout=10)
        # The retained .lock file is harmless after clean or abrupt process exit.
        assert state_path.with_name('state.json.lock').exists()
        accepted = subprocess.run(command + ['--stage', 'plan'], capture_output=True, text=True, timeout=10)
        assert accepted.returncode == 0, accepted.stderr
        assert json.loads(accepted.stdout)['scope'] == 'plan_only_no_HTTP_or_model_calls'
        assert state_path.read_bytes() == before
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.communicate(timeout=10)


def test_state_lock_releases_when_stage_raises(tmp_path):
    state = tmp_path / 'state.json'
    with pytest.raises(RuntimeError, match='stage failed'):
        with tool.state_lock(state):
            raise RuntimeError('stage failed')
    with tool.state_lock(state):
        assert not state.exists()


def test_two_real_cli_processes_cannot_duplicate_a_mutation_during_http_wait(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    entered, release = threading.Event(), threading.Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            entered.set()
            if not release.wait(10):
                return
            data = json.dumps({'id': 'only-pack', 'version': 'v1'}).encode()
            self.send_response(201)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_path, state = tmp_path / 'config.json', tmp_path / 'state.json'
    config_path.write_text('{"name":"same initial import"}')
    command = [sys.executable, str(MODULE), '--config', str(config_path), '--state', str(state),
               '--base-url', 'http://127.0.0.1:' + str(server.server_port), '--stage', 'import']
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert entered.wait(10), 'First CLI did not reach HTTP while holding state lock'
        before = state.read_bytes()
        second = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert second.returncode == 2 and 'Another acceptance CLI owns this state' in second.stderr
        assert calls == ['/api/motion-packs'] and state.read_bytes() == before
        release.set()
        stdout, stderr = first.communicate(timeout=10)
        assert first.returncode == 0, stderr
        assert json.loads(stdout)['packId'] == 'only-pack'
        # A later process reuses the persisted receipt after the first releases.
        repeated = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert repeated.returncode == 0, repeated.stderr
        assert calls == ['/api/motion-packs']
        assert json.loads(state.read_text())['packId'] == 'only-pack'
    finally:
        release.set()
        if first.poll() is None:
            first.kill()
        first.communicate(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
