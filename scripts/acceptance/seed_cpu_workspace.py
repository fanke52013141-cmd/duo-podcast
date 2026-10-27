"""Create a NEW CPU-only acceptance workspace with PCM and FFmpeg test patterns.

These are explicit synthetic fixtures, never real generated speech or reviewed
motion. Refuses any nonempty destination; does not start servers or engines.
"""
import argparse
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import wave

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'apps/server'))


def seed(destination):
    from duocast.storage.artifacts import ArtifactStore
    from duocast.storage.project_store import ProjectStore
    from duocast.storage.motion_packs import MotionPackStore
    from duocast.core.eventbus import EventBus
    from duocast.jobs.manager import JobManager
    from duocast.domain.job import Job,JobStatus
    destination=Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Destination must be new or empty; no existing project data is changed')
    if not shutil.which('ffmpeg'):
        raise ValueError('FFmpeg is required for CPU video fixtures')
    destination.mkdir(parents=True,exist_ok=True)
    assets=ArtifactStore(destination/'artifacts')
    fixtures=assets.root/'cpu-fixtures';fixtures.mkdir()
    audio=fixtures/'tone.wav'
    with wave.open(str(audio),'wb') as output:
        output.setparams((1,2,48000,0,'NONE','not compressed'))
        output.writeframes(b''.join(struct.pack('<h',int(8000*math.sin(i*2*math.pi*440/48000)*(0.3+0.7*math.sin(i*math.pi/48000)**2))) for i in range(144000)))
    video=fixtures/'motion-test-pattern.mp4'
    subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','testsrc2=size=320x180:rate=25:duration=3',
        '-an','-c:v','libx264','-pix_fmt','yuv420p',str(video)],check=True,timeout=30)
    image=fixtures/'reference.png'
    subprocess.run(['ffmpeg','-v','error','-nostdin','-i',str(video),'-frames:v','1',str(image)],check=True,timeout=30)
    refs={}
    for id,kind,file in [('CPU-WAV','audio',audio),('CPU-VIDEO','video',video),('CPU-IMAGE','image',image)]:
        entry=assets.register(id,kind,str(file),'cpu-fixture',{'syntheticFixture':True})
        refs[id]={'artifactId':id,'fileHash':entry['fileHash']}
    packs=MotionPackStore(destination/'motion-packs',assets.by_id)
    pack=packs.create({'name':'CPU测试图案 · 不代表真实角色动作',
        'characters':{role:{'characterId':role,'referenceImage':refs['CPU-IMAGE']} for role in 'AB'},
        'cameras':[{'id':'dual','mode':'twoShot','masterImage':refs['CPU-IMAGE']}],
        'clips':[{'id':'clip-'+role,'role':role,'cameraId':'dual','state':'speaking','media':refs['CPU-VIDEO'],
            'nativeWidth':320,'nativeHeight':180,'fps':25,'frameCount':75,'usableEndFrame':75,
            'provenance':{'sourceMedia':refs['CPU-VIDEO'],'parameters':{'syntheticFixture':True}}} for role in 'AB']},'CPU-REVIEW')
    store=ProjectStore(destination/'projects')
    project=store.create('CPU-MEDIA','CPU验收 · 测试音而非真实语音')
    turns=[{'id':f'T{i+1}','speaker':role,'lines':[{'id':f'L{i+1}','displayText':text,'spokenText':text}]} for i,(role,text) in enumerate([
        ('A','这是无显卡验收的第一句。'),('B','现有音频可以离线检查。'),('A','波形和选段均读取实际文件。')])]
    store.apply(project.id,{'currentDraftRevision':'R1','scriptRevisions':[{'id':'R1','turns':turns}],
        'audioTimeline':{'revisionId':'R1','sampleRate':48000,'sampleCount':144000,'masterAudioAssetId':'CPU-WAV',
            'unitOffsets':{f'U{i+1}':i*48000 for i in range(3)},
            'units':[{'id':f'U{i+1}','turnId':f'T{i+1}','sampleCount':48000} for i in range(3)]}},project.revision)
    store.flush()
    manager=JobManager(destination/'jobs',EventBus(destination/'events.jsonl'))
    manager._persist(Job(id='CPU-UNKNOWN',kind='motion.prepare',owner_type='motionPack',owner_id=pack.id,
        status=JobStatus.UNKNOWN,stage='CPU fixture; no engine submission',
        input_snapshot={'syntheticFixture':True},error='CPU验收：缺少已知外部ID，不能重复提交'))
    report={'syntheticFixture':True,'projectId':project.id,'motionPackId':pack.id,
        'audioSeconds':3,'videoFrames':75,'notice':'仅用于播放器、逐帧、迁移和状态UI测试；无模型推理、真实语音、人工采用或画质证明。'}
    (destination/'CPU-FIXTURES.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--storage',required=True)
    args=parser.parse_args()
    print(json.dumps(seed(args.storage),ensure_ascii=False,indent=2))
