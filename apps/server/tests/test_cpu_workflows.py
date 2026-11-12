"""No model imports/inference: real PCM, storage relocation and mocked GET-only engines."""
import asyncio
import copy
import importlib.util
import io
import json
from pathlib import Path
import wave
import zipfile

import httpx
import pytest

from duocast.domain.job import Job, JobStatus
from duocast.domain.project import Project
from duocast.domain.motion_pack import content_hash
from duocast.services.backup import make_backup, import_backup
from duocast.services.external_status import inspect_external, external_receipts
from duocast.services.motion_impact import motion_impact
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.motion_packs import MotionPackStore
from duocast.storage.project_store import ProjectStore
from test_motion_pack_store import setup_pack, adopt, draft_of
from test_offline_workflow import client
from duocast.main import app


def test_backup_reports_references_absent_from_manifest(tmp_path):
    assets=ArtifactStore(tmp_path/'artifacts')
    project=Project.model_validate({'id':'missing','audioTimeline':{'revisionId':'R1','masterAudioAssetId':'missing-audio','sampleRate':48000,'sampleCount':1,'units':[]}})
    archive=make_backup(project,assets)
    with zipfile.ZipFile(archive) as source:
        meta=json.loads(source.read('duocast-backup.json'))
    assert meta['missing']==['missing-audio']


def test_backup_rekeys_candidate_sample_counts(tmp_path):
    assets=ArtifactStore(tmp_path/'source/artifacts')
    audio=tmp_path/'tone.wav';audio.write_bytes(b'candidate-bytes')
    assets.register('candidate','audio',str(audio),'')
    project=Project.model_validate({'id':'source','audioTimeline':{'revisionId':'R1','sampleRate':48000,'sampleCount':48000,'units':[{'id':'U1','turnId':'T1','sampleCount':48000,'candidateAudioAssetIds':['candidate'],'candidateSampleCounts':{'candidate':48000}}]}})
    target=ArtifactStore(tmp_path/'target/artifacts');store=ProjectStore(tmp_path/'target/projects')
    with make_backup(project,assets).open('rb') as source:
        report=import_backup(source,store,target)
    unit=store.load(report['projectId']).audio_timeline.units[0]
    new_id=unit.candidate_audio_asset_ids[0]
    assert new_id!='candidate' and unit.candidate_sample_counts=={new_id:48000}


def preparation_job():
    workflow={'1':{'class_type':'ExplicitTestFixture','inputs':{}}}
    return Job(id='known',kind='motion.prepare',owner_type='motionPack',owner_id='pack',
        status=JobStatus.UNKNOWN,provider_task_id='original-id',
        input_snapshot={'workflow':workflow,'resumableMotionPreparation':True},
        completed_calls={'motionPreparation':{'promptId':'original-id','workflowHash':content_hash(workflow)}})


@pytest.mark.parametrize('state',['completed','failed','running','queued','unknown'])
def test_external_recovery_queries_only_original_id_and_never_posts(state):
    job=preparation_job();before=job.model_dump();calls=[]
    def engine(request):
        calls.append((request.method,request.url.path))
        if request.url.path.startswith('/history/'):
            data={'original-id':{'status':{'completed':state=='completed','status_str':'error' if state=='failed' else 'success'}}} if state in {'completed','failed'} else {}
        else:
            data={'queue_running':[[0,'original-id']] if state=='running' else [],'queue_pending':[[0,'original-id']] if state=='queued' else []}
        return httpx.Response(200,json=data)
    result=asyncio.run(inspect_external(job,'http://test-engine',transport=httpx.MockTransport(engine)))
    assert result['receipts'][0]['status']==state
    assert all(method=='GET' for method,_ in calls)
    assert calls[0][1]=='/history/original-id'
    assert job.model_dump()==before


def test_external_missing_or_changed_receipts_never_contact_engine():
    job=preparation_job();job.input_snapshot['workflow']['1']['inputs']['changed']=True
    def forbidden(_):
        raise AssertionError('Must not contact engine')
    report=asyncio.run(inspect_external(job,'http://test',transport=httpx.MockTransport(forbidden)))
    assert not report['canInspect'] and report['receipts']==[]


@pytest.mark.parametrize('record',[None,[],{'segments':[]}])
def test_malformed_external_receipts_are_not_recoverable(record):
    job=preparation_job();job.completed_calls['motionPreparation']=record
    assert external_receipts(job)==[]
    job=Job(id='expert',kind='renders',project_id='episode',input_snapshot={'projectSnapshot':{'id':'episode','motionPack':{'id':'pack','version':'v1'}}})
    job.completed_calls['expertMotion']=record
    if isinstance(record,dict):
        record['snapshotHash']=content_hash(job.input_snapshot)
    assert external_receipts(job)==[]


