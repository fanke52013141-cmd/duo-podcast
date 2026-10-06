import copy
import json
from pathlib import Path
import pytest
from fastapi import HTTPException
from duocast.services.generation_profiles import configure_generation_profile, validate_motion_reference, listener_layer_filter
from duocast.services.render_inputs import validate_render_options
from duocast.services.render_inputs import validate_motion_source
from duocast.storage.artifacts import ArtifactStore
from test_render_dependencies import project


def workflow():
    return json.loads((Path(__file__).parents[1]/'duocast/adapters/multitalk_workflow.json').read_text(encoding='utf-8'))


def test_accepted_profile_preserves_workflow_exactly():
    wf=workflow();before=copy.deepcopy(wf)
    configure_generation_profile(wf,'accepted')
    assert wf==before


def test_matched_profile_changes_only_sampling_mode():
    wf=workflow();before=copy.deepcopy(wf)
    configure_generation_profile(wf,'matched')
    before['192']['inputs']['mode']='auto'
    assert wf==before


def test_reference_uses_video_latents_and_partial_denoising():
    wf=workflow();configure_generation_profile(wf,'reference','reference.mp4')
    assert wf['192']['inputs']['mode']=='auto'
    assert wf['motion_reference']['inputs']['force_rate']==25
    assert wf['motion_reference']['inputs']['frame_load_cap']==['223',0]
    assert wf['motion_latents']['inputs']['image']==['motion_reference',0]
    assert wf['199']['inputs']['samples']==['motion_latents',0]
    assert wf['199']['inputs']['start_step']==2
    assert wf['199']['inputs']['add_noise_to_samples'] is True


@pytest.mark.parametrize('filename',[None,'../reference.mp4','folder/reference.mp4'])
def test_reference_filename_must_be_owned_input_basename(filename):
    with pytest.raises(ValueError): configure_generation_profile(workflow(),'reference',filename)


def test_unknown_profile_rejected_before_queue():
    with pytest.raises(HTTPException) as exc:validate_render_options(project(),{'generationProfile':'unknown'})
    assert exc.value.status_code==422


def test_changed_profile_cannot_pass_off_original_motion_as_regenerated():
    p=project()
    class Source:
        def by_id(self, _):
            return {'paramsSnapshot':{'projectId':p.id,'projectSnapshot':p.model_dump(by_alias=True),
                    'visualVariantId':'V1','seed':1,'generationProfile':'accepted'}}
    payload={'reuseMotionArtifactId':'VID','visualVariantId':'V1','generationProfile':'matched'}
    with pytest.raises(HTTPException) as exc:validate_motion_source(p,payload,Source())
    assert exc.value.status_code==409
    validate_motion_source(p,{**payload,'redoFromSec':0},Source())


def reference(tmp_path):
    p=project();p.visual_variants[0].master_image.artifact_id='IMG1'
    store=ArtifactStore(tmp_path);path=tmp_path/'reference.mp4';path.write_bytes(b'fixture')
    params={'projectId':p.id,'purpose':'motionReference','visualArtifactId':'IMG1',
            'rangeStartSec':0,'rangeEndSec':p.audio_timeline.sample_count/p.audio_timeline.sample_rate}
    store.register('REF','video',str(path),'hash',params)
    payload={'generationProfile':'reference','motionReferenceArtifactId':'REF','redoFromSec':0,'visualVariantId':'V1'}
    return p,store,payload,path


def test_owned_reference_matches_asset_and_interval(tmp_path):
    p,store,payload,_=reference(tmp_path)
    validate_motion_reference(p,payload,store)


@pytest.mark.parametrize('change',['owner','image','interval','file','profile'])
def test_reference_rejects_mismatched_inputs(tmp_path,change):
    p,store,payload,path=reference(tmp_path)
    if change=='owner':p.id='other'
    if change=='image':p.visual_variants[0].master_image.artifact_id='IMG2'
    if change=='interval':payload['redoFromSec']=5
    if change=='file':path.write_bytes(b'changed')
    if change=='profile':payload['generationProfile']='matched'
    with pytest.raises(HTTPException):validate_motion_reference(p,payload,store)


def test_listener_blending_uses_normalized_coordinates_for_all_chroma_planes():
    regions={'A':{'x':0,'width':0.5},'B':{'x':0.5,'width':0.5}}
    filters=listener_layer_filter('A',[0.51,0.55],regions)
    assert 'X/W' in filters and 'if(lt(X,' not in filters
    assert '0.5100000000),A' in filters
    assert '0.5500000000)' in filters
    assert '0.0400000000,B))' in filters
    reversed_filter=listener_layer_filter('B',[0.51,0.55],regions)
    assert '0.5100000000),B' in reversed_filter
    assert '0.0400000000,A))' in reversed_filter


@pytest.mark.parametrize('band',[None,[0,1],[0.2,0.5],[0.9,0.95],[float('nan'),0.5]])
def test_invalid_listener_band_rejected(band):
    with pytest.raises(ValueError):listener_layer_filter('A',band,{'A':{'x':0,'width':0.5},'B':{'x':0.5,'width':0.5}})


def test_listener_layer_rejects_overwriting_any_listener_speech(tmp_path):
    p,store,payload,path=reference(tmp_path)
    entry=store.by_id('REF');params=entry['paramsSnapshot']
    params.update({'listenerSpeaker':'A','blendBand':[0.51,0.55]})
    store.register('REF-layer','video',str(path),'hash',params)
    payload.update({'motionReferenceArtifactId':'REF-layer','generationProfile':'listener','reuseMotionArtifactId':'VID'})
    with pytest.raises(HTTPException) as exc:validate_motion_reference(p,payload,store)
    assert '倾听者发言' in exc.value.detail
    p.script_revisions[0].turns[0].speaker='B'
    validate_motion_reference(p,payload,store)


def test_motion_reference_always_receives_safe_silent_audio(tmp_path):
    import asyncio
    from duocast.adapters.comfyui_video import ComfyUIVideoProvider
    calls=[]
    provider=ComfyUIVideoProvider(base_url="http://localhost",input_dir=tmp_path,artifact_store=None)
    async def command(*args):calls.append(args)
    provider._command=command
    asyncio.run(provider._prepare_motion_reference("silent.mp4",tmp_path/"prepared.mp4"))
    args=calls[0]
    assert "anullsrc=r=48000:cl=mono" in args
    assert args[args.index("-c:v")+1]=="copy"
    assert "0:v:0" in args and "1:a:0" in args and "-shortest" in args


@pytest.mark.parametrize("duration,frames",[(9.88,247),(14.88,371),(0,0)])
def test_incomplete_final_video_rejected(duration,frames):
    from duocast.services.generation_profiles import validate_video_stream
    with pytest.raises(ValueError):validate_video_stream({"duration":duration,"nb_frames":frames},14.880375,25)


def test_complete_final_video_accepted():
    from duocast.services.generation_profiles import validate_video_stream
    validate_video_stream({"duration":"14.88","nb_frames":"372"},14.880375,25)
