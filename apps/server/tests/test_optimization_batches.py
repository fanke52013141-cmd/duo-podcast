"""Regression tests for input protection, cancellation, configuration and recovery."""
import asyncio
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from duocast.main import app, settings, _job_runner
from duocast.core.eventbus import EventBus
from duocast.domain.project import Line, ScriptRevision, Turn
from duocast.jobs.manager import JobManager
from duocast.storage.project_store import ProjectStore
from duocast.storage.artifacts import ArtifactStore
from duocast.adapters.mock import MockTextProvider, MockImageProvider, MockVideoProvider


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(settings, storage_root=tmp_path, text_provider='mock', image_provider='mock', tts_provider='mock', video_provider='mock'))
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize('action,terminal', [('cancel', 'cancelled'), ('pause', 'paused')])
def test_running_control_stops_next_unit_and_prevents_adoption(tmp_path, action, terminal):
    async def exercise():
        store = ProjectStore(tmp_path/'projects', debounce_ms=0)
        p = store.create('control')
        revision = ScriptRevision(id='R1', turns=[Turn(id=f'T{i}', speaker=s, lines=[Line(id=f'L{i}', display_text='你好', spoken_text='你好')]) for i, s in enumerate(('A', 'B'))])
        p = store.apply(p.id, {'script_revisions':[revision], 'current_draft_revision':'R1'}, p.revision)
        assets = ArtifactStore(tmp_path/'artifacts')
        class Blocking:
            capabilities = {'simulated': True}
            def __init__(self):
                self.started = asyncio.Event(); self.release = asyncio.Event(); self.calls = 0
            async def synthesize(self, request):
                self.calls += 1; self.started.set(); await self.release.wait()
                return {'sampleRate':48000, 'sampleCount':48000}
        tts = Blocking()
        manager = JobManager(tmp_path/'jobs', EventBus(tmp_path/'events.jsonl'), {'gpu':1, 'api':1, 'cpu':1})
        manager.set_runner(_job_runner(manager, store, assets, MockTextProvider(), MockImageProvider(), tts, MockVideoProvider()))
        manager.start()
        try:
            job = manager.submit('tts.synthesize', p.id, 'gpu', 'control', {'projectSnapshot':p.model_dump(by_alias=True), 'revisionId':'R1', 'voiceBindings':{}})
            await asyncio.wait_for(tts.started.wait(), 2)
            getattr(manager, action)(job.id); tts.release.set()
            for _ in range(100):
                if job.status.value == terminal: break
                await asyncio.sleep(.01)
            assert job.status.value == terminal
            assert tts.calls == 1
            assert store.load(p.id).audio_timeline is None
            assert store.load(p.id).audio_history == []
            if action == 'pause':
                manager.resume(job.id)
                for _ in range(100):
                    if job.status.value == 'succeeded': break
                    await asyncio.sleep(.01)
                assert job.status.value == 'succeeded'
                assert tts.calls == 2, 'completed first unit must not be inferred twice after resume'
                assert len(store.load(p.id).audio_timeline.units) == 2
        finally:
            await manager.stop()
    asyncio.run(exercise())


def test_creation_draft_is_durable_and_restored(client):
    p = client.post('/api/projects', json={'title':'输入保护'}).json()
    draft = {'sourceKind':'article', 'content':'长文章'*500, 'durationMode':'approx', 'approxMinutes':3, 'title':'新的节目名'}
    r = client.patch(f"/api/projects/{p['id']}", json={'expectedRevision':p['revision'], 'creationDraft':draft, 'title':draft['title']})
    assert r.status_code == 200, r.text
    store = app.state.project_store
    fresh = ProjectStore(store.root)
    assert fresh.load(p['id']).creation_draft == draft
    assert client.get(f"/api/projects/{p['id']}").json()['title'] == draft['title']


def test_runtime_config_rejects_secrets_and_keeps_budget(client):
    assert client.patch('/api/machine', json={'runtimeConfig':{'api_key':'secret'}}).status_code == 422
    assert client.patch('/api/machine', json={'runtimeConfig':{'text_base_url':'https://user:secret@example.com'}}).status_code == 422
    config = {'text_provider':'mock', 'credential_env':'DUOCAST_TOAPIS_KEY', 'budget_yuan':3.5}
    assert client.patch('/api/machine', json={'runtimeConfig':config}).status_code == 200
    assert client.get('/api/machine').json()['runtimeConfig'] == config
    result = client.post('/api/providers/text/test').json()
    assert result['simulated'] and '模拟' in result['message']


