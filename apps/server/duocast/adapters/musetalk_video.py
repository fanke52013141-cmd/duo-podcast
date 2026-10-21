"""Server adapter for the isolated, offline fast-mode worker."""
import asyncio
import hashlib
import json
import os
import uuid
import wave
from pathlib import Path

def engine_root():
    value = os.environ.get('DUOCAST_FAST_ENGINE')
    return Path(value).resolve() if value else None

def require_engine():
    root = engine_root()
    required = ['worker.py', 'quality.py', 'render_geometry.py', 'assets/manifest.json', 'venv/Scripts/python.exe', 'assets/stage.png', 'assets/parsing_bisenet.pth',
                'assets/template_A.mp4', 'assets/template_B.mp4', 'assets/face_coordinates.json',
                'models/musetalkV15/unet.pth', 'models/sd-vae/diffusion_pytorch_model.bin', 'models/whisper/pytorch_model.bin']
    if root is None or any(not (root / p).is_file() for p in required):
        raise ValueError('快速模式引擎未安装完整，请检查本机配置')
    return root

def template_variant(artifacts):
    root = require_engine()
    path = root / 'assets/stage.png'
    digest = artifacts._hash_file(str(path))
    asset_id = 'IMG-FAST-' + digest[:20]
    if not artifacts.by_id(asset_id):
        artifacts.register(asset_id, 'image', str(path), digest, {'template': 'fast-studio-v1'}, 'musetalk-template-v1')
    return {'id': 'FAST-STUDIO-V1', 'masterImage': {'artifactId': asset_id, 'path': str(path), 'fileHash': digest},
            'aspect': 'landscape', 'mode': 'twoShot'}

def validate_fast_options(project, payload):
    if project.generation_mode != 'fast': return
    root = require_engine()
    visual_id = payload.get('visualVariantId') or project.selected_visual_variant_id
    visual = next((v for v in project.visual_variants if v.id == visual_id), None)
    expected = hashlib.sha256((root/'assets/stage.png').read_bytes()).hexdigest()
    if not visual or visual.mode != 'twoShot' or visual.master_image.file_hash != expected or project.aspect != 'landscape':
        raise ValueError('快速模式请使用已验证的横屏双人演播室模板')
    if payload.get('generationProfile', 'accepted') != 'accepted' or payload.get('reuseMotionArtifactId') or payload.get('redoFromSec') is not None:
        raise ValueError('快速模式直接按当前配音更新口型，请使用标准生成配置')

class ModeVideoProvider:
    def __init__(self, standard, artifacts):
        self.standard = standard
        self.fast = MuseTalkVideoProvider(artifacts)
        self.capabilities = {**standard.capabilities, 'generationModes': ['standard', 'fast']}

    async def render(self, req, progress=None):
        provider = self.fast if req['projectSnapshot'].get('generationMode') == 'fast' else self.standard
        return await provider.render(req, progress)