def test_expert_receipts_require_frozen_snapshot_and_engine_errors_stay_unknown():
    job=Job(id='expert',kind='renders',project_id='episode',input_snapshot={'projectSnapshot':{'id':'episode','motionPack':{'id':'pack','version':'v1'}}})
    job.completed_calls={'expertMotion':{'snapshotHash':content_hash(job.input_snapshot),'segments':{'s1':{'promptId':'known'}}}}
    assert external_receipts(job)==[('s1','known')]
    report=asyncio.run(inspect_external(job,'http://test',transport=httpx.MockTransport(lambda _:httpx.Response(503))))
    assert report['receipts'][0]['status']=='unavailable'
    job.input_snapshot['changed']=True
    assert external_receipts(job)==[]


def test_motion_pack_backup_relocates_to_new_machine_and_resets_adoption(setup_pack,tmp_path):
    packs,assets,pack,_=setup_pack
    pack=adopt(packs,pack,'dual')
    project=Project(id='original',title='Portable',motion_pack={'id':pack.id,'version':pack.version})
    archive=make_backup(project,assets,packs)
    target_assets=ArtifactStore(tmp_path/'second-machine/artifacts')
    target_projects=ProjectStore(tmp_path/'second-machine/projects')
    target_packs=MotionPackStore(tmp_path/'second-machine/packs',target_assets.by_id)
    with archive.open('rb') as source:
        report=import_backup(source,target_projects,target_assets,target_packs)
    restored=target_projects.load(report['projectId'])
    assert restored.id!='original' and restored.motion_pack.id!=pack.id
    migrated=target_packs.load_version(restored.motion_pack.id,restored.motion_pack.version)
    assert migrated.checks=={} and migrated.reviews=={}
    assert report['summary']['motionPack']['requiresReview']
    assert report['summary']['motionPack']['imported']
    assert len(migrated.clips)==len(pack.clips)
    for clip in migrated.clips:
        entry=target_assets.by_id(clip.media.artifact_id)
        assert Path(entry['path']).is_relative_to(target_assets.root)
        assert target_assets._hash_file(entry['path'])==clip.media.file_hash
    assert packs.load_version(pack.id,pack.version).reviews


def test_backup_rejects_duplicate_media_ids_before_creating_project(setup_pack,tmp_path):
    packs,assets,pack,_=setup_pack
    archive=make_backup(Project(id='source',motion_pack={'id':pack.id,'version':pack.version}),assets,packs)
    with zipfile.ZipFile(archive) as source:
        meta=json.loads(source.read('duocast-backup.json'))
        meta['artifacts'].append(copy.deepcopy(meta['artifacts'][0]))
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as target:
            for name in source.namelist():
                target.writestr(name,json.dumps(meta) if name=='duocast-backup.json' else source.read(name))
    projects=ProjectStore(tmp_path/'destination')
    with pytest.raises(Exception) as error:
        import_backup(io.BytesIO(buffer.getvalue()),projects,assets,packs)
    assert getattr(error.value,'status_code',None)==422
    assert projects.list_ids()==[]


@pytest.fixture
def pcm_project(setup_pack,tmp_path):
    packs,assets,pack,_=setup_pack
    draft=draft_of(pack)
    for clip in draft['clips']:
        coordinates=tmp_path/(clip['id']+'.json')
        coordinates.write_text(json.dumps({'videoFileHash':clip['media']['fileHash'],'role':clip['role'],'boxes':[[1,1,20,20]]*100}))
        entry=assets.register('coords-'+clip['id'],'mixed',str(coordinates),'')
        clip['faceCoordinates']={'artifactId':entry['artifactId'],'fileHash':entry['fileHash']}
    pack=packs.update(pack.id,draft,pack.revision)
    pack=adopt(packs,pack,'dual')
    path=tmp_path/'master.wav'
    with wave.open(str(path),'wb') as output:
        output.setparams((1,2,48000,0,'NONE','not compressed'));output.writeframes(b'\x01\x00'*96000)
    assets.register('master','audio',str(path),'')
    project=Project.model_validate({'id':'cpu','motionPack':{'id':pack.id,'version':pack.version},
        'currentDraftRevision':'R1','scriptRevisions':[{'id':'R1','turns':[{'id':'T1','speaker':'A'},{'id':'T2','speaker':'B'}]}],
        'audioTimeline':{'revisionId':'R1','sampleRate':48000,'sampleCount':96000,'masterAudioAssetId':'master',
        'unitOffsets':{'U1':0,'U2':48000},'units':[{'id':'U1','turnId':'T1','sampleCount':48000},{'id':'U2','turnId':'T2','sampleCount':48000}]}})
    from duocast.services.voice_svc import _unit_fingerprint
    for turn,unit in zip(project.script_revisions[0].turns,project.audio_timeline.units):
        unit.pronunciation_revision=_unit_fingerprint(turn,unit,[])
    return project,assets,packs