def test_backup_relocates_media_and_never_overwrites(client, tmp_path):
    p = client.post('/api/projects', json={'title':'可迁移工程'}).json()
    assets = app.state.artifacts
    file = tmp_path/'original.wav'; file.write_bytes(b'fixture-audio')
    entry = assets.register('AUDIO-backup', 'audio', str(file), 'dep')
    store = app.state.project_store
    current = store.load(p['id'])
    store.apply(p['id'], {'voice_bindings':[{'id':'VB-A','characterId':'A','providerProfileId':'local', 'localRefAudio':{'artifactId':entry['artifactId'],'path':str(file),'fileHash':entry['fileHash']}}]}, current.revision)
    package = client.get(f"/api/projects/{p['id']}/backup")
    assert package.status_code == 200
    response = client.post('/api/projects/import-backup', files={'file':('backup.zip', package.content, 'application/zip')})
    assert response.status_code == 200, response.text
    restored = store.load(response.json()['projectId'])
    ref = restored.voice_bindings[0].local_ref_audio
    assert restored.id != p['id']
    assert ref.artifact_id != entry['artifactId']
    assert Path(ref.path).read_bytes() == b'fixture-audio'
    assert client.get(f"/api/artifacts/{ref.artifact_id}/file").content == b'fixture-audio'


def test_backup_tampering_rejected_before_project_creation(client):
    p = client.post('/api/projects', json={'title':'原项目'}).json()
    content = client.get(f"/api/projects/{p['id']}/backup").content
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        data = json.loads(archive.read('duocast-backup.json'))
    data['artifacts'] = [{'artifactId':'bad','kind':'image','path':'arbitrary','member':'../escape','fileHash':'wrong'}]
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('duocast-backup.json', json.dumps(data)); archive.writestr('../escape', b'bad')
    before = client.get('/api/projects').json()
    assert client.post('/api/projects/import-backup', files={'file':('bad.zip', output.getvalue())}).status_code == 422
    assert client.get('/api/projects').json() == before


def test_soft_delete_restore_and_cache_is_recoverable(client):
    p = client.post('/api/projects', json={'title':'恢复测试'}).json()
    assert client.delete(f"/api/projects/{p['id']}").status_code == 200
    records = client.get('/api/maintenance').json()['trash']
    record = next(t for t in records if t.get('projectId') == p['id'])
    assert client.post('/api/maintenance/restore/'+record['token']).status_code == 200
    assert client.get(f"/api/projects/{p['id']}").json()['title'] == '恢复测试'
    root = app.state.artifacts.root.parent
    cache = root/'cache'; cache.mkdir(exist_ok=True); (cache/'cache.json').write_text('{"entries":{}}')
    moved = client.post('/api/maintenance/cache-trash').json()
    assert (cache/'cache.json').exists() is False
    assert client.post('/api/maintenance/restore/'+moved['token']).status_code == 200
    assert (cache/'cache.json').is_file()


def test_region_calibration_rejects_outside_canvas(client):
    p = client.post('/api/projects', json={'title':'区域'}).json()
    response = client.post(f"/api/projects/{p['id']}/visual/regions", json={'variantId':'V1','regions':{'A':{'x':0,'y':0,'width':2,'height':1},'B':{'x':.5,'y':0,'width':.5,'height':1}}})
    assert response.status_code == 422


def test_authored_script_preserves_punctuation_and_same_speaker_lines():
    from duocast.services.script_svc import parse_authored_script
    source = 'A: 时间是 12：30。\nA：ＡＢ 是原稿文本。\nB: 收到！'
    parsed = parse_authored_script(source)
    assert parsed['sourceInput']['content'] == source
    assert [t['speaker'] for t in parsed['turns']] == ['A', 'A', 'B']
    assert parsed['turns'][0]['lines'][0]['displayText'] == '时间是 12：30。'
    assert parsed['turns'][1]['lines'][0]['displayText'] == 'ＡＢ 是原稿文本。'


def test_character_binding_uses_snapshot_and_regions_are_persisted(client, tmp_path):
    from duocast.domain.visual import VisualVariant
    p = client.post('/api/projects', json={'title':'绑定人物'}).json()
    c = client.post('/api/characters', json={'name':'新主持','traits':'沉稳'}).json()
    response = client.post(f"/api/projects/{p['id']}/characters/A", json={'characterId':c['id'], 'expectedRevision':p['revision']})
    assert response.status_code == 200
    client.patch('/api/characters/'+c['id'], json={'name':'全局新名字'})
    assert client.get(f"/api/projects/{p['id']}").json()['characterBindings']['A']['name'] == '新主持'
    store = app.state.project_store
    current = store.load(p['id'])
    current = store.apply(p['id'], {'visual_variants':[VisualVariant(id='V1')]}, current.revision)
    regions = {'A':{'x':.1,'y':.1,'width':.3,'height':.8}, 'B':{'x':.6,'y':.1,'width':.3,'height':.8}}
    response = client.post(f"/api/projects/{p['id']}/visual/regions", json={'variantId':'V1','regions':regions,'expectedRevision':current.revision})
    assert response.status_code == 200
    assert ProjectStore(store.root).load(p['id']).visual_variants[0].person_regions == regions


