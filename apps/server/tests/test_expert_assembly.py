"""Assemble synthetic registered windows and PCM through the actual export path; no model."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import subprocess
import wave
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from duocast.api.expert_assembly import router
from duocast.core.eventbus import EventBus
from duocast.domain.job import JobStatus
from duocast.jobs.manager import JobManager
from duocast.services.expert_assembly import assembly_plan, ExpertAssemblyService
from duocast.services.render_inputs import dependency_hash
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.expert_versions import ExpertVersionStore
from duocast.storage.project_store import ProjectStore


@pytest.fixture
def prepared(tmp_path):
    artifacts=ArtifactStore(tmp_path/'artifacts')
    manager=JobManager(tmp_path/'jobs',EventBus(tmp_path/'events.jsonl'))
    store=ProjectStore(tmp_path/'projects')
    project=store.create('p')
    master=tmp_path/'master.wav'
    with wave.open(str(master),'wb') as audio:
        audio.setparams((1,2,48000,0,'NONE','not compressed'));audio.writeframes(b'\0\0'*96000)
    artifacts.register('master','audio',str(master),'audio-source')
    project=store.apply('p',{'current_draft_revision':'R1','audio_timeline':{
        'revisionId':'R1','sampleRate':48000,'sampleCount':96000,'masterAudioAssetId':'master'}},project.revision)
    state=SimpleNamespace(project_store=store,artifacts=artifacts,job_manager=manager,
        expert_version_store=ExpertVersionStore(tmp_path/'versions'))
    identity=dependency_hash(project)
    windows=[]
    for i,color in enumerate(('blue','red')):
        path=tmp_path/f'core{i}.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'color=c={color}:s=128x72:r=25:d=1',
            '-an','-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True,timeout=15)
        entry=artifacts.register(f'core{i}','video',str(path),f'dependency{i}')
        windows.append({'windowId':f'EW-{i:05d}','index':i,'artifactId':f'core{i}',
            'fileHash':entry['fileHash'],'dependencyHash':f'dependency{i}',
            'outputStartFrame':i*25,'outputEndFrame':(i+1)*25})
    plan={'snapshotId':'snapshot','sampleRate':48000,'totalSamples':96000,'totalFrames':50,'fullWindowCount':2,
        'windows':[{'windowId':w['windowId'],'segment':{k:w[k] for k in ('outputStartFrame','outputEndFrame','dependencyHash')}} for w in windows]}
    job=manager.submit(kind='expert.generate',project_id='p',queue_class='gpu',client_token='fixture-candidate',
        input_snapshot={'expertSelection':plan,'dependencyHash':identity})
    artifacts.register('preview','video',str(tmp_path/'core0.mp4'),identity)
    job.status=JobStatus.SUCCEEDED
    job.result={'artifactIds':['preview'],'simulated':False,'meta':{'windowArtifacts':windows}}
    state.expert_version_store.select('p',expected_revision=0,snapshot_id='snapshot',job_id=job.id,
        windows=[w['windowId'] for w in windows])
    return state,job,windows


def test_real_assembly_and_explicit_adoption_updates_normal_output(prepared):
    state,_,_=prepared
    app=FastAPI();app.include_router(router)
    for key,value in vars(state).items():setattr(app.state,key,value)
    with TestClient(app) as client:
        plan=client.get('/api/projects/p/expert-video/assembly-plan').json()
        assert plan['ready']
        request={'assemblyId':plan['assemblyId'],'clientToken':'assembly-request'}
        response=client.post('/api/projects/p/expert-video/assemble',json=request)
        assert response.status_code==200,response.text
        job_id=response.json()['jobId']
        assert client.post('/api/projects/p/expert-video/assemble',json=request).json()['jobId']==job_id
        job=state.job_manager.get(job_id)
        result=asyncio.run(ExpertAssemblyService(state).run(job))
        assert asyncio.run(ExpertAssemblyService(state).run(job))==result
        assert result['meta']['durationSec']==2 and result['meta']['modelCallsSubmitted']==0
        assert state.project_store.load('p').output_manifest is None
        job.status=JobStatus.SUCCEEDED;job.result=result
        url=f'/api/projects/p/expert-video/assemblies/{job_id}/adopt'
        revision=state.project_store.load('p').revision
        assert client.post(url,json={'expectedProjectRevision':revision,'reviewed':False}).status_code==422
        adopted=client.post(url,json={'expectedProjectRevision':revision,'reviewed':True})
        assert adopted.status_code==200,adopted.text
        assert state.project_store.load('p').output_manifest['artifactIds']==result['artifactIds']
        assert client.post(url,json={'expectedProjectRevision':revision,'reviewed':True}).json()==adopted.json()
        assert len(state.project_store.load('p').output_history)==1


def test_partial_coverage_and_changed_core_are_rejected(prepared):
    state,job,windows=prepared
    state.expert_version_store.select('p',expected_revision=1,snapshot_id='different-snapshot',job_id=job.id,windows=['EW-00000'])
    # Reset ownership to the real snapshot with only one adopted window.
    state.expert_version_store.select('p',expected_revision=2,snapshot_id='snapshot',job_id=job.id,windows=['EW-00000'])
    plan=assembly_plan(state,'p')
    assert not plan['ready'] and plan['missingWindowIds']==['EW-00001']
    from pathlib import Path
    Path(state.artifacts.by_id(windows[0]['artifactId'])['path']).write_bytes(b'changed')
    with pytest.raises(HTTPException):assembly_plan(state,'p')
