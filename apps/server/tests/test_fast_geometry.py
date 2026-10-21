import importlib.util
from pathlib import Path
import pytest

SOURCE=Path(__file__).resolve().parents[3]/'scripts/fast-mode'
def module(name):
    spec=importlib.util.spec_from_file_location(name,SOURCE/(name+'.py'))
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result
geometry=module('render_geometry')

def test_composite_preserves_hd_and_enlarges_before_mouth_blend():
    assert geometry.composite_size(640,720,1920,1080)==(960,1080)
    assert geometry.composite_size(432,480,1920,1080)==(972,1080)
    assert geometry.composite_size(960,1080,854,480)==(960,1080)
    projected=geometry.project_box([10,20,110,150],(432,480),(972,1080))
    assert projected==(22,45,248,338)
    with pytest.raises(ValueError):geometry.composite_size(0,480,1920,1080)
    with pytest.raises(ValueError):geometry.project_box([10,10,10,10],(432,480),(972,1080))

def test_cache_invalidated_by_motion_coordinates_and_vae(tmp_path):
    video=tmp_path/'motion.mp4';video.write_bytes(b'original')
    config=tmp_path/'config.json';config.write_text('{}')
    weights=tmp_path/'vae.bin';weights.write_bytes(b'weights')
    def key(boxes):return geometry.preparation_key(video,boxes,config,weights)
    original=key([[1,2,30,40]])
    assert original==key([[1,2,30,40]])
    assert original!=key([[2,2,30,40]])
    video.write_bytes(b'changed')
    assert original!=key([[1,2,30,40]])
    other=key([[1,2,30,40]])
    weights.write_bytes(b'new-weights')
    assert other!=key([[1,2,30,40]])

def test_runtime_installer_includes_all_required_modules(tmp_path):
    install=module('install_runtime')
    runtime=tmp_path/'runtime';(runtime/'models').mkdir(parents=True);(runtime/'assets').mkdir()
    install.install(SOURCE,runtime)
    for name in ('worker.py','render_geometry.py','quality.py','runtime_version.json'):
        assert (runtime/name).is_file()

def test_template_validation_accepts_native_hd_and_rejects_stale_boxes(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    validator=module('validate_template')
    assets=tmp_path/'assets';assets.mkdir()
    (assets/'manifest.json').write_text(json.dumps({'turns':[{'speaker':'A','start':0,'end':.04},{'speaker':'B','start':.16,'end':.2}]}))
    boxes={s:[[10,10,60,60]]*6 for s in ('A','B')}
    coords=assets/'face_coordinates.json';coords.write_text(json.dumps(boxes))
    video={'width':640,'height':720,'nb_frames':'6','avg_frame_rate':'25/1'}
    monkeypatch.setattr(validator.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(stdout=json.dumps({'streams':[video]})))
    assert all(r['nativeHD'] for r in validator.validate(tmp_path)['roles'])
    boxes['A'][0]=[0,0,900,800];coords.write_text(json.dumps(boxes))
    with pytest.raises(ValueError,match='outside'):validator.validate(tmp_path)

def test_template_validation_requires_both_speech_and_listening(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    validator=module('validate_template')
    assets=tmp_path/'assets';assets.mkdir()
    (assets/'manifest.json').write_text(json.dumps({'turns':[{'speaker':s,'start':0,'end':1} for s in ('A','B')]}))
    (assets/'face_coordinates.json').write_text(json.dumps({s:[[10,10,60,60]]*25 for s in ('A','B')}))
    video={'width':640,'height':720,'nb_frames':'25','avg_frame_rate':'25/1'}
    monkeypatch.setattr(validator.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(stdout=json.dumps({'streams':[video]})))
    with pytest.raises(ValueError,match='listening'):validator.validate(tmp_path)
