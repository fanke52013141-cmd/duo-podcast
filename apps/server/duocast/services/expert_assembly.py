"""Assemble verified adopted native cores with one authoritative master; no inference."""
import asyncio
import json
from pathlib import Path
from fastapi import HTTPException
from .expert_candidates import current_project, verified_candidate
from .expert_preview import digest
from .render_inputs import dependency_hash
from .owned_process import spawn_owned


def checked_artifact(artifacts, artifact_id, expected_hash=None):
    entry = artifacts.by_id(artifact_id)
    if (not entry or not Path(entry['path']).is_file()
            or expected_hash is not None and entry['fileHash'] != expected_hash
            or artifacts._hash_file(entry['path']) != entry['fileHash']):
        raise ValueError('已采用媒体缺失或内容改变，请恢复有效版本')
    return entry


def assembly_plan(state, project_id):
    project = current_project(state, project_id)
    selection = state.expert_version_store.read(project_id)
    if not selection['selected']:
        return {'ready':False, 'reason':'请先生成并采用视频窗口', 'missingWindowIds':[], 'selectionRevision':selection['revision']}
    jobs = {id:verified_candidate(state, project_id, id, project) for id in set(selection['selected'].values())}
    plan = next(iter(jobs.values())).input_snapshot['expertSelection']
    count = plan.get('fullWindowCount')
    if type(count) is not int or not 0 < count <= 10000:
        raise HTTPException(422, '旧候选缺少整期窗口身份，请重新生成')
    clock = {key:plan[key] for key in ('sampleRate','totalSamples','totalFrames','fullWindowCount')}
    expected = [f'EW-{i:05d}' for i in range(count)]
    if set(selection['selected'])-set(expected):
        raise HTTPException(422, '采用记录包含整期之外的窗口')
    missing = [id for id in expected if id not in selection['selected']]
    windows = []
    cursor = 0
    for window_id in expected:
        if window_id in missing: continue
        job = jobs[selection['selected'][window_id]]
        source = job.input_snapshot['expertSelection']
        if source['snapshotId'] != selection['snapshotId'] or any(source.get(k) != v for k,v in clock.items()):
            raise HTTPException(409, '采用窗口来自不同整期快照，不能混合合成')
        record = next((w for w in job.result.get('meta',{}).get('windowArtifacts',[]) if w['windowId']==window_id), None)
        spec = next((w['segment'] for w in source['windows'] if w['windowId']==window_id), None)
        if not record or not spec or any(record.get(k)!=spec[k] for k in ('outputStartFrame','outputEndFrame','dependencyHash')):
            raise HTTPException(422, '候选缺少可验证的原生窗口，请重新生成')
        try:
            entry = checked_artifact(state.artifacts, record['artifactId'], record['fileHash'])
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if entry['kind'] != 'video' or entry['dependencyHash'] != record['dependencyHash']:
            raise HTTPException(422, '窗口产物身份不一致')
        if not missing and record['outputStartFrame'] != cursor:
            raise HTTPException(422, '已采用窗口出现缺口或重叠')
        cursor = record['outputEndFrame']
        windows.append({**record, 'sourceJobId':job.id})
    if not missing and cursor != clock['totalFrames']:
        raise HTTPException(422, '已采用窗口未覆盖完整时间轨')
    result = {'ready':not missing, 'reason':f'还缺少 {len(missing)} 个已采用窗口' if missing else '可以合成，无需模型生成',
        'executionProfile':plan.get('executionProfile','expert-full-frame-reference'),
        'selectionRevision':selection['revision'], 'snapshotId':selection['snapshotId'],
        'dependencyHash':dependency_hash(project), 'missingWindowIds':missing, 'windows':windows, **clock}
    result['assemblyId'] = 'EA-'+digest(result)[:24]
    return result


