"""Offline workflow acceptance: readiness, configuration, recovery and safe diagnostics."""
import io
import json
import zipfile
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from duocast.main import app, settings
from duocast.domain.project import Project, Approval, ScriptRevision, Turn, Line
from duocast.domain.audio import AudioTimeline, SynthesisUnit
from duocast.domain.visual import VisualVariant
from duocast.domain.job import Job, JobStatus
from duocast.services.render_inputs import dependency_hash, visual_dependency_hash


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(settings, storage_root=tmp_path, text_provider='mock', image_provider='mock', tts_provider='mock', video_provider='mock'))
    with TestClient(app) as c:
        yield c


def test_config_preflight_does_not_persist_or_contact_a_provider(client, monkeypatch):
    monkeypatch.delenv('DUOCAST_TEST_ABSENT', raising=False)
    before = client.get('/api/machine').json()['runtimeConfig']
    invalid = {'text_provider':'toapis','text_base_url':'https://example.com/v1','credential_env':'DUOCAST_TEST_ABSENT'}
    result = client.post('/api/machine/validate-config', json={'runtimeConfig':invalid}).json()
    assert not result['valid'] and len(result['errors']) == 2
    valid = {**invalid,'text_base_url':'https://example.com','text_model':'test-model'}
    result = client.post('/api/machine/validate-config', json={'runtimeConfig':valid}).json()
    assert result['valid'] and result['warnings']
    assert client.get('/api/machine').json()['runtimeConfig'] == before
    assert client.post('/api/machine/validate-config', json={'runtimeConfig':{'credential_env':'1INVALID'}}).json()['valid'] is False
    assert client.post('/api/machine/validate-config', json={'runtimeConfig':{'video_provider':'comfyui','video_url':'http://localhost:8191'}}).json()['warnings']
    assert client.post('/api/machine/validate-config', json={}).json()['valid'] is False


def test_diagnostics_package_is_allowlisted_even_with_secret_bearing_errors(client, monkeypatch):
    secret = 'test-super-secret-token'
    monkeypatch.setenv('DUOCAST_TOAPIS_KEY', secret)
    job = Job(id='sensitive', kind='renders', status=JobStatus.FAILED, project_id='private-project',
              error=f'401 {secret} https://user:password@example.test/secret?token={secret} C:/private/file',
              input_snapshot={'script':'confidential screenplay','api_key':secret})
    app.state.job_manager._jobs[job.id] = job
    response = client.get('/api/diagnostics/package')
    assert response.status_code == 200
    for sensitive in (secret,'password','private-project','confidential screenplay','C:/private','example.test'):
        assert sensitive not in response.text
    report = response.json()
    assert report['recentFailures'] == [{'kind':'renders','category':'credentials'}]
    assert report['jobCounts']['failed'] == 1
    assert report['applicationVersion']
    assert 'attachment' in response.headers['content-disposition']


def test_import_summary_reports_present_missing_and_external_dependencies(client, tmp_path):
    p = client.post('/api/projects', json={'title':'original'}).json()
    assets, store = app.state.artifacts, app.state.project_store
    file = tmp_path/'ref.wav'; file.write_bytes(b'audio-reference')
    missing = tmp_path/'missing.wav'; missing.write_bytes(b'missing')
    for key, path in (('ref',file),('missing-ref',missing)):
        assets.register(key,'audio',str(path),'')
    current = store.load(p['id'])
    store.apply(p['id'], {'creation_draft':{'reference':'ref','missingReference':'missing-ref'}},current.revision)
    missing.unlink()
    content = client.get(f"/api/projects/{p['id']}/backup").content
    imported = client.post('/api/projects/import-backup', files={'file':('backup.zip',content)}).json()
    assert imported['projectId'] != p['id']
    assert imported['summary']['sourceProjectId'] == p['id']
    assert imported['summary']['mediaCount'] == 1
    assert imported['summary']['mediaBytes'] == len(b'audio-reference')
    assert imported['missing'] == ['missing-ref']
    assert imported['summary']['external'] and imported['summary']['recheck']
    assert store.load(p['id']).creation_draft['reference'] == 'ref'
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        meta=json.loads(archive.read('duocast-backup.json'))
    meta['artifacts'][0]['fileHash']='tampered'
    damaged=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(damaged,'w') as target:
        for name in source.namelist():
            target.writestr(name,json.dumps(meta) if name=='duocast-backup.json' else source.read(name))
    before=store.list_ids()
    assert client.post('/api/projects/import-backup',files={'file':('damaged.zip',damaged.getvalue())}).status_code == 422
    assert store.list_ids() == before


