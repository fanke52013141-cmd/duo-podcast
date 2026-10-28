import asyncio
from copy import deepcopy
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from duocast.services.expert_preview import select_windows,digest
from duocast.services.expert_continuation import freeze_reuse,validate_continuation
from duocast.services.render_inputs import dependency_hash
from duocast.domain.project import Project
from duocast.domain.job import JobStatus
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.expert_versions import ExpertVersionStore
from duocast.core.eventbus import EventBus
from duocast.jobs.manager import JobManager


@pytest.fixture
def prepared(tmp_path):
    project=Project.model_validate({'id':'p','motionPack':{'id':'pack','version':'v1'},'currentDraftRevision':'R',
        'scriptRevisions':[{'id':'R','turns':[{'id':'A','speaker':'A'},{'id':'B','speaker':'B'}]}],
        'audioTimeline':{'revisionId':'R','sampleRate':48000,'sampleCount':96000,'masterAudioAssetId':'master',
            'units':[{'id':r,'turnId':r,'adoptedAudioAssetId':r,'sampleCount':48000} for r in 'AB'],
            'unitOffsets':{'A':0,'B':48000}}})
    turns=[{'id':r,'speaker':r,'startSample':i*48000,'endSample':(i+1)*48000} for i,r in enumerate('AB')]
    full={'sampleRate':48000,'totalSamples':96000,'totalFrames':50,'segments':[
        {'cameraId':'dual','outputStartFrame':i*25,'outputEndFrame':(i+1)*25,'contextStartFrame':i*25,
         'contextEndFrame':(i+1)*25,'trimStartFrame':0,'trimEndFrame':25,'modelFrames':29,
         'seed':i,'dependencyHash':str(i)*64} for i in range(2)]}
    artifacts=ArtifactStore(tmp_path/'artifacts');manager=JobManager(tmp_path/'jobs',EventBus(tmp_path/'events.jsonl'))
    state=SimpleNamespace(artifacts=artifacts,job_manager=manager,expert_version_store=ExpertVersionStore(tmp_path/'versions'))
    full_plan=select_windows(full,turns,operation='continue')
    full_plan.update(projectId='p',snapshotId='snapshot',dependencyHash=dependency_hash(project))
    source=deepcopy(full_plan);source['windows']=source['windows'][:1]
    source['windows'][0]['segment'].update(seed=99,dependencyHash='a'*64)  # Adopted local retry must survive continuation.
    path=tmp_path/'core.mp4';path.write_bytes(b'isolated native core fixture')
    entry=artifacts.register('core','video',str(path),'a'*64)
    artifacts.register('preview','video',str(path),dependency_hash(project))
    artifacts.register('master','audio',str(path),'master-source')
    job=manager.submit('expert.generate','p','gpu','original',{'expertSelection':source,'dependencyHash':dependency_hash(project)})
    job.status=JobStatus.SUCCEEDED;job.result={'simulated':False,'artifactIds':['preview'],'meta':{'windowArtifacts':[
        {'windowId':'EW-00000','artifactId':'core','fileHash':entry['fileHash'],'dependencyHash':'a'*64,'outputStartFrame':0,'outputEndFrame':25}]}}
    state.expert_version_store.select('p',expected_revision=0,snapshot_id='snapshot',job_id=job.id,windows=['EW-00000'])
    return state,project,full_plan,full,path


def test_freeze_reuses_adopted_retry_and_binds_selection_revision(prepared):
    state,project,plan,_,_=prepared
    result=freeze_reuse(state,project,plan)
    assert result['reusedWindowCount']==1 and result['generateWindowCount']==1
    assert result['reuseSelectionRevision']==1
    assert result['windows'][0]['segment']['seed']==99
    assert result['selectedSampleRange']==[0,96000] and result['displayFrameRange']==[0,50]
    validate_continuation(result,plan['windows'])
    invalid=deepcopy(result);invalid['windows'][0]['segment']['outputEndFrame']=26
    with pytest.raises(ValueError):validate_continuation(invalid,plan['windows'])


def test_changed_core_or_snapshot_refuses_reuse(prepared):
    state,project,plan,_,path=prepared
    invalid=deepcopy(plan);invalid['snapshotId']='changed'
    with pytest.raises(HTTPException):freeze_reuse(state,project,invalid)
    path.write_bytes(b'changed')
    with pytest.raises(HTTPException):freeze_reuse(state,project,plan)


def test_worker_only_calls_sampler_for_missing_window(prepared,monkeypatch):
    from duocast.adapters import expert_motion_provider as module
    from duocast.services import fast_runtime
    state,project,plan,full,path=prepared
    selection=freeze_reuse(state,project,plan)
    req={'projectSnapshot':project.model_dump(mode='json',by_alias=True),'revisionId':'R','seed':1,
         'generationProfile':'expertMotionReference','expertSelection':selection,'expertEngineFingerprint':'c'*64}
    job=state.job_manager.submit('expert.generate','p','gpu','continue',req)
    provider=module.ExpertMotionVideoProvider(state.artifacts,None,base_url='http://engine.invalid',
        input_dir=path.parent/'input',fast_runtime_root=path.parent/'runtime',job_manager=state.job_manager,engine_fingerprint='c'*64)
    (provider.runtime/'prepare_expert_motion.py').parent.mkdir(parents=True)
    (provider.runtime/'prepare_expert_motion.py').write_text('# isolated protocol stub')
    monkeypatch.setattr(module,'build_motion_worker_inputs',lambda *_a,**_k:{})
    monkeypatch.setattr(module,'camera_edit_plan',lambda *_a:[])
    monkeypatch.setattr(module,'build_expert_motion_plan',lambda *_a,**_k:deepcopy(full))
    monkeypatch.setattr(provider,'_write_tracks',lambda *_a:(48000,96000,[],{'master':'fixture'}))
    monkeypatch.setattr(provider,'_source_code_identity',lambda:{'fixture':'code'})
    calls=[]
    async def command(*_a,**_k):return b'isolated tool version'
    async def release():pass
    async def prepare(_job,_folder,_key,payload):
        assert len(payload['expertPlan']['segments'])==1
        return {'preparedSegments':[{'segmentDependencyHash':payload['expertPlan']['segments'][0]['dependencyHash']}]}
    async def sample(_client,_job,_key,segment,*_args):
        calls.append(segment['outputStartFrame']);return path,{'coreHash':state.artifacts._hash_file(str(path))}
    async def video(*_args):return {}
    async def finish(_job,_req,_key,_folder,cores,records,*_args):
        assert len(cores)==2 and records[0]['reused'] is True
        return {'samplerCalls':len(calls)}
    for name,fn in [('_command',command),('_prepare_sources',prepare),('_segment',sample),('_video',video),('_finish',finish)]:monkeypatch.setattr(provider,name,fn)
    monkeypatch.setattr(fast_runtime,'release_all',release)
    assert asyncio.run(provider._render(job,req,None))=={'samplerCalls':1}
    assert calls==[25]