def test_cpu_segment_report_is_a_candidate_not_gpu_reuse(pcm_project):
    project,assets,packs=pcm_project
    first=motion_impact(project,assets,packs)
    assert first['available'] and first['candidateCount']==0 and not first['baselineAvailable']
    same=motion_impact(project,assets,packs,project)
    assert same['candidateCount']==len(same['segments'])
    assert not same['cacheOutputsVerified'] and not same['runtimeVerified']
    encoded=project.model_copy(deep=True);encoded.render_spec={'fps':30,'resolution':'1920x1080'}
    assert motion_impact(encoded,assets,packs,project)['candidateCount']==len(same['segments'])
    changed=project.model_copy(deep=True);changed.audio_timeline.units[0].sample_count=47000
    report=motion_impact(changed,assets,packs,project)
    assert report['available'] and report['candidateCount']<len(report['segments'])


def test_cpu_plan_reports_missing_corrupt_and_stale_inputs(pcm_project):
    project,assets,packs=pcm_project
    stale=project.model_copy(deep=True);stale.current_draft_revision='absent'
    assert not motion_impact(stale,assets,packs)['available']
    changed=project.model_copy(deep=True);changed.script_revisions[0].turns[0].speed_ratio=1.2
    assert not motion_impact(changed,assets,packs)['available']
    Path(assets.by_id('master')['path']).write_bytes(b'corrupted')
    report=motion_impact(project,assets,packs)
    assert not report['available'] and '改变' in report['blockers'][0]


def test_cpu_preflight_reports_frontend_missing_and_does_not_require_gpu(tmp_path):
    source=Path(__file__).parents[3]/'scripts/check_cpu_environment.py'
    spec=importlib.util.spec_from_file_location('cpu_preflight',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    report=module.inspect(tmp_path,check_port=False)
    assert not report['ready']
    assert not next(row for row in report['checks'] if row['name']=='前端生产构建')['ok']
    assert all('cuda' not in row['name'].lower() and 'torch' not in row['name'].lower() for row in report['checks'])


def test_public_cpu_endpoints_preserve_unknown_job_and_report_missing_inputs(client):
    job=Job(id='no-external-id',kind='motion.prepare',status=JobStatus.UNKNOWN,
        owner_type='motionPack',owner_id='pack')
    app.state.job_manager._jobs[job.id]=job
    response=client.get('/api/jobs/no-external-id/external-status')
    assert response.status_code==200 and not response.json()['canInspect']
    assert job.status==JobStatus.UNKNOWN
    assert client.get('/api/jobs/absent/external-status').status_code==404
    project=client.post('/api/projects',json={'title':'CPU plan'}).json()
    plan=client.get(f"/api/projects/{project['id']}/plan/motion")
    assert plan.status_code==200 and not plan.json()['available']
    assert client.get('/api/projects/absent/plan/motion').status_code==404


def test_cpu_fixture_workspace_uses_real_small_media_and_refuses_overwrite(tmp_path):
    if not __import__('shutil').which('ffmpeg'):
        pytest.skip('FFmpeg required for synthetic CPU media')
    source=Path(__file__).parents[3]/'scripts/acceptance/seed_cpu_workspace.py'
    spec=importlib.util.spec_from_file_location('cpu_seed',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    destination=tmp_path/'new-workspace'
    report=module.seed(destination)
    assert report['syntheticFixture'] and report['videoFrames']==75
    audio=destination/'artifacts/cpu-fixtures/tone.wav'
    before=audio.read_bytes()
    with wave.open(str(audio),'rb') as wav:
        assert wav.getnframes()==144000 and wav.getframerate()==48000
    with pytest.raises(ValueError,match='new or empty'):
        module.seed(destination)
    assert audio.read_bytes()==before