class MuseTalkVideoProvider:
    def __init__(self, artifacts):
        self.artifacts = artifacts

    def asset(self, asset_id, kind):
        entry = self.artifacts.by_id(asset_id)
        allowed = {'audio','mixed'} if kind == 'audio' else {kind}
        if not entry or entry['kind'] not in allowed or not Path(entry['path']).is_file() or self.artifacts._hash_file(entry['path']) != entry['fileHash']:
            raise RuntimeError('输入资产缺失或内容已改变')
        return Path(entry['path'])

    async def render(self, req, progress=None):
        from ..domain.project import Project
        from ..services.video_plans import camera_plan, clipped_camera_plan, crop_region
        project = Project.model_validate(req['projectSnapshot'])
        validate_fast_options(project, req)
        root = require_engine()
        timeline = req['projectSnapshot']['audioTimeline']
        visual_id = req.get('visualVariantId') or project.selected_visual_variant_id
        visual = next(v for v in project.visual_variants if v.id == visual_id)
        self.asset(visual.master_image.artifact_id, 'image')
        master = self.asset(timeline['masterAudioAssetId'], 'audio')
        revision = next(r for r in project.script_revisions if r.id == req['revisionId'])
        if timeline['revisionId'] != revision.id: raise RuntimeError('脚本与配音版本不一致')
        rate, total = timeline['sampleRate'], timeline['sampleCount']
        start = round(req.get('rangeStartSec', 0)*rate)
        end = min(total, round(req.get('rangeEndSec', total/rate)*rate))
        if not 0 <= start < end <= total: raise RuntimeError('非法生成区间')
        folder = self.artifacts.root / 'video_fast' / uuid.uuid4().hex
        folder.mkdir(parents=True)
        with wave.open(str(master), 'rb') as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (rate, 1, 2) or audio.getnframes() != total:
                raise RuntimeError('母轨必须为完整 PCM16 单声道音频')
            audio.setpos(start)
            pcm = audio.readframes(end-start)
        roles = {s: bytearray(len(pcm)) for s in ('A','B')}
        speakers = {t.id: t.speaker for t in revision.turns}
        turns = []
        for unit in timeline['units']:
            a = max(start, timeline['unitOffsets'][unit['id']])
            b = min(end, timeline['unitOffsets'][unit['id']] + unit['sampleCount'])
            if b <= a: continue
            speaker = speakers[unit['turnId']]
            roles[speaker][(a-start)*2:(b-start)*2] = pcm[(a-start)*2:(b-start)*2]
            turns.append({'speaker':speaker,'start':(a-start)/rate,'end':(b-start)/rate})
        for name, data in {'master':pcm, **roles}.items():
            with wave.open(str(folder/f'{name}.wav'),'wb') as audio:
                audio.setparams((1,2,rate,0,'NONE','not compressed'))
                audio.writeframes(data)
        worker_req = {'outputDir':str(folder), 'master':str(folder/'master.wav'), 'tracks':{s:str(folder/f'{s}.wav') for s in roles},
            'turns':turns, 'sampleRate':rate, 'sampleCount':end-start, 'seed':req.get('seed',42),
            'resolution':req.get('resolution','1920x1080'), 'fps':req.get('fps',25), 'fastQuality':req.get('fastQuality','original')}
        cameras = camera_plan(req['projectSnapshot'],req.get('cameraMode', project.camera_mode))
        worker_req['cameraPlan'] = [{**shot, 'start':shot['start']-start/rate, 'end':shot['end']-start/rate,
            'crop':crop_region(visual.person_regions[shot['camera']]) if shot['camera'] in ('A','B') else None}
            for shot in clipped_camera_plan(cameras,start/rate,end/rate)]
        request_file = folder/'request.json'
        request_file.write_text(json.dumps(worker_req),encoding='utf-8')
        if progress: progress('快速模式：更新双人口型')
        with (folder/'worker.log').open('wb') as log:
            process = await asyncio.create_subprocess_exec(str(root/'venv/Scripts/python.exe'), '-B', str(root/'worker.py'), str(request_file),
                cwd=str(root), stdout=log, stderr=log)
            try:
                await asyncio.wait_for(process.wait(), timeout=1800)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                process.kill()
                await process.wait()
                raise
        if process.returncode: raise RuntimeError(f'快速模式生成失败，详情见 {folder / "worker.log"}')
        final = folder/'final.mp4'
        probe = await asyncio.create_subprocess_exec('ffprobe','-v','error','-show_streams','-of','json',str(final),stdout=asyncio.subprocess.PIPE)
        info, _ = await probe.communicate()
        streams = json.loads(info)['streams']
        video = next(s for s in streams if s['codec_type']=='video')
        audio = next(s for s in streams if s['codec_type']=='audio')
        duration = (end-start)/rate
        if abs(float(video['duration'])-duration) > 1/worker_req['fps']+.005 or abs(float(audio['duration'])-duration) > .03:
            raise RuntimeError('成片声画时长不完整，未登记输出')
        artifact_id = 'VID-FAST-'+uuid.uuid4().hex
        self.artifacts.register(artifact_id,'video',str(final),req.get('dependencyHash',''),req,'musetalk-fast-v2')
        return {'artifactIds':[artifact_id], 'meta':{'path':str(final),'durationSec':duration,'resolution':worker_req['resolution'],
            'fps':worker_req['fps'],'fastQuality':worker_req['fastQuality'],'generationMode':'fast','cameraMode':req.get('cameraMode',project.camera_mode),'cameraPlan':cameras,'qualityStatus':'needs_review',
            'generationStats':json.loads((folder/'metrics.json').read_text(encoding='utf-8'))}}
