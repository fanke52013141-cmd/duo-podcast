"""Server adapter for the isolated, offline fast-mode worker."""
import asyncio
import hashlib
import json
import os
import re
import uuid
import wave
from pathlib import Path

def engine_root():
    value = os.environ.get('DUOCAST_FAST_ENGINE')
    return Path(value).resolve() if value else None

def require_engine(pinned_root=None):
    root = Path(pinned_root).resolve() if pinned_root else engine_root()
    required = ['worker.py', 'quality.py', 'render_geometry.py', 'assets/manifest.json', 'venv/Scripts/python.exe', 'assets/stage.png', 'assets/parsing_bisenet.pth',
                'assets/template_A.mp4', 'assets/template_B.mp4', 'assets/face_coordinates.json',
                'models/musetalkV15/unet.pth', 'models/sd-vae/diffusion_pytorch_model.bin', 'models/whisper/pytorch_model.bin']
    if root is None or any(not (root / p).is_file() for p in required):
        raise ValueError('快速模式引擎未安装完整，请检查本机配置')
    if (root / '.runtime-install-pending.json').exists():
        raise ValueError('运行时安装尚未完成，请先回退记录的发布版本')
    return root


def require_motion_engine(pinned_root=None):
    root = Path(pinned_root).resolve() if pinned_root else engine_root()
    required = ['worker.py', 'motion_worker.py', 'motion_audio_cache.py', 'quality.py', 'render_geometry.py',
                'venv/Scripts/python.exe', 'assets/parsing_bisenet.pth',
                'models/musetalkV15/unet.pth', 'models/sd-vae/diffusion_pytorch_model.bin',
                'models/whisper/pytorch_model.bin']
    if root is None or any(not (root / name).is_file() for name in required):
        raise ValueError('动作包口型运行时尚未安装完整，请安装当前版本运行时')
    if (root / '.runtime-install-pending.json').exists():
        raise ValueError('运行时安装尚未完成，请先回退记录的发布版本')
    return root

def template_identity(root):
    files = ['worker.py', 'quality.py', 'render_geometry.py', 'assets/stage.png', 'assets/manifest.json',
             'assets/template_A.mp4', 'assets/template_B.mp4', 'assets/face_coordinates.json']
    hashes = {}
    for name in files:
        digest = hashlib.sha256()
        with (root/name).open('rb') as source:
            for chunk in iter(lambda: source.read(1024*1024), b''):
                digest.update(chunk)
        hashes[name] = digest.hexdigest()
    version = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {'id': 'studio-' + version[:12], 'version': version, 'runtimeRoot': str(root), 'fileHashes': hashes}

def project_engine(project):
    if project.motion_pack is not None:
        return require_motion_engine()
    pinned = project.fast_template
    root = require_engine(pinned.get('runtimeRoot'))
    if pinned and template_identity(root)['version'] != pinned.get('version'):
        raise ValueError('本期模板或 worker 已改变；请恢复原版本运行目录后重试，不能静默更换人物')
    return root

def template_variant(artifacts, runtime_root=None):
    root = require_engine(runtime_root)
    path = root / 'assets/stage.png'
    digest = artifacts._hash_file(str(path))
    asset_id = 'IMG-FAST-' + digest[:20]
    if not artifacts.by_id(asset_id):
        artifacts.register(asset_id, 'image', str(path), digest, {'template': 'fast-studio-v1'}, 'musetalk-template-v1')
    return {'id': 'FAST-STUDIO-V1', 'masterImage': {'artifactId': asset_id, 'path': str(path), 'fileHash': digest},
            'aspect': 'landscape', 'mode': 'twoShot'}

def validate_fast_options(project, payload, pinned_root=None):
    if project.generation_mode != 'fast': return
    root = require_motion_engine(pinned_root) if project.motion_pack is not None else project_engine(project)
    if project.motion_pack is not None:
        if payload.get('generationProfile', 'accepted') != 'accepted' or payload.get('reuseMotionArtifactId') or payload.get('redoFromSec') is not None:
            raise ValueError('动作包快速模式使用已采用素材，请使用标准生成配置')
        return
    visual_id = payload.get('visualVariantId') or project.selected_visual_variant_id
    visual = next((v for v in project.visual_variants if v.id == visual_id), None)
    expected = hashlib.sha256((root/'assets/stage.png').read_bytes()).hexdigest()
    if not visual or visual.mode != 'twoShot' or visual.master_image.file_hash != expected or project.aspect != 'landscape':
        raise ValueError('快速模式请使用已验证的横屏双人演播室模板')
    if payload.get('generationProfile', 'accepted') != 'accepted' or payload.get('reuseMotionArtifactId') or payload.get('redoFromSec') is not None:
        raise ValueError('快速模式直接按当前配音更新口型，请使用标准生成配置')

class ModeVideoProvider:
    def __init__(self, standard, artifacts, motion_store=None):
        self.standard = standard
        self.fast = MuseTalkVideoProvider(artifacts, motion_store)
        self.capabilities = {**standard.capabilities, 'generationModes': ['standard', 'fast']}

    async def render(self, req, progress=None):
        provider = self.fast if req['projectSnapshot'].get('generationMode') == 'fast' else self.standard
        return await provider.render(req, progress)

