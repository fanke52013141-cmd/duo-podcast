import copy
import io
import json
import wave
import zipfile
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from duocast.domain.project import Project, ScriptRevision, Turn, Line
from duocast.domain.audio import AudioTimeline
from duocast.services.video_plans import camera_plan, clipped_camera_plan, adaptive_prompt
from duocast.services.render_inputs import validate_render_options


def episode():
    return {"scriptRevisions": [{"id": "r", "turns": [{"id":str(i),"speaker":"A" if i%2==0 else "B"} for i in range(4)]}],
            "audioTimeline": {"revisionId":"r", "sampleRate":1000,"sampleCount":15000,
                              "units":[{"id":str(i),"turnId":str(i)} for i in range(4)],
                              "unitOffsets":{"0":0,"1":3500,"2":6500,"3":9500}}}


def test_preview_keeps_episode_camera_decisions():
    plan=camera_plan(episode())
    preview=clipped_camera_plan(plan,2,7)
    assert [(s['start'],s['end'],s['camera']) for s in preview]==[(2,3.5,'twoShot'),(3.5,6.5,'B'),(6.5,7,'A')]
    assert all(s['end']-s['start']>=2 for s in plan)


def test_short_turns_do_not_create_short_cutaways():
    data=episode(); data['audioTimeline']['unitOffsets']={'0':0,'1':100,'2':200,'3':300}
    assert camera_plan(data)==[{'start':0.0,'end':15.0,'camera':'twoShot','reason':'opening'}]


def test_listener_is_selected_from_roles_and_asset_positions():
    regions={'A':{'x':0.5,'width':0.5},'B':{'x':0,'width':0.5}}
    prompt=adaptive_prompt([{'start':9.5,'end':15,'speaker':'B','listener':'A'}],regions,10,15)
    assert prompt.startswith('At the beginning, the host on the left speaks; the host on the right')
    assert 'From 0.32 to 5.32 seconds' in prompt
    assert 'woman' not in prompt and 'man on' not in prompt


def test_deleted_turn_invalidates_old_audio_without_voice_bindings():
    from test_render_dependencies import project
    p=project();p.voice_bindings=[];p.script_revisions[0].turns.pop()
    with pytest.raises(HTTPException) as exc: validate_render_options(p,{},real_video=False)
    assert exc.value.status_code==409


@pytest.mark.parametrize('start',[-5,2.5,float('nan'),True,20])
def test_local_redo_boundary_rejected(start):
    from test_render_dependencies import project
    with pytest.raises(HTTPException) as exc: validate_render_options(project(),{'redoFromSec':start,'reuseMotionArtifactId':'source'},real_video=False)
    assert exc.value.status_code==422


def test_historical_delivery_uses_frozen_script_and_reuses_zip(tmp_path):
    from duocast.api.export import router
    from duocast.storage.artifacts import ArtifactStore
    from duocast.storage.project_store import ProjectStore
    store=ProjectStore(tmp_path/'projects');base=store.create('export','delivery')
    p=Project(id='export',current_draft_revision='r',script_revisions=[ScriptRevision(id='r',turns=[Turn(id='t',speaker='A',lines=[Line(id='l',display_text='original',spoken_text='original')])])],audio_timeline=AudioTimeline(revision_id='r',sample_count=48000,unit_offsets={'u':0},units=[]))
    from duocast.domain.audio import SynthesisUnit
    p.audio_timeline.units=[SynthesisUnit(id='u',turn_id='t',sample_count=48000)]
    p.audio_timeline.master_audio_asset_id='audio'
    assets=ArtifactStore(tmp_path/'artifacts');audio=tmp_path/'master.wav'
    with wave.open(str(audio),'wb') as w: w.setparams((1,2,48000,0,'NONE','not compressed'));w.writeframes(bytes(96000))
    video=tmp_path/'video.mp4';video.write_bytes(b'video fixture')
    assets.register('audio','audio',str(audio),'a')
    assets.register('video','video',str(video),'v',{'projectSnapshot':p.model_dump(by_alias=True),'projectId':p.id,'dependencyHash':'original'})
    manifest={'version':'OUT-r-v1','artifactIds':['video'],'meta':{}}
    p.script_revisions[0].turns[0].lines[0].display_text='new edit'
    p.output_manifest=manifest;p.output_history=[manifest]
    store.apply('export',p.model_dump(),expected_revision=base.revision)
    app=FastAPI();app.include_router(router);app.state.project_store=store;app.state.artifacts=assets
    with TestClient(app) as client:
        one=client.get('/api/projects/export/export/package?version=OUT-r-v1')
        two=client.get('/api/projects/export/export/package?version=OUT-r-v1')
    assert one.status_code==two.status_code==200 and one.content==two.content
    with zipfile.ZipFile(io.BytesIO(one.content)) as z:
        assert 'original' in z.read('dialogue.srt').decode('utf-8-sig')
        assert 'new edit' not in z.read('dialogue.srt').decode('utf-8-sig')
    assert len(list((assets.root/'export').glob('*/delivery.zip')))==1