def test_delivery_empty_project_lists_actionable_issues(client):
    p=client.post('/api/projects',json={}).json()
    result=client.get(f"/api/projects/{p['id']}/plan/delivery").json()
    assert not result['ready']
    assert {'no_script','stale_audio','no_sample','no_output'} <= {i['code'] for i in result['issues']}
    assert all(i['target'] in {'script','voice','visual','render'} for i in result['issues'])
    assert client.get('/api/projects/absent/plan/delivery').status_code == 404


def test_delivery_ready_then_detects_spec_drift_and_media_damage(client,tmp_path,monkeypatch):
    from duocast.services.delivery_check import delivery_check
    assets=app.state.artifacts
    # Focus this test on delivery references; audio fingerprint matching has dedicated suites.
    monkeypatch.setattr('duocast.services.render_inputs.audio_inputs_match',lambda *args:True)
    for key,kind in (('master','audio'),('unit','audio'),('visual','image')):
        path=tmp_path/(key+'.bin');path.write_bytes(key.encode());assets.register(key,kind,str(path),'')
    p=Project(id='delivery',current_draft_revision='R1',script_revisions=[ScriptRevision(id='R1',turns=[Turn(id='T1',speaker='A',lines=[Line(id='L1',display_text='hello')])])],
              audio_timeline=AudioTimeline(revision_id='R1',sample_count=48000,master_audio_asset_id='master',units=[SynthesisUnit(id='U1',turn_id='T1',adopted_audio_asset_id='unit',sample_count=48000)]),
              visual_variants=[VisualVariant(id='V1',master_image={'artifactId':'visual'})],selected_visual_variant_id='V1',render_spec={'fps':25},
              output_manifest={'version':'OUT-R1-v1','artifactIds':['video']})
    p.approvals=[Approval(kind=k,input_revision_id='R1') for k in ('script','voice')]
    p.approvals.append(Approval(kind='sample',input_revision_id='R1',spec={'dependencyHash':visual_dependency_hash(p),'renderSpec':{'fps':25}}))
    path=tmp_path/'video.bin'
    path.write_bytes(b'video')
    assets.register('video','video',str(path),'',{'dependencyHash':dependency_hash(p)})
    assert delivery_check(p,assets)['ready']
    p.render_spec={'fps':30}
    assert {'sample_spec','stale_output'} <= {i['code'] for i in delivery_check(p,assets)['issues']}
    path.write_bytes(b'corrupted')
    assert any(i['code']=='corrupt_成片' for i in delivery_check(p,assets)['issues'])
    (tmp_path/'master.bin').unlink()
    assert any(i['code']=='missing_母轨' for i in delivery_check(p,assets)['issues'])


def test_stale_edit_cannot_overwrite_newer_script(client):
    p=client.post('/api/projects',json={}).json()
    base=f"/api/projects/{p['id']}"
    script={'id':'R1','turns':[{'id':'T1','speaker':'A','lines':[{'id':'L1','displayText':'new server content'}]}]}
    saved=client.patch(base,json={'expectedRevision':p['revision'],'scriptRevisions':[script],'currentDraftRevision':'R1'}).json()
    response=client.patch(base,json={'expectedRevision':p['revision'],'scriptRevisions':[]})
    assert response.status_code == 409
    assert client.get(base).json()['scriptRevisions'] == saved['scriptRevisions']
