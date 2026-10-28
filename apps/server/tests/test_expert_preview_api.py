from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from duocast.api import expert_previews as api
from duocast.domain.project import Project


@pytest.fixture
def client(monkeypatch, tmp_path):
    project = Project.model_validate({'id':'p','motionPack':{'id':'pack','version':'v1'},
        'currentDraftRevision':'R1','scriptRevisions':[{'id':'R1','turns':[
            {'id':f'T{i}','speaker':'AB'[i%2]} for i in range(4)]}],
        'audioTimeline':{'revisionId':'R1','sampleRate':48000,'sampleCount':192000,
            'unitOffsets':{f'U{i}':i*48000 for i in range(4)},'units':[
                {'id':f'U{i}','turnId':f'T{i}','sampleCount':48000,'adoptedAudioAssetId':f'A{i}'} for i in range(4)]}})
    app=FastAPI();app.include_router(api.router)
    app.state.project_store=SimpleNamespace(load=lambda id:project if id=='p' else None)
    app.state.motion_pack_store=object();app.state.artifacts=object()
    from duocast.storage.expert_plans import ExpertPlanStore
    app.state.expert_plan_store = ExpertPlanStore(tmp_path / 'plans')
    from duocast.storage.expert_versions import ExpertVersionStore
    app.state.expert_version_store=ExpertVersionStore(tmp_path/'versions')
    monkeypatch.setattr(api,'validate_project_motion_binding',lambda *_:None)
    monkeypatch.setattr(api,'build_motion_worker_inputs',lambda *_a,**_k:{})
    monkeypatch.setattr(api,'camera_edit_plan',lambda *_:[])
    full={'sampleRate':48000,'totalSamples':192000,'totalFrames':100,'segments':[
        {'outputStartFrame':i*25,'outputEndFrame':(i+1)*25,'contextStartFrame':max(0,i*25-8),
         'contextEndFrame':min(100,(i+1)*25+8),'trimStartFrame':8,'trimEndFrame':33,
         'modelFrames':41,'seed':i,'dependencyHash':str(i)*64} for i in range(4)]}
    monkeypatch.setattr(api,'build_expert_motion_plan',lambda *_a,**_k:full)
    with TestClient(app) as test:yield test,project


def test_preview_plan_is_server_derived_without_fake_execution(client):
    test,_=client
    response=test.post('/api/projects/p/expert-video/preview-plan',json={})
    assert response.status_code==200,response.text
    data=response.json()
    assert data['selectedTurnIds']==['T0','T1','T2','T3']
    assert data['modelCallsSubmitted']==0 and not data['executionAvailable']
    assert len(data['dependencyHash'])==64 and data['snapshotId'].startswith('ES-')


def test_unknown_runtime_paths_and_stale_project_rejected(client):
    test,_=client
    assert test.post('/api/projects/p/expert-video/preview-plan',json={'runtimePath':'C:/other'}).status_code==422
    assert test.post('/api/projects/p/expert-video/preview-plan',json={'expectedDependencyHash':'0'*64}).status_code==409
    assert test.post('/api/projects/missing/expert-video/preview-plan',json={}).status_code==404


def test_regeneration_requires_explicit_range_and_candidate(client):
    test,_=client
    url='/api/projects/p/expert-video/regeneration-plan'
    assert test.post(url,json={}).status_code==422
    response=test.post(url,json={'turnIds':['T1','T2'],'candidateToken':'candidate-123'})
    assert response.status_code==200,response.text
    assert response.json()['operation']=='regenerate'
    assert len(response.json()['windows'])==4


def test_changed_voice_revision_cannot_preview(client):
    test,project=client
    project.audio_timeline.revision_id='old'
    assert test.post('/api/projects/p/expert-video/preview-plan',json={}).status_code==422


def test_continuation_plans_complete_episode_without_generation(client):
    test,_=client
    url='/api/projects/p/expert-video/continuation-plan'
    response=test.post(url,json={})
    assert response.status_code==200,response.text
    plan=response.json()
    assert plan['operation']=='continue' and plan['generateWindowCount']==4
    assert plan['reusedWindowCount']==0 and plan['selectedSampleRange']==[0,192000]
    assert plan['modelCallsSubmitted']==0
    assert test.post(url,json={'turnIds':['T0']}).status_code==422


def test_plan_history_survives_requests_and_marks_changed_inputs_stale(client):
    test, project = client
    base = '/api/projects/p/expert-video'
    saved = test.post(base+'/preview-plan', json={}).json()
    test.post(base+'/preview-plan', json={})
    plans = test.get(base+'/plans').json()
    assert len(plans) == 1 and plans[0]['planId'] == saved['planId']
    assert plans[0]['stale'] is False
    project.audio_timeline.sample_count += 48000
    assert test.get(base+'/plans').json()[0]['stale'] is True
    assert test.get('/api/projects/missing/expert-video/plans').status_code == 404