def test_only_resumable_model_jobs_requeue_after_restart(tmp_path):
    from duocast.domain.job import Job,JobStatus
    from duocast.jobs.recovery import recover_on_startup
    for kind,resumable in [('renders',True),('tts.synthesize',False)]:
        job=Job(id=kind,kind=kind,status=JobStatus.RUNNING,input_snapshot={'resumableVideo':resumable})
        folder=tmp_path/kind;folder.mkdir();(folder/'job.json').write_text(job.model_dump_json(by_alias=True),encoding='utf-8')
    states={j.kind:j.status for j in recover_on_startup(tmp_path)}
    assert states['renders']==JobStatus.QUEUED
    assert states['tts.synthesize']==JobStatus.UNKNOWN


def test_manual_camera_override_is_applied_to_global_plan():
    p=episode();p['shotCameraOverrides']={'1':'A'}
    assert camera_plan(p)[1]['camera']=='A'
    assert camera_plan(p)[1]['reason']=='manual'


def test_retiming_preserves_adopted_samples_and_removes_approvals(tmp_path):
    from duocast.services.voice_svc import retime_adopted_audio
    from duocast.domain.project import Approval
    from duocast.storage.project_store import ProjectStore
    from duocast.storage.artifacts import ArtifactStore
    from test_render_dependencies import project
    assets=ArtifactStore(tmp_path/'artifacts');store=ProjectStore(tmp_path/'projects')
    base=store.create('test','rhythm');p=project()
    for index,unit in enumerate(p.audio_timeline.units):
        path=tmp_path/f'{index}.wav'
        with wave.open(str(path),'wb') as w:
            w.setparams((1,2,48000,0,'NONE','not compressed'));w.writeframes((index+1).to_bytes(2,'little')*48000)
        aid=f'a{index}';unit.adopted_audio_asset_id=aid
        assets.register(aid,'audio',str(path),'unit')
    p.approvals=[Approval(kind='voice'),Approval(kind='sample'),Approval(kind='script')]
    store.apply('test',p.model_dump(),expected_revision=base.revision)
    result=retime_adopted_audio(store,'test',[180],assets)
    assert result.unit_offsets['U-T2']==48000+8640
    assert result.sample_count==104640
    assert [u.adopted_audio_asset_id for u in result.units]==['a0','a1']
    entry=assets.by_id(result.master_audio_asset_id)
    with wave.open(entry['path']) as w: pcm=w.readframes(w.getnframes())
    assert pcm[:96000]==b'\x01\x00'*48000
    assert pcm[96000:113280]==bytes(17280)
    assert pcm[113280:]==b'\x02\x00'*48000
    assert [a.kind for a in store.load('test').approvals]==['script']


def test_legacy_profile_detects_same_revision_text_edit():
    from test_render_dependencies import project
    p=project();p.voice_bindings=[]
    validate_render_options(p,{},real_video=False)
    p.script_revisions[0].turns[0].lines[0].spoken_text='edited'
    with pytest.raises(HTTPException) as exc:validate_render_options(p,{},real_video=False)
    assert exc.value.status_code==409