class ExpertAssemblyService:
    capabilities = {'simulated':False}

    def __init__(self, state):
        self.state = state

    @staticmethod
    def result(job,plan,artifact_id,path):
        return {'artifactIds':[artifact_id], 'simulated':False,'meta':{'path':str(path),
            'durationSec':plan['totalSamples']/plan['sampleRate'],'fps':25,'resolution':'1280x720',
            'assemblyId':plan['assemblyId'],'selectionRevision':plan['selectionRevision'],
            'qualityStatus':'needs_review','candidate':True,'dependencyHash':plan['dependencyHash'],
            'modelCallsSubmitted':0,'expertAssemblyJobId':job.id}}

    async def command(self, *args, timeout=300):
        process = await spawn_owned(*map(str,args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, error = await asyncio.wait_for(process.communicate(),timeout)
            if process.returncode:
                raise ValueError('整期合成媒体检查失败：'+error.decode('utf-8','replace')[-300:])
            return out
        finally:
            await process.stop()

    async def run(self, job):
        state = self.state
        plan = job.input_snapshot['assemblyPlan']
        current = await asyncio.to_thread(assembly_plan,state,job.project_id)
        if current.get('assemblyId') != plan['assemblyId'] or not current['ready']:
            raise ValueError('采用版本已改变，请重新检查整期合成计划')
        project = current_project(state,job.project_id)
        master = await asyncio.to_thread(checked_artifact,state.artifacts,project.audio_timeline.master_audio_asset_id)
        folder = state.artifacts.root/'expert-assembly'/job.id
        folder.mkdir(parents=True,exist_ok=True)
        artifact_id='VID-EXPERT-ASSEMBLY-'+job.id
        existing=state.artifacts.by_id(artifact_id)
        if existing:
            await asyncio.to_thread(checked_artifact,state.artifacts,artifact_id)
            if (existing.get('paramsSnapshot')!=job.input_snapshot or Path(existing['path'])!=folder/'final.mp4'
                    or existing['dependencyHash']!=plan['dependencyHash']):
                raise ValueError('已有整期产物身份不一致，拒绝覆盖')
            return self.result(job,plan,artifact_id,existing['path'])
        paths = []
        for item in plan['windows']:
            state.job_manager.checkpoint(job)
            entry = await asyncio.to_thread(checked_artifact,state.artifacts,item['artifactId'],item['fileHash'])
            probe = json.loads(await self.command('ffprobe','-v','error','-count_frames','-show_streams','-of','json',entry['path']))
            video = next(s for s in probe['streams'] if s['codec_type']=='video')
            if int(video['nb_read_frames']) != item['outputEndFrame']-item['outputStartFrame'] or video['avg_frame_rate'] not in {'25/1','25'}:
                raise ValueError('原生窗口帧数或帧率改变')
            paths.append(Path(entry['path']))
        import wave
        with wave.open(master['path'],'rb') as audio:
            if (audio.getframerate(),audio.getnframes(),audio.getnchannels(),audio.getsampwidth()) != (plan['sampleRate'],plan['totalSamples'],1,2):
                raise ValueError('母轨格式或采样时钟与采用计划不符')
        concat = folder/'cores.txt'
        concat.write_text('\n'.join("file '"+p.as_posix().replace("'","'\\''")+"'" for p in paths),encoding='utf-8')
        partial, final = folder/'final.partial.mp4', folder/'final.mp4'
        duration = plan['totalSamples']/plan['sampleRate']
        roi_profile=plan['executionProfile']=='expert-sage-roi-v1'
        video_filter=('scale=1280:720:flags=lanczos,unsharp=5:5:0.45:5:5:0,setsar=1' if roi_profile
                      else 'scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,setsar=1')
        state.job_manager.set_stage(job,'合成已采用窗口与原始配音，不调用模型')
        await self.command('ffmpeg','-v','error','-y','-f','concat','-safe',0,'-i',concat,'-i',master['path'],
            '-map','0:v:0','-map','1:a:0','-vf',video_filter,
            '-frames:v',plan['totalFrames'],'-t',duration,'-r',25,'-c:v','libx264','-crf',16 if roi_profile else 19,
            '-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart',partial)
        info=json.loads(await self.command('ffprobe','-v','error','-count_frames','-show_streams','-of','json',partial))
        video=next(s for s in info['streams'] if s['codec_type']=='video')
        audio=next(s for s in info['streams'] if s['codec_type']=='audio')
        if (int(video['nb_read_frames']) != plan['totalFrames'] or abs(float(audio['duration'])-duration)>.03
                or abs(float(video['duration'])-duration)>.045 or (video['width'],video['height'])!=(1280,720)):
            raise ValueError('整期成片帧数或声音时长不完整')
        await self.command('ffmpeg','-v','error','-xerror','-i',partial,'-f','null','-')
        if (await asyncio.to_thread(assembly_plan,state,job.project_id)).get('assemblyId') != plan['assemblyId']:
            raise ValueError('合成期间采用版本发生变化，未登记输出')
        await asyncio.to_thread(checked_artifact,state.artifacts,master['artifactId'],master['fileHash'])
        state.job_manager.checkpoint(job)
        partial.replace(final)
        final_hash=await asyncio.to_thread(state.artifacts._hash_file,str(final))
        state.artifacts.register(artifact_id,'video',str(final),plan['dependencyHash'],job.input_snapshot,'expert-assembly-v1',file_hash=final_hash)
        return self.result(job,plan,artifact_id,final)