class MuseTalkVideoProvider:
    def __init__(self, artifacts, motion_store=None):
        self.artifacts = artifacts
        self.motion_store = motion_store
        self._runtimes = {}

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
        frozen = req.get('fastRuntime')
        if project.motion_pack is not None:
            if (not isinstance(frozen, dict) or set(frozen) != {'root', 'identity', 'protocol'}
                    or frozen.get('protocol') != 'motion-runtime-v1' or not isinstance(frozen.get('root'), str)
                    or not Path(frozen['root']).is_absolute() or not isinstance(frozen.get('identity'), str)
                    or not re.fullmatch('[a-f0-9]{64}', frozen['identity'])):
                raise ValueError('本任务没有有效的固定运行时身份，请重新提交样片或生成任务')
            root = require_motion_engine(frozen['root'])
        else:
            root = project_engine(project)
        validate_fast_options(project, req, pinned_root=str(root))
        timeline = req['projectSnapshot']['audioTimeline']
        visual_id = req.get('visualVariantId') or project.selected_visual_variant_id
        visual = next((v for v in project.visual_variants if v.id == visual_id), None)
        if project.motion_pack is None:
            if visual is None:
                raise RuntimeError('视觉底图缺失')
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
        sample_turns = []
        for unit in timeline['units']:
            a = max(start, timeline['unitOffsets'][unit['id']])
            b = min(end, timeline['unitOffsets'][unit['id']] + unit['sampleCount'])
            if b <= a: continue
            speaker = speakers[unit['turnId']]
            roles[speaker][(a-start)*2:(b-start)*2] = pcm[(a-start)*2:(b-start)*2]
            turns.append({'speaker':speaker,'start':(a-start)/rate,'end':(b-start)/rate})
            sample_turns.append({'speaker': speaker, 'startSample': a-start, 'endSample': b-start})
        for name, data in {'master':pcm, **roles}.items():
            with wave.open(str(folder/f'{name}.wav'),'wb') as audio:
                audio.setparams((1,2,rate,0,'NONE','not compressed'))
                audio.writeframes(data)
        worker_req = {'outputDir':str(folder), 'master':str(folder/'master.wav'), 'tracks':{s:str(folder/f'{s}.wav') for s in roles},
            'turns':turns, 'sampleRate':rate, 'sampleCount':end-start, 'seed':req.get('seed',42),
            'resolution':req.get('resolution','1920x1080'), 'fps':req.get('fps',25), 'fastQuality':req.get('fastQuality','original')}
        if frozen is not None and project.motion_pack is not None:
            worker_req['expectedRuntimeIdentity'] = frozen['identity']
        if project.motion_pack is not None:
            if self.motion_store is None:
                raise RuntimeError('动作包存储未装配')
            from ..services.motion_render_inputs import build_motion_worker_inputs, camera_edit_plan
            worker_req['motionAssets'] = await asyncio.to_thread(
                build_motion_worker_inputs, self.motion_store, self.artifacts,
                project.motion_pack.id, project.motion_pack.version, project.shot_style, sample_turns,
                sample_rate=rate, total_samples=end-start, seed=str(req.get('seed', 42)))
            cameras = camera_edit_plan(worker_req['motionAssets'], sample_turns, rate)
            worker_req['cameraEditPlan'] = cameras
        else:
            cameras = camera_plan(req['projectSnapshot'],req.get('cameraMode', project.camera_mode))
            worker_req['cameraPlan'] = [{**shot, 'start':shot['start']-start/rate, 'end':shot['end']-start/rate,
                'crop':crop_region(visual.person_regions[shot['camera']]) if shot['camera'] in ('A','B') else None}
                for shot in clipped_camera_plan(cameras,start/rate,end/rate)]
        request_file = folder/'request.json'
        request_file.write_text(json.dumps(worker_req),encoding='utf-8')
        if progress: progress('快速模式：更新双人口型')
        runtime_stats = None
        if project.motion_pack is not None and os.environ.get('DUOCAST_FAST_RESIDENT', '1') == '1' and (root/'resident_worker.py').is_file():
            from ..services.fast_runtime import FastRuntimeManager
            if str(root) not in self._runtimes:
                self._runtimes[str(root)] = FastRuntimeManager(root)
            runtime = self._runtimes[str(root)]
            runtime_stats = await runtime.render(request_file)
        else:
            from ..services.fast_runtime import release_all, runtime_lease
            await release_all()
            async with runtime_lease(root):
                if project.motion_pack is not None:
                    from ..services.fast_runtime import runtime_identity
                    if await asyncio.to_thread(runtime_identity, root) != frozen['identity']:
                        raise ValueError('任务排队后运行时已改变，请恢复原版本或重新提交')
                else:
                    project_engine(project)
                await self._one_shot(root, folder, request_file)
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
            'generationStats':{**json.loads((folder/'metrics.json').read_text(encoding='utf-8')),
                               **({'runtime': runtime_stats} if runtime_stats is not None else {})}}}

    async def _one_shot(self, root, folder, request_file):
        from ..services.owned_process import spawn_owned
        with (folder/'worker.log').open('wb') as log:
            process = await spawn_owned(str(root/'venv/Scripts/python.exe'), '-B', str(root/'worker.py'), str(request_file),
                cwd=str(root), stdout=log, stderr=log)
            try:
                await asyncio.wait_for(process.wait(), timeout=1800)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                await process.stop()
                raise
        if process.returncode:
            raise RuntimeError(f'快速模式生成失败，详情见 {folder / "worker.log"}')
