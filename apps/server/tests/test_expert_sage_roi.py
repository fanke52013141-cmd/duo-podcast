import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import httpx
import pytest
from duocast.adapters.expert_motion_provider import ExpertMotionVideoProvider
from duocast.storage.artifacts import ArtifactStore


def test_sage_runtime_requires_exact_package_and_success_counter(tmp_path):
    provider=ExpertMotionVideoProvider(ArtifactStore(tmp_path/'artifacts'),None,base_url='http://engine.invalid',
        input_dir=tmp_path/'input',fast_runtime_root=tmp_path/'runtime',profile='expert-sage-roi-v1',sage_tree_hash='a'*64)
    async def inspect(value):
        async with httpx.AsyncClient(base_url='http://engine.invalid',transport=httpx.MockTransport(
                lambda request:httpx.Response(200,json={'attentionAddon':value}))) as client:
            return await provider._sage_identity(client)
    assert asyncio.run(inspect({'treeHash':'a'*64,'calls':160}))['calls']==160
    for invalid in ({},{'treeHash':'b'*64,'calls':160},{'treeHash':'a'*64,'calls':'160'}):
        with pytest.raises(ValueError):asyncio.run(inspect(invalid))


def test_roi_recipe_deployment_copies_are_identical():
    root=Path(__file__).resolve().parents[3]
    paths=[root/'apps/server/duocast/services/expert_roi_geometry.py',root/'scripts/fast-mode/expert_roi_geometry.py',root/'scripts/acceptance/expert_roi.py']
    assert len({hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})==1


def test_real_cpu_crop_and_composition_without_model(tmp_path):
    import os
    runtime=Path(os.environ.get('DUOCAST_ROI_TEST_PYTHON','D:/PPT_Studio_Assets/InfiniteTalk_TTS/InfiniteTalk_Runtime/venv/Scripts/python.exe'))
    if not runtime.is_file():pytest.skip('A cv2/numpy CPU runtime is required')
    root=Path(__file__).resolve().parents[3]
    script=tmp_path/'check.py'
    script.write_text('''
import sys,json,subprocess
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import cv2,numpy as np
from expert_roi_media import crop_prepared,compose
from expert_roi_geometry import composite_frame
from motion_worker import file_hash
root=Path(sys.argv[2]); native=np.full((480,640,3),120,dtype=np.uint8)
files={}
for kind,name in [('anchorImage','anchor.png'),('maskA','A.png'),('maskB','B.png')]:
    ok,data=cv2.imencode('.png',native);path=root/name;path.write_bytes(bytes(data));files[kind]={'path':str(path),'fileHash':file_hash(path)}
reference=root/'reference.mp4'
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=gray:s=640x480:r=25:d=0.2','-f','lavfi','-i','anullsrc=r=48000:cl=mono','-frames:v','5','-t','0.2','-c:v','libx264','-c:a','aac','-pix_fmt','yuv420p',str(reference)],check=True)
files['referenceVideo']={'path':str(reference),'fileHash':file_hash(reference)}
prepared={'files':files,'inputNames':{'anchorImage':'anchor.png','referenceVideo':'reference.mp4','roleMasks':{'A':'A.png','B':'B.png'}},'native':{'width':640,'height':480,'frames':5,'fps':25}}
crop=[128,64,384,320];prepared=crop_prepared(prepared,crop)
assert prepared['fullReference']['fileHash']==file_hash(reference)
assert prepared['native']['width']==384 and prepared['native']['height']==320
patch=root/'patch.mp4'
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=384x320:r=25:d=0.12','-frames:v','3','-an','-c:v','libx264','-pix_fmt','yuv420p',str(patch)],check=True)
spec={'modelFrames':5,'trimStartFrame':1,'trimEndFrame':4,'outputStartFrame':0,'outputEndFrame':3,'sourceByRole':{'A':[{'faceBox':[160,128,260,250]}]*5,'B':[{'faceBox':[380,128,480,250]}]*5}}
output=root/'composite.mp4'
report=compose({'segment':spec,'prepared':prepared,'core':{'path':str(patch),'fileHash':file_hash(patch)},'output':str(output)})
assert report['frames']==3 and report['outsideCropExactBeforeEncoding']
decoded=cv2.VideoCapture(str(output));assert int(decoded.get(cv2.CAP_PROP_FRAME_COUNT))==3
ok,frame=decoded.read();assert ok and frame.shape==(480,640,3);decoded.release()
region=np.full((320,384,3),255,dtype=np.uint8)
frame=composite_frame(native,region,crop,[[160,128,260,250],[380,128,480,250]])
assert np.array_equal(native[:64],frame[:64]) and np.array_equal(native[384:],frame[384:])
assert np.array_equal(native[:,:128],frame[:,:128]) and np.array_equal(native[:,512:],frame[:,512:])
print(json.dumps({'passed':True,'frames':3,'modelCalls':0}))
''',encoding='utf-8')
    result=subprocess.run([str(runtime),str(script),str(root/'scripts/fast-mode'),str(tmp_path)],capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['modelCalls']==0
