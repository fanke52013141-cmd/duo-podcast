import asyncio
from dataclasses import replace
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from duocast.main import app, settings
from duocast.domain.project import Project
from duocast.adapters.musetalk_video import ModeVideoProvider, MuseTalkVideoProvider, validate_fast_options
from duocast.storage.artifacts import ArtifactStore
from duocast.services.render_inputs import dependency_hash, visual_dependency_hash

def test_legacy_hashes_and_default_mode():
    project=Project(id='legacy')
    assert project.generation_mode=='standard'
    data=project.model_dump(by_alias=True)
    data.pop('generationMode')
    assert dependency_hash(project)==dependency_hash(data)
    assert visual_dependency_hash(project)==visual_dependency_hash(data)
    fast=project.model_copy(update={'generation_mode':'fast'})
    assert dependency_hash(fast)!=dependency_hash(project)
    assert visual_dependency_hash(fast)!=visual_dependency_hash(project)

def fake_install(root):
    for name in ('worker.py','quality.py','render_geometry.py','assets/manifest.json','venv/Scripts/python.exe','assets/stage.png','assets/parsing_bisenet.pth',
        'assets/template_A.mp4','assets/template_B.mp4','assets/face_coordinates.json',
        'models/musetalkV15/unet.pth','models/sd-vae/diffusion_pytorch_model.bin','models/whisper/pytorch_model.bin'):
        path=root/name
        path.parent.mkdir(exist_ok=True,parents=True)
        path.write_bytes(b'test fixture')

def test_create_validation_and_template_persistence(tmp_path,monkeypatch):
    monkeypatch.setattr('duocast.main.settings',replace(settings,storage_root=tmp_path/'storage'))
    monkeypatch.delenv('DUOCAST_FAST_ENGINE',raising=False)
    with TestClient(app) as client:
        assert client.post('/api/projects',json={'id':'bad','generationMode':'unknown'}).status_code==422
        assert client.post('/api/projects',json={'id':'missing','generationMode':'fast'}).status_code==422
        assert client.get('/api/projects').json()==[]
        root=tmp_path/'engine'
        fake_install(root)
        monkeypatch.setenv('DUOCAST_FAST_ENGINE',str(root))
        result=client.post('/api/projects',json={'id':'FAST','generationMode':'fast'})
        assert result.status_code==200,result.text
        project=Project.model_validate(result.json())
        assert project.generation_mode=='fast'
        assert project.camera_mode=='twoShot'
        assert project.selected_visual_variant_id=='FAST-STUDIO-V1'
        validate_fast_options(project,{})
        changed=project.model_copy(deep=True)
        changed.visual_variants[0].master_image.file_hash='other image'
        with pytest.raises(ValueError,match='模板'): validate_fast_options(changed,{})
        with pytest.raises(ValueError,match='标准生成'): validate_fast_options(project,{'generationProfile':'dialogue'})
        for payload in ({'reuseMotionArtifactId':'old'},{'redoFromSec':0}):
            with pytest.raises(ValueError): validate_fast_options(project,payload)
        reread=client.get('/api/projects/FAST').json()
        assert reread['generationMode']=='fast'
        assert client.patch('/api/projects/FAST',json={'generationMode':'standard','expectedRevision':project.revision}).status_code==422

def test_master_mixed_audio_and_tamper_detection(tmp_path):
    artifacts=ArtifactStore(tmp_path)
    path=tmp_path/'master.wav'
    path.write_bytes(b'original')
    artifacts.register('master','mixed',str(path),'dependency')
    adapter=MuseTalkVideoProvider(artifacts)
    assert adapter.asset('master','audio')==path
    with pytest.raises(RuntimeError): adapter.asset('master','image')
    path.write_bytes(b'changed')
    with pytest.raises(RuntimeError): adapter.asset('master','audio')

def test_dispatch_uses_frozen_project_mode():
    class Stub:
        capabilities={'simulated':False}
        def __init__(self,name): self.name=name
        async def render(self,req,progress=None): return self.name
    standard=Stub('standard')
    provider=ModeVideoProvider(standard,None)
    provider.fast=Stub('fast')
    assert asyncio.run(provider.render({'projectSnapshot':{}}))=='standard'
    assert asyncio.run(provider.render({'projectSnapshot':{'generationMode':'fast'}}))=='fast'
