"""ROI may save model pixels, never rescale faces or replace untouched background."""
import importlib
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]/'scripts/acceptance'))
roi = importlib.import_module('expert_roi')


def test_crop_covers_both_complete_trajectories_at_native_pixel_scale():
    segment={'geometry':{'native':[864,480,25]},'sourceByRole':{
        'A':[{'faceBox':[216,110,327,253]},{'faceBox':[206,100,330,260]}],
        'B':[{'faceBox':[519,119,648,283]}]}}
    x,y,w,h=roi.joint_crop([segment],[864,480])
    assert all(v%16==0 for v in (x,y,w,h)) and w*h<864*480
    for values in segment['sourceByRole'].values():
        for item in values:
            a,b,c,d=item['faceBox']
            assert x<a<c<x+w and y<b<d<y+h


def test_native_patch_preserves_all_pixels_outside_face_feather():
    np=pytest.importorskip('numpy')
    source=np.full((480,864,3),40,dtype=np.uint8)
    patch=np.full((336,576,3),200,dtype=np.uint8)
    result=roi.composite_frame(source,patch,[144,32,576,336],[[216,110,327,253],[519,119,648,283]])
    assert np.array_equal(result[:32],source[:32])
    assert np.array_equal(result[:, :144],source[:, :144])
    assert np.array_equal(result[400:],source[400:])
    assert (result[180,270]==200).all() and (result[200,580]==200).all()
    assert (result[340,400]==40).all()
    assert np.any((result>40)&(result<200))  # Smooth feather, no hard rectangular edge.
    with pytest.raises(ValueError,match='without resizing'):
        roi.composite_frame(source,patch[:100],[144,32,576,336],[[216,110,327,253]])


def test_face_outside_crop_rejected():
    np=pytest.importorskip('numpy')
    with pytest.raises(ValueError,match='outside fixed ROI'):
        roi.composite_frame(np.zeros((480,864,3),dtype=np.uint8),np.zeros((336,576,3),dtype=np.uint8),
                            [144,32,576,336],[[10,10,100,100]])


def test_roi_cannot_be_stretched_into_full_frame(tmp_path):
    import json
    module=importlib.import_module('assemble_expert_benchmark')
    state={'completed':True,'plan':{'segments':[{'index':0,'nativeSize':[576,336]}]},'results':[{'index':0}]}
    request={'expertPlan':{'segments':[{}]}}
    p=tmp_path/'state.json';p.write_text(json.dumps(state),encoding='utf-8')
    r=tmp_path/'request.json';r.write_text(json.dumps(request),encoding='utf-8')
    with pytest.raises(ValueError,match='native source composition'):
        module.assemble(p,r,tmp_path/'ref.mp4',tmp_path/'out.mp4')


def test_delivery_master_must_match_frozen_model_conditioning(tmp_path):
    import hashlib
    import struct
    import wave
    module=importlib.import_module('assemble_expert_benchmark')
    def write(path, samples):
        with wave.open(str(path),'wb') as out:
            out.setparams((1,2,48000,0,'NONE','not compressed'))
            out.writeframes(struct.pack('<'+'h'*len(samples),*samples))
    master=tmp_path/'master.wav';prepared=tmp_path/'mix.wav'
    write(master,list(range(10)));write(prepared,[0,0,*range(2,8),0])
    request={'master':str(master),'sampleRate':48000,'sampleCount':10,
             'expertPlan':{'segments':[{'audioSlice':{'startSample':2,'endSample':8,
                 'prefixSilentSamples':2,'suffixSilentSamples':1}}]}}
    specs=[{'index':0,'workflow':{'242':{'inputs':{'audio':'mix.wav'}}}}]
    inputs={'mix.wav':{'path':str(prepared),'fileHash':hashlib.sha256(prepared.read_bytes()).hexdigest()}}
    assert module.verify_master_pcm(request,specs,inputs)==master
    write(master,[0,1,2,99,4,5,6,7,8,9])
    with pytest.raises(ValueError,match='no longer matches'):
        module.verify_master_pcm(request,specs,inputs)