def test_shared_render_spec_and_representative_range(client):
    import time
    p = client.post('/api/projects', json={'title':'共用规格'}).json()
    base = f"/api/projects/{p['id']}"
    script = client.post(base+'/script/generate', json={'sourceInput':{'kind':'script','content':'A: 你好\nB: 你好\nA: 再见'}}).json()
    def wait(job_id):
        for _ in range(200):
            job = client.get('/api/jobs/'+job_id).json()
            if job['status'] == 'succeeded': return job
            assert job['status'] not in {'failed','cancelled'}, job
            time.sleep(.02)
        raise AssertionError('timeout')
    wait(script['jobId'])
    voice = client.post(base+'/voice/synthesize', json={}).json()
    wait(voice['jobId'])
    current = client.get(base).json()
    assert client.patch(base, json={'expectedRevision':current['revision'],'renderSpec':{'resolution':'1280x720','fps':30,'fastQuality':'clear'}}).status_code == 200
    plan = client.get(base+'/visual/sample-plan').json()
    assert plan['end'] > plan['start']
    sample = client.post(base+'/visual/sample', json={}).json()
    job = wait(sample['jobId'])
    assert job['inputSnapshot']['resolution'] == '1280x720'
    assert job['inputSnapshot']['fps'] == 30
    assert job['inputSnapshot']['fastQuality'] == 'clear'
    render = client.post(base+'/render/generate', json={}).json()
    job = wait(render['jobId'])
    assert job['inputSnapshot']['resolution'] == '1280x720'
    assert client.get(base+'/plan').json()['estimateSeconds'] is None


def test_fast_template_pin_detects_changed_worker(tmp_path, monkeypatch):
    from test_fast_mode import fake_install
    from duocast.adapters.musetalk_video import template_identity, project_engine
    from duocast.domain.project import Project
    runtime = tmp_path/'runtime'; fake_install(runtime)
    project = Project(id='pinned', fast_template=template_identity(runtime))
    assert project_engine(project) == runtime
    (runtime/'worker.py').write_bytes(b'changed')
    with pytest.raises(ValueError, match='改变'):
        project_engine(project)


def test_provider_check_probes_models_and_never_returns_credential(client, monkeypatch):
    import httpx
    secret = 'test-only-credential-never-echo'
    app.state.runtime_settings = replace(app.state.runtime_settings, toapis_key=secret)
    app.state.capabilities.text = {'simulated':False}
    seen = []
    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, headers=None):
            seen.append(url)
            return httpx.Response(401, request=httpx.Request('GET',url), json={'error':'unauthorized'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: FakeClient())
    response = client.post('/api/providers/text/test')
    assert response.json()['ok'] is False
    assert secret not in response.text
    assert seen == ['https://toapis.com/v1/models']


def test_requested_live_missing_credentials_is_not_a_successful_demo_check(client):
    app.state.public_config['text_provider'] = 'toapis'
    response = client.post('/api/providers/text/test').json()
    assert response['ok'] is False
    assert '回落演示' in response['message']


def test_scene_library_can_be_shared_without_overwriting_prior_project(client):
    from test_round4_optimizations import PNG_BYTES
    uploaded = client.post('/api/scenes', files={'file':('双人录音棚.png', PNG_BYTES, 'image/png')})
    assert uploaded.status_code == 200
    scene = uploaded.json()
    assert any(s['id'] == scene['id'] for s in client.get('/api/scenes').json())
    for title in ('第一期','第二期'):
        p = client.post('/api/projects', json={'title':title}).json()
        used = client.post(f"/api/projects/{p['id']}/visual/use-scene", json={'sceneId':scene['id'],'expectedRevision':p['revision']})
        assert used.status_code == 200, used.text
        project = client.get(f"/api/projects/{p['id']}").json()
        assert project['selectedVisualVariantId'] == used.json()['variantId']
        assert project['visualVariants'][0]['masterImage']['artifactId'] == scene['id']
