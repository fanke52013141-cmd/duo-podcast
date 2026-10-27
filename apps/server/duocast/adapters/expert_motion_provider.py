"""Unrouted experimental expert provider with durable, non-resampling task state."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import wave

import httpx

from .expert_motion_video import build_expert_motion_workflow
from ..domain.motion_pack import content_hash
from ..domain.project import Project
from ..services.expert_motion import build_expert_motion_plan
from ..services.motion_render_inputs import build_motion_worker_inputs, camera_edit_plan
from ..services.owned_process import spawn_owned

VERSION = 'expert-motion-reference-provider-v1'


def _atomic_json(path, data, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        return
    fd, name = tempfile.mkstemp(prefix='.expert-', suffix='.partial', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _read_json(path):
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Expert state is missing or oversized')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid expert state')
    return value


def _pcm_hash(path, sample_rate, sample_count):
    with wave.open(str(path), 'rb') as audio:
        if (audio.getframerate(), audio.getnframes(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (
                sample_rate, sample_count, 1, 2, 'NONE'):
            raise ValueError('Expert owned PCM no longer matches its frozen clock')
        digest, remaining = hashlib.sha256(), sample_count
        while remaining:
            count = min(remaining, 65536)
            block = audio.readframes(count)
            if len(block) != count * 2:
                raise ValueError('Expert owned PCM is truncated')
            digest.update(block)
            remaining -= count
    return digest.hexdigest()


class ExpertMotionVideoProvider:
    """Requires server-owned job context; never submits an ambiguous task twice.

    This adapter is deliberately not selected by current project APIs. A real
    GPU acceptance gate is still required before enabling expert motion packs.
    """
    capabilities = {'provider': 'expert-motion-reference', 'simulated': False,
                    'experimental': True, 'productionReady': False, 'motionPackExpert': True}

    def __init__(self, artifacts, motion_store, *, base_url, input_dir, fast_runtime_root,
                 job_manager=None, transport=None, poll_interval=3.0, timeout_sec=3600,
                 engine_preparer=None, engine_fingerprint=None):
        self.artifacts, self.motion_store, self.manager = artifacts, motion_store, job_manager
        self.base_url, self.input_dir = base_url, Path(input_dir).resolve()
        self.runtime = Path(fast_runtime_root).resolve()
        self.transport, self.poll_interval, self.timeout_sec = transport, poll_interval, timeout_sec
        self.engine_preparer = engine_preparer
        if engine_fingerprint is not None and (not isinstance(engine_fingerprint, str)
                or not re.fullmatch(r'[a-f0-9]{64}', engine_fingerprint)):
            raise ValueError('Expert engine fingerprint must be a server-verified SHA256')
        self.engine_fingerprint = engine_fingerprint
        self._locks = {}

    def _source_code_identity(self):
        files = [Path(__file__), Path(__file__).with_name('expert_motion_video.py'),
                 Path(__file__).parents[1] / 'services/expert_motion.py',
                 *(self.runtime / name for name in ('prepare_expert_motion.py', 'motion_worker.py', 'motion_audio_cache.py'))]
        if any(not path.is_file() for path in files):
            raise ValueError('Expert preparation/runtime code is not installed completely')
        packages = self.runtime / 'venv/Lib/site-packages'
        for pattern in ('numpy-*.dist-info/METADATA', 'opencv_python*.dist-info/METADATA'):
            files.extend(packages.glob(pattern))
        if (self.runtime / 'venv/pyvenv.cfg').is_file():
            files.append(self.runtime / 'venv/pyvenv.cfg')
        return {str(path): self.artifacts._hash_file(str(path)) for path in files}

    def _frozen_engine(self, job):
        fingerprint = job.completed_calls.get('expertMotion', {}).get('engineFingerprint') or self.engine_fingerprint
        if not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint):
            raise ValueError('Expert engine fingerprint is unknown; refusing new model inference')
        return fingerprint

    def _artifact(self, artifact_id, kinds):
        entry = self.artifacts.by_id(artifact_id)
        if (not entry or entry.get('kind') not in kinds or not Path(entry['path']).is_file()
                or self.artifacts._hash_file(entry['path']) != entry.get('fileHash')):
            raise ValueError('Expert input artifact is missing or changed')
        return Path(entry['path'])

    def _job(self, req):
        job_id = req.get('jobId')
        if self.manager is None or not isinstance(job_id, str):
            raise ValueError('Expert inference requires a server-owned job context')
        job = self.manager.get(job_id)
        snapshot = {key: value for key, value in req.items() if key != 'jobId'}
        project = snapshot.get('projectSnapshot', {})
        if (job is None or job.kind not in {'renders', 'sample.generate'} or job.owner_type != 'project'
                or job.project_id != project.get('id') or job.input_snapshot != snapshot
                or not project.get('motionPack')):
            raise ValueError('Expert job does not own this frozen project/motion snapshot')
        self.manager.checkpoint(job)
        return job

    def _remember(self, job, request_key, segment_key=None, record=None):
        state = deepcopy(job.completed_calls.get('expertMotion', {}))
        if state and state.get('requestKey') != request_key:
            raise ValueError('Expert job implementation or frozen input changed; restore its original version')
        if not state:
            state = {'requestKey': request_key, 'snapshotHash': content_hash(job.input_snapshot),
                     'implementation': VERSION, 'segments': {}, 'engineFingerprint': self._frozen_engine(job)}
        if segment_key is not None:
            state['segments'][segment_key] = deepcopy(record)
            if record.get('promptId'):
                job.provider_task_id = record['promptId']
        job.completed_calls['expertMotion'] = state
        self.manager._persist(job)

    async def _command(self, *args, timeout=1800):
        process = await spawn_owned(*(str(a) for a in args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
            await process.wait()
            if process.returncode:
                raise RuntimeError('Expert media command failed: ' + stderr.decode('utf-8', 'replace')[-500:])
            return stdout
        finally:
            await process.stop()

    async def _video(self, path, expected_frames):
        raw = await self._command('ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-select_streams', 'v:0', '-count_frames', '-show_entries',
            'stream=width,height,avg_frame_rate,nb_read_frames,duration', '-of', 'json', path, timeout=120)
        stream = json.loads(raw)['streams'][0]
        if (stream.get('avg_frame_rate') != '25/1' or int(stream.get('nb_read_frames', 0)) != expected_frames
                or not 0 < int(stream.get('width', 0)) <= 4096 or not 0 < int(stream.get('height', 0)) <= 2160
                or abs(float(stream.get('duration', 0)) - expected_frames / 25) > .041):
            raise ValueError('Expert model output does not contain its complete requested frame clock')
        await self._command('ffmpeg', '-v', 'error', '-xerror', '-protocol_whitelist', 'file,pipe',
                            '-i', path, '-map', '0:v:0', '-f', 'null', '-', timeout=120)
        return {'width': int(stream['width']), 'height': int(stream['height']),
                'frames': expected_frames, 'fps': 25, 'durationSec': expected_frames / 25}

    def _write_tracks(self, project, req, master, folder):
        timeline = project.audio_timeline
        revision = next((r for r in project.script_revisions if r.id == req['revisionId']), None)
        if not timeline or not revision or timeline.revision_id != revision.id:
            raise ValueError('Expert script/audio revision does not match')
        rate, total = timeline.sample_rate, timeline.sample_count
        bounds = (req.get('rangeStartSec', 0), req.get('rangeEndSec', total / rate))
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds):
            raise ValueError('Invalid expert sample range')
        start, end = round(bounds[0] * rate), min(total, round(bounds[1] * rate))
        if not 0 <= start < end <= total or end - start > rate * 300:
            raise ValueError('Expert range outside authoritative audio')
        with wave.open(str(master), 'rb') as audio:
            if (audio.getframerate(), audio.getnframes(), audio.getnchannels(), audio.getsampwidth()) != (rate, total, 1, 2):
                raise ValueError('Expert master must be complete PCM16 mono')
            audio.setpos(start)
            pcm = audio.readframes(end - start)
        if len(pcm) != 2 * (end - start):
            raise ValueError('Expert master is truncated')
        roles = {role: bytearray(len(pcm)) for role in 'AB'}
        speakers, turns = {turn.id: turn.speaker for turn in revision.turns}, []
        for unit in timeline.units:
            left = max(start, timeline.unit_offsets[unit.id])
            right = min(end, timeline.unit_offsets[unit.id] + unit.sample_count)
            if right <= left:
                continue
            role = speakers[unit.turn_id]
            roles[role][(left-start)*2:(right-start)*2] = pcm[(left-start)*2:(right-start)*2]
            turns.append({'speaker': role, 'startSample': left-start, 'endSample': right-start})
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in {'master': pcm, **roles}.items()}
        if folder is not None:
            folder.mkdir(parents=True, exist_ok=True)
            for name, data in {'master': pcm, **roles}.items():
                target = folder / f'{name}.wav'
                with wave.open(str(target), 'wb') as audio:
                    audio.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
                    audio.writeframes(data)
        return rate, end-start, turns, hashes

    async def _prepare_sources(self, job, folder, request_key, payload):
        target = folder / 'preparation.json'
        _atomic_json(target, payload)
        digest = self.artifacts._hash_file(str(target))
        manifest = Path(payload['outputDir']) / 'prepared_segments.json'
        if manifest.exists():
            result = _read_json(manifest)
            if result.get('requestFileHash') != digest:
                raise ValueError('Frozen expert preparation changed')
            self._verify_prepared(result, Path(payload['outputDir']))
            await self._verify_prepared_media(result, payload)
            return result
        state = job.completed_calls.get('expertMotion', {})
        if any(item.get('submissionIntent') for item in state.get('segments', {}).values()):
            raise ValueError('Prepared expert inputs disappeared after engine submission; refusing to reconstruct them')
        helper = self.runtime / 'prepare_expert_motion.py'
        python = self.runtime / 'venv/Scripts/python.exe'
        if not helper.is_file() or not python.is_file():
            raise ValueError('CPU expert reference preparation runtime is not installed')
        self.manager.checkpoint(job)
        with (folder / 'preparation.log').open('ab') as log:
            process = await spawn_owned(str(python), '-B', str(helper), str(target), cwd=str(self.runtime), stdout=log, stderr=log)
            try:
                await asyncio.wait_for(process.wait(), 1800)
            finally:
                await process.stop()
        if process.returncode:
            raise RuntimeError('Expert CPU source preparation failed; inspect preparation.log')
        result = _read_json(manifest)
        if result.get('requestFileHash') != digest:
            raise ValueError('Expert CPU preparation did not bind its actual input')
        self._verify_prepared(result, Path(payload['outputDir']))
        await self._verify_prepared_media(result, payload)
        return result

    async def _verify_prepared_media(self, result, payload):
        segments = {s['dependencyHash']: s for s in payload['expertPlan']['segments']}
        for prepared in result['preparedSegments']:
            segment = segments.get(prepared.get('segmentDependencyHash'))
            if segment is None:
                raise ValueError('Unexpected prepared expert segment')
            native = await self._video(prepared['files']['referenceVideo']['path'], segment['modelFrames'])
            if native['width'] != prepared['native']['width'] or native['height'] != prepared['native']['height']:
                raise ValueError('Expert prepared native dimensions differ from actual media')
            samples = segment['audioSlice']['modelSampleCount']
            for kind, entry in prepared['files'].items():
                if kind == 'masterAudio' or kind.startswith('audio'):
                    _pcm_hash(entry['path'], payload['sampleRate'], samples)

    def _verify_prepared(self, result, root):
        if result.get('modelInferencePerformed') is not False or result.get('automaticAdoption') is not False:
            raise ValueError('Expert preparation cannot declare inferred/adopted model media')
        for item in result.get('preparedSegments', []):
            for entry in item.get('files', {}).values():
                path = Path(entry.get('path', '')).resolve()
                if (not path.is_relative_to(root.resolve()) or not path.is_file()
                        or self.artifacts._hash_file(str(path)) != entry.get('fileHash')):
                    raise ValueError('Prepared expert input file changed or escaped its owned directory')

    async def render(self, req, progress=None):
        job = self._job(req)
        async with self._locks.setdefault(job.id, asyncio.Lock()):
            from ..services.fast_runtime import runtime_lease
            async with runtime_lease(self.runtime):
                return await self._render(job, req, progress)

    async def _render(self, job, req, progress):
        project = Project.model_validate(req['projectSnapshot'])
        pin = project.motion_pack
        if pin is None or project.audio_timeline is None:
            raise ValueError('Expert motion reference requires an explicitly pinned pack')
        if project.generation_mode != 'standard' or req.get('generationProfile', 'accepted') not in {'accepted', 'expertMotionReference'}:
            raise ValueError('Expert motion provider requires its explicit standard-mode profile')
        if req.get('reuseMotionArtifactId') or req.get('redoFromSec') is not None:
            raise ValueError('Expert motion uses its own verified segment reuse; legacy redo inputs are unsupported')
        frozen_engine = self._frozen_engine(job)
        master = self._artifact(project.audio_timeline.master_audio_asset_id, {'audio', 'mixed'})
        rate, samples, turns, pcm_hashes = self._write_tracks(project, req, master, None)
        assets = await asyncio.to_thread(build_motion_worker_inputs, self.motion_store, self.artifacts,
            pin.id, pin.version, project.shot_style, turns, sample_rate=rate, total_samples=samples,
            seed=str(req.get('seed', 1)))
        edits = camera_edit_plan(assets, turns, rate)
        expert = build_expert_motion_plan(assets, edits, sample_rate=rate, total_samples=samples, seed=req.get('seed', 1))
        template = json.loads(Path(__file__).with_name('multitalk_workflow.json').read_text(encoding='utf-8'))
        helper = self.runtime / 'prepare_expert_motion.py'
        if not helper.is_file():
            raise ValueError('Expert CPU source preparation helper missing')
        tool_identity = {name: hashlib.sha256(await self._command(name, '-version', timeout=15)).hexdigest()
                         for name in ('ffmpeg', 'ffprobe')}
        request_key = content_hash({'implementation': VERSION, 'code': self._source_code_identity(),
            'tools': tool_identity, 'engineFingerprint': frozen_engine,
            'projectId': project.id, 'assets': assets, 'expert': expert,
            'pcm': pcm_hashes, 'template': template})
        self._remember(job, request_key)
        folder = (self.artifacts.root / 'video_expert_motion' / request_key).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        if self._write_tracks(project, req, master, folder / 'audio')[-1] != pcm_hashes:
            raise ValueError('Expert master changed during source freezing')
        payload = {'outputDir': str(folder / 'prepared'), 'motionAssets': assets, 'expertPlan': expert,
            'master': str(folder / 'audio/master.wav'), 'tracks': {r: str(folder / f'audio/{r}.wav') for r in 'AB'},
            'sampleRate': rate, 'sampleCount': samples, 'cameraEditPlan': edits}
        if progress: progress('专家模式：准备已采用动作与整数采样音轨')
        prepared = await self._prepare_sources(job, folder, request_key, payload)
        items = {item['segmentDependencyHash']: item for item in prepared['preparedSegments']}
        if set(items) != {s['dependencyHash'] for s in expert['segments']}:
            raise ValueError('Expert CPU preparation omitted or added segments')
        from ..services.fast_runtime import release_all
        await release_all()
        cores, records = [], []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=60, trust_env=False, transport=self.transport) as client:
            engine_context = {}
            for index, segment in enumerate(expert['segments']):
                self.manager.checkpoint(job)
                if progress: progress(f'专家模式：机位 {segment["cameraId"]}，片段 {index+1}/{len(expert["segments"])}')
                core, record = await self._segment(client, job, request_key, segment, items[segment['dependencyHash']], folder, template, engine_context)
                cores.append(core)
                records.append(record)
        self.manager.checkpoint(job)
        self._artifact(project.audio_timeline.master_audio_asset_id, {'audio', 'mixed'})
        return await self._finish(job, req, request_key, folder, cores, records, expert, samples, rate, pcm_hashes)

    def _copy_inputs(self, prepared):
        self.input_dir.mkdir(parents=True, exist_ok=True)
        names = prepared['inputNames']
        flat = {'anchorImage': names['anchorImage'], 'referenceVideo': names['referenceVideo'],
                'masterAudio': names['masterAudio'],
                **{'audio' + r: value for r, value in names['roleAudio'].items()},
                **{'mask' + r: value for r, value in names['roleMasks'].items()}}
        if set(flat) != set(prepared['files']):
            raise ValueError('Expert prepared filenames differ from actual file manifest')
        for kind, name in flat.items():
            source = Path(prepared['files'][kind]['path'])
            target = self.input_dir / name
            if (source.name != name or Path(name).name != name or target.resolve().parent != self.input_dir
                    or self.artifacts._hash_file(str(source)) != prepared['files'][kind]['fileHash']):
                raise ValueError('Expert owned input name/hash changed')
            if not target.is_file() or self.artifacts._hash_file(str(target)) != prepared['files'][kind]['fileHash']:
                partial = target.with_name(target.name + '.partial')
                shutil.copyfile(source, partial)
                if self.artifacts._hash_file(str(partial)) != prepared['files'][kind]['fileHash']:
                    partial.unlink(missing_ok=True)
                    raise ValueError('Expert engine input changed during copy')
                partial.replace(target)

    async def _segment(self, client, job, request_key, segment, prepared, folder, template, engine_context=None):
        if engine_context is None:
            engine_context = {}
        built = build_expert_motion_workflow(template, segment, prepared['inputNames'],
                                             output_prefix=segment['dependencyHash'])
        workflow, trace = built['workflow'], built['controlTrace']
        workflow_hash = content_hash(workflow)
        input_hashes = {name: entry['fileHash'] for name, entry in prepared['files'].items()}
        segment_key = content_hash({'requestKey': request_key, 'source': segment,
                                   'workflowHash': workflow_hash, 'preparedInputs': input_hashes})
        directory = folder / 'segments' / segment_key
        directory.mkdir(parents=True, exist_ok=True)
        record_path = directory / 'engine.json'
        expected = {'requestKey': request_key, 'segmentKey': segment_key,
                    'workflowHash': workflow_hash, 'inputHashes': input_hashes,
                    'packId': segment['packId'], 'packVersion': segment['packVersion'],
                    'engineFingerprint': self._frozen_engine(job)}
        record = _read_json(record_path) if record_path.exists() else None
        if record is not None:
            if any(record.get(key) != value for key, value in expected.items()):
                raise ValueError('Frozen expert task inputs/workflow differ')
            self._remember(job, request_key, segment_key, record)
            if record.get('state') == 'complete':
                core = directory / 'core.mp4'
                if self.artifacts._hash_file(str(core)) != record.get('coreHash'):
                    raise ValueError('Completed expert media changed; refusing to regenerate')
                await self._video(core, segment['outputEndFrame'] - segment['outputStartFrame'])
                return core, {**record, 'cacheHit': True}
            if record.get('externalTerminalFailure'):
                raise RuntimeError('Expert external task already failed; an explicit new candidate is required')
            if not record.get('promptId'):
                raise RuntimeError('EXPERT_SUBMISSION_UNKNOWN: original POST may have reached engine; refusing to resample')
        else:
            if self.engine_fingerprint is None or self.engine_fingerprint != expected['engineFingerprint']:
                raise ValueError('Current expert engine identity is not verified for a new submission; known IDs may only be queried')
            available = (await client.get('/object_info'))
            available.raise_for_status()
            missing = sorted({node['class_type'] for node in workflow.values()} - set(available.json()))
            if missing:
                raise RuntimeError('EXPERT_WORKFLOW_UNAVAILABLE: ' + ', '.join(missing))
            if self.engine_preparer and not engine_context.get('prepared'):
                await self.engine_preparer(client)
                engine_context['prepared'] = True
            self._copy_inputs(prepared)
            self.manager.checkpoint(job)
            record = {**expected, 'submissionIntent': True, 'state': 'submitting', 'controlTrace': trace}
            try:
                _atomic_json(record_path, record, exclusive=True)
            except FileExistsError as exc:
                raise RuntimeError('Expert submission is already owned by another execution; inspect its saved state') from exc
            self._remember(job, request_key, segment_key, record)
            response = await client.post('/prompt', json={'prompt': workflow, 'client_id': 'expert_' + segment_key})
            response.raise_for_status()
            submitted = response.json()
            prompt_id = submitted.get('prompt_id')
            if (submitted.get('node_errors') or not isinstance(prompt_id, str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', prompt_id)):
                raise RuntimeError('EXPERT_WORKFLOW_REJECTED: engine did not return a valid task identity')
            record.update(promptId=prompt_id, state='pending')
            _atomic_json(record_path, record)
            self._remember(job, request_key, segment_key, record)
        prompt_id = record['promptId']
        if not isinstance(prompt_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', prompt_id):
            raise ValueError('Invalid persisted expert task identity')
        deadline = time.monotonic() + self.timeout_sec
        while time.monotonic() < deadline:
            self.manager.checkpoint(job)
            response = await client.get('/history/' + prompt_id)
            response.raise_for_status()
            history = response.json().get(prompt_id)
            status = (history or {}).get('status', {}).get('status_str')
            if status == 'success':
                return await self._collect(client, job, request_key, segment_key, segment,
                                           history, directory, record_path, record)
            if status in {'error', 'failed'}:
                record.update(state='failed', externalTerminalFailure=True)
                _atomic_json(record_path, record)
                self._remember(job, request_key, segment_key, record)
                raise RuntimeError('EXPERT_ENGINE_FAILED: original task retained, no automatic resampling')
            await asyncio.sleep(self.poll_interval)
        raise TimeoutError('EXPERT_PENDING: original external task retained; resume by its recorded ID')

    async def _collect(self, client, job, request_key, segment_key, segment, history, directory, record_path, record):
        raw = directory / 'model.mp4'
        raw_id = 'VID-EXPERT-SEG-' + segment_key[:32]
        entry = self.artifacts.by_id(raw_id)
        if entry:
            if Path(entry['path']) != raw or entry['fileHash'] != self.artifacts._hash_file(str(raw)):
                raise ValueError('Immutable expert model artifact changed')
        else:
            videos = history.get('outputs', {}).get('229', {}).get('gifs', [])
            selected = next((v for v in videos if isinstance(v, dict) and isinstance(v.get('filename'), str)
                             and v['filename'].lower().endswith('.mp4')), None)
            if (not selected or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,240}', selected['filename'])
                    or '..' in selected['filename'] or selected.get('type') != 'output'):
                raise ValueError('Expert engine did not return a safe real video output')
            subfolder = selected.get('subfolder', '')
            if (not isinstance(subfolder, str) or not re.fullmatch(r'[A-Za-z0-9_/-]*', subfolder)
                    or '..' in subfolder.split('/') or subfolder.startswith('/')):
                raise ValueError('Invalid expert output subfolder')
            partial = directory / 'download.partial.mp4'
            try:
                async with client.stream('GET', '/view', params={'filename': selected['filename'], 'subfolder': subfolder, 'type': 'output'}) as response:
                    response.raise_for_status()
                    size = 0
                    with partial.open('wb') as stream:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 256 * 1024 * 1024:
                                raise ValueError('Expert output exceeds 256 MiB')
                            stream.write(chunk)
                await self._video(partial, segment['modelFrames'])
                partial.replace(raw)
            finally:
                partial.unlink(missing_ok=True)
        native = await self._video(raw, segment['modelFrames'])
        if entry is None:
            entry = self.artifacts.register(raw_id, 'video', str(raw), segment_key,
                {'source': 'expertMotionModelCandidate', 'native': native, 'promptId': record['promptId'],
                 'workflowHash': record['workflowHash'], 'inputHashes': record['inputHashes'],
                 'controlTrace': record['controlTrace'], 'automaticAdoption': False}, VERSION)
        core = directory / 'core.mp4'
        core_id = 'VID-EXPERT-CORE-' + segment_key[:32]
        core_entry = self.artifacts.by_id(core_id)
        core_frames = segment['outputEndFrame'] - segment['outputStartFrame']
        if core_entry:
            if Path(core_entry['path']) != core or core_entry['fileHash'] != self.artifacts._hash_file(str(core)):
                raise ValueError('Immutable expert core artifact changed')
        else:
            partial = directory / 'core.partial.mp4'
            await self._command('ffmpeg', '-v', 'error', '-y', '-i', raw, '-vf',
                f'trim=start_frame={segment["trimStartFrame"]}:end_frame={segment["trimEndFrame"]},setpts=PTS-STARTPTS',
                '-frames:v', core_frames, '-r', 25, '-fps_mode', 'cfr', '-video_track_timescale', 90000,
                '-an', '-c:v', 'libx264', '-crf', 19, '-pix_fmt', 'yuv420p', partial)
            await self._video(partial, core_frames)
            partial.replace(core)
            core_entry = self.artifacts.register(core_id, 'video', str(core), segment_key,
                {'source': 'expertMotionCoreCandidate', 'modelArtifactId': raw_id, 'promptId': record['promptId'],
                 'packId': segment['packId'], 'packVersion': segment['packVersion'],
                 'outputStartFrame': segment['outputStartFrame'], 'outputEndFrame': segment['outputEndFrame'],
                 'native': {**native, 'frames': core_frames, 'durationSec': core_frames / 25},
                 'automaticAdoption': False}, VERSION)
        await self._video(core, core_frames)
        record.update(state='complete', modelArtifactId=raw_id, modelHash=entry['fileHash'],
                      coreArtifactId=core_id, coreHash=core_entry['fileHash'], native=native,
                      actualGraphCompleted=True, actualMotionRetentionVerified=False)
        _atomic_json(record_path, record)
        self._remember(job, request_key, segment_key, record)
        return core, {**record, 'cacheHit': False}

    async def _finish(self, job, req, request_key, folder, cores, records, expert, samples, rate, pcm_hashes):
        if _pcm_hash(folder / 'audio/master.wav', rate, samples) != pcm_hashes['master']:
            raise ValueError('Expert master PCM changed before final export')
        resolution, fps = req.get('resolution', '854x480'), req.get('fps', 25)
        if resolution not in {'854x480', '1280x720', '1920x1080'} or fps not in {25, 30}:
            raise ValueError('Unsupported expert export specification')
        width, height = map(int, resolution.split('x'))
        encode_key = content_hash({'requestKey': request_key, 'coreHashes': [r['coreHash'] for r in records],
                                   'resolution': resolution, 'fps': fps, 'recipe': 'uniform-master-single-aac-v1'})
        output = folder / 'exports' / encode_key
        output.mkdir(parents=True, exist_ok=True)
        final = output / 'final.mp4'
        artifact_id = 'VID-EXPERT-' + encode_key[:32]
        existing = self.artifacts.by_id(artifact_id)
        if existing and (Path(existing['path']) != final or existing['fileHash'] != self.artifacts._hash_file(str(final))):
            raise ValueError('Immutable expert final artifact changed')
        if not existing:
            concat = output / 'cores.txt'
            concat.write_text('\n'.join("file '" + path.as_posix().replace("'", "'\\''") + "'" for path in cores), encoding='utf-8')
            partial = output / 'final.partial.mp4'
            await self._command('ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', 0, '-i', concat,
                '-i', folder / 'audio/master.wav', '-map', '0:v:0', '-map', '1:a:0', '-t', samples / rate,
                '-vf', f'fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},setsar=1',
                '-frames:v', (samples * fps + rate - 1) // rate, '-c:v', 'libx264', '-preset', 'fast',
                '-crf', 19, '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', partial)
            raw = json.loads(await self._command('ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-of', 'json', partial))
            video = next(s for s in raw['streams'] if s['codec_type'] == 'video')
            audio = next(s for s in raw['streams'] if s['codec_type'] == 'audio')
            if (int(video['nb_read_frames']) != (samples * fps + rate - 1) // rate
                    or (int(video['width']), int(video['height'])) != (width, height)
                    or abs(float(video['duration']) - samples / rate) > 1 / fps + .005
                    or abs(float(audio['duration']) - samples / rate) > .03):
                raise ValueError('Expert final media does not match authoritative video/audio clock')
            self.manager.checkpoint(job)
            if _pcm_hash(folder / 'audio/master.wav', rate, samples) != pcm_hashes['master']:
                raise ValueError('Expert master PCM changed during final export')
            await self._command('ffmpeg', '-v', 'error', '-xerror', '-i', partial, '-f', 'null', '-', timeout=120)
            partial.replace(final)
        stats = {'engine': 'ComfyUI/WanVideoV2V+MultiTalk', 'motionSource': 'expert-pinned-motion-reference',
            'generated': sum(not r.get('cacheHit') for r in records), 'cached': sum(bool(r.get('cacheHit')) for r in records),
            'externalPromptIds': [r['promptId'] for r in records], 'sourcePcmHashes': pcm_hashes,
            'frameCount': expert['totalFrames'], 'referenceMotionRetentionVerified': False}
        meta = {'path': str(final), 'durationSec': samples / rate, 'resolution': resolution, 'fps': fps,
                'generationMode': 'standard', 'generationProfile': 'expertMotionReference',
                'shotStyle': req['projectSnapshot'].get('shotStyle', 'twoShot'), 'qualityStatus': 'needs_review',
                'motionPack': {'id': expert['packId'], 'version': expert['packVersion']},
                'generationStats': stats, 'expertPlan': expert, 'automaticAdoption': False}
        if not existing:
            self.artifacts.register(artifact_id, 'video', str(final), req.get('dependencyHash', request_key),
                {**req, 'expertMotionRequestKey': request_key, 'generationStats': stats}, VERSION)
        _atomic_json(output / 'expert_manifest.json', {'meta': meta, 'segmentRecords': records})
        return {'artifactIds': [artifact_id], 'meta': meta}

    def inspect_recovery(self, job):
        """Read-only resume facts; never attach arbitrary tasks or restart a POST."""
        if job.owner_type != 'project' or job.kind not in {'renders', 'sample.generate'}:
            raise ValueError('Not an expert project render job')
        state = job.completed_calls.get('expertMotion', {})
        if state.get('snapshotHash') != content_hash(job.input_snapshot):
            raise ValueError('Expert recovery snapshot differs')
        key = state.get('requestKey', '')
        if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ValueError('Expert recovery request identity missing')
        known, unknown, failed = [], [], []
        for id, record in state.get('segments', {}).items():
            if not isinstance(id, str) or not re.fullmatch(r'[a-f0-9]{64}', id):
                raise ValueError('Invalid expert recovery segment identity')
            # A crash can happen after promptId is atomically saved but before
            # the job checkpoint. Read that exact server-owned record without
            # attaching any client-supplied task or mutating the snapshot.
            saved_path = self.artifacts.root / 'video_expert_motion' / key / 'segments' / id / 'engine.json'
            if saved_path.is_file():
                saved = _read_json(saved_path)
                if any(saved.get(field) != record.get(field) for field in (
                        'requestKey', 'segmentKey', 'workflowHash', 'inputHashes', 'packId', 'packVersion', 'engineFingerprint')):
                    raise ValueError('Expert recovery file differs from frozen job intent')
                record = saved
            if record.get('externalTerminalFailure'):
                failed.append(id)
            elif record.get('promptId'):
                if not isinstance(record['promptId'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', record['promptId']):
                    raise ValueError('Invalid known expert task ID')
                known.append({'segmentKey': id, 'promptId': record['promptId'], 'state': record.get('state')})
            elif record.get('submissionIntent'):
                unknown.append(id)
        return {'requestKey': key, 'knownTasks': known, 'unknownSubmissions': unknown,
                'terminalFailures': failed, 'canResumeQuery': bool(known) and not unknown and not failed,
                'resubmissionAllowed': False, 'engineFingerprint': state.get('engineFingerprint')}
