"""Explicit local-API acceptance stages; default plan performs no HTTP/model calls.

Keep config/state under .review: they may contain authored text and local inputs.
Automatic checks and decodable media never create human approval or full success.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from run_motion_acceptance import compare_audio, inspect_media, input_record

MAX_JSON = 16 * 1024 * 1024
MAX_MEDIA = 512 * 1024 * 1024
TERMINAL = {'succeeded', 'failed', 'cancelled', 'unknown', 'paused', 'waiting_confirmation'}
STYLE = {'twoShot', 'overShoulder', 'mixed'}


class AcceptanceStateBusyError(RuntimeError):
    pass


@contextmanager
def state_lock(state_path):
    """Hold one OS lock across CLI state loading, HTTP calls and atomic saves.

    The lock file is deliberately retained; only the live OS lock means busy.
    A crashed process releases ownership without inventing a new submission.
    """
    state_path = Path(state_path).resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    path = state_path.with_name(state_path.name + '.lock')
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
        raise ValueError('Acceptance state lock must not be linked')
    with path.open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AcceptanceStateBusyError('Another acceptance CLI owns this state; no HTTP calls or token changes were made') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.api-evidence-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def local_base(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
        raise ValueError('Use a local http://127.0.0.1:PORT API, without credentials or path')
    return value.rstrip('/')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('API redirect refused; no local input may be forwarded elsewhere')


class LocalAPI:
    def __init__(self, base_url, timeout=180):
        self.base = local_base(base_url)
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, *, payload=None, raw=None):
        if not path.startswith('/api/') or path.startswith('//') or '://' in path:
            raise ValueError('Only explicit local API paths are accepted')
        body = raw if raw is not None else json_bytes(payload) if payload is not None else None
        headers = {'Content-Type': 'application/octet-stream' if raw is not None else 'application/json'}
        try:
            with self.opener.open(Request(self.base + path, data=body, headers=headers, method=method), timeout=self.timeout) as response:
                content = response.read(MAX_JSON + 1)
                if len(content) > MAX_JSON:
                    raise ValueError('API JSON exceeds evidence limit')
                return response.status, json.loads(content)
        except HTTPError as exc:
            raise ValueError(f'API rejected {method} {path}: HTTP {exc.code}') from exc

    def download(self, artifact_id, target):
        if target.exists():
            raise ValueError('Downloaded evidence must not overwrite an existing file')
        path = '/api/artifacts/' + quote(artifact_id, safe='') + '/file'
        size = 0
        try:
            with self.opener.open(self.base + path, timeout=self.timeout) as response, target.open('xb') as output:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_MEDIA:
                        raise ValueError('Artifact download exceeds 512MiB evidence limit')
                    output.write(block)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return input_record(target)


class AcceptanceRun:
    def __init__(self, config, config_dir, state_path, base_url, *, api=None):
        self.config, self.config_dir = config, Path(config_dir).resolve()
        self.path = Path(state_path).resolve()
        self.base = local_base(base_url)
        self.api = api or LocalAPI(self.base)
        config_hash = digest(json_bytes(config))
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if self.state.get('configHash') != config_hash or self.state.get('baseUrl') != self.base:
                raise ValueError('State belongs to a different frozen config or API; use a new state file')
        else:
            self.state = {'formatVersion': 1, 'configHash': config_hash, 'baseUrl': self.base,
                          'operations': {}, 'jobs': {}, 'media': {}, 'pending': [],
                          'fullAcceptancePassed': False,
                          'scope': 'Explicit API observations; human quality and complete T01-T12 acceptance remain separate'}

    def save(self):
        atomic_json(self.path, self.state)

    def file(self, name):
        if not isinstance(name, str) or not name:
            raise ValueError('Explicit local input file is required')
        path = Path(name)
        return (path if path.is_absolute() else self.config_dir / path).resolve(strict=True)

    def uploaded_bytes(self, path, limit):
        if not path.is_file() or not 0 < path.stat().st_size <= limit:
            raise ValueError('Local upload is empty or exceeds its bounded size')
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Local upload changed or exceeds its bounded size')
        return data

    def done(self, key):
        return self.state['operations'].get(key, {}).get('status') == 'done'

    def get(self, path):
        status, result = self.api.request('GET', path)
        if status != 200:
            raise ValueError('Unexpected API read status')
        return result

    def once(self, key, method, path, payload=None, raw=None):
        request_hash = digest(raw if raw is not None else json_bytes(payload))
        operation = self.state['operations'].get(key)
        if operation:
            if operation['requestHash'] != request_hash or operation['path'] != path or operation['method'] != method:
                raise ValueError('Frozen operation input changed: ' + key)
            if operation['status'] == 'done':
                return operation['response']
            raise ValueError('Submission outcome is unresolved; inspect saved evidence before any retry: ' + key)
        operation = {'method': method, 'path': path, 'requestHash': request_hash, 'status': 'intent'}
        if raw is None:
            operation['payload'] = payload
        self.state['operations'][key] = operation
        self.save()  # Intent reaches disk before a possibly expensive or non-idempotent call.
        try:
            status, response = self.api.request(method, path, payload=payload, raw=raw)
        except BaseException as exc:
            operation.update(status='unresolved', errorType=type(exc).__name__)
            self.save()
            raise
        operation.update(status='done', httpStatus=status, response=response)
        self.save()
        return response

    def pending(self, reason):
        self.state['pending'] = [reason]
        self.save()
        return {'pending': reason}

    def pack(self):
        if not self.state.get('packId'):
            raise ValueError('Run import first')
        return self.get('/api/motion-packs/' + quote(self.state['packId'], safe=''))

    def project(self):
        if not self.state.get('projectId'):
            raise ValueError('Run project first')
        return self.get('/api/projects/' + quote(self.state['projectId'], safe=''))

    def plan(self):
        files = []
        for character in self.config.get('characters', {}).values():
            files.append(input_record(self.file(character['referenceImageFile'])))
        for camera in self.config.get('cameras', []):
            files.append(input_record(self.file(camera['masterImageFile'])))
        for clip in self.config.get('clips', []):
            files.append(input_record(self.file(clip['videoFile'])))
            if clip.get('coordinatesFile'):
                files.append(input_record(self.file(clip['coordinatesFile'])))
        return {'scope': 'plan_only_no_HTTP_or_model_calls', 'inputs': files,
                'stages': ['import', 'coordinates', 'check', 'review', 'project', 'script', 'confirm', 'voice', 'sample', 'render', 'verify'],
                'optionalModelStages': ['prepare', 'resume'], 'humanApprovalNeverInferred': True}

    def import_assets(self):
        draft = {'name': self.config['name'], 'description': self.config.get('description', ''),
                 'characters': {}, 'cameras': [], 'clips': []}
        for role, character in self.config.get('characters', {}).items():
            path = self.file(character['referenceImageFile'])
            ref = self.once('image-character-' + role, 'POST', '/api/motion-media/images', raw=self.uploaded_bytes(path, 10 * 1024 * 1024))
            draft['characters'][role] = {k: v for k, v in character.items()
                if k in {'characterId', 'name', 'objectType', 'motionNotes'}} | {
                    'referenceImage': {'artifactId': ref['artifactId'], 'fileHash': ref['fileHash']}}
        for camera in self.config.get('cameras', []):
            path = self.file(camera['masterImageFile'])
            ref = self.once('image-camera-' + camera['id'], 'POST', '/api/motion-media/images', raw=self.uploaded_bytes(path, 10 * 1024 * 1024))
            draft['cameras'].append({k: v for k, v in camera.items()
                if k in {'id', 'mode', 'subjectSpeaker', 'foregroundSpeaker', 'subjectRegion'}} | {
                    'masterImage': {'artifactId': ref['artifactId'], 'fileHash': ref['fileHash']}})
        for clip in self.config.get('clips', []):
            path = self.file(clip['videoFile'])
            uploaded = self.once('video-' + clip['id'], 'POST', '/api/motion-media?extension=' + path.suffix.lstrip('.').lower(), raw=self.uploaded_bytes(path, 200 * 1024 * 1024))
            native = uploaded['native']
            if not float(native['fps']).is_integer():
                raise ValueError('Motion pack requires an integer source fps')
            draft['clips'].append({k: v for k, v in clip.items()
                if k in {'id', 'role', 'cameraId', 'state', 'usableStartFrame', 'usableEndFrame', 'bodyRegion', 'provenance'}} | {
                    'media': {'artifactId': uploaded['artifactId'], 'fileHash': uploaded['fileHash']},
                    'nativeWidth': native['width'], 'nativeHeight': native['height'], 'fps': int(native['fps']),
                    'frameCount': native['frames'], 'usableEndFrame': clip.get('usableEndFrame', native['frames'])})
        pack = self.once('create-pack', 'POST', '/api/motion-packs', draft)
        self.state['packId'] = pack['id']
        self.save()
        return {'packId': pack['id'], 'version': pack['version'], 'adopted': False}

    def coordinates(self):
        for clip in self.config.get('clips', []):
            if not clip.get('coordinatesFile'):
                continue
            if self.done('coordinates-' + clip['id']):
                continue
            pack = self.pack()
            boxes = json.loads(self.file(clip['coordinatesFile']).read_text(encoding='utf-8'))
            if isinstance(boxes, dict):
                boxes = boxes.get('boxes')
            self.once('coordinates-' + clip['id'], 'POST', f'/api/motion-packs/{quote(pack["id"], safe="")}/clips/{quote(clip["id"], safe="")}/coordinates',
                      {'expectedRevision': pack['revision'], 'boxes': boxes})
        return self.pending('Coordinates bound; actual check and explicit human review still required')

    def check(self):
        for clip in self.config.get('clips', []):
            if self.done('inspect-' + clip['id']):
                continue
            if not clip.get('inspection'):
                raise ValueError('Explicit body/exclusion/background inspection regions required for ' + clip['id'])
            pack = self.pack()
            self.once('inspect-' + clip['id'], 'POST', f'/api/motion-packs/{quote(pack["id"], safe="")}/clips/{quote(clip["id"], safe="")}/inspect',
                      {'expectedRevision': pack['revision'], **clip['inspection']})
        return self.pending('Read server checks and watch full selected intervals before explicit human review')

    def review(self, reviews, apply=False):
        if not apply or not reviews.get('clips'):
            return self.pending('Explicit --apply-reviews and independently supplied per-clip conclusions are required')
        for review in reviews['clips']:
            if (set(review) != {'clipId', 'accepted', 'reason'} or type(review['accepted']) is not bool
                    or not isinstance(review['reason'], str) or not review['reason'].strip()):
                raise ValueError('Each review needs clipId, strict accepted boolean and a nonempty human reason')
            if self.done('human-review-' + review['clipId']):
                stored = self.state['operations']['human-review-' + review['clipId']]['response']
                previous = stored.get('reviews', {}).get(review['clipId'], {})
                if previous.get('accepted') != review['accepted'] or previous.get('reason') != review['reason'].strip():
                    raise ValueError('Previously applied human conclusion differs; use a new explicit review operation')
                continue
            pack = self.pack()
            clip_id = review['clipId']
            self.once('human-review-' + clip_id, 'POST', f'/api/motion-packs/{quote(pack["id"], safe="")}/clips/{quote(clip_id, safe="")}/review',
                      {'expectedRevision': pack['revision'], 'accepted': review['accepted'], 'reason': review['reason']})
        self.state['pending'] = []
        self.save()
        return {'humanConclusionsApplied': len(reviews['clips']), 'fullAcceptancePassed': False}

    def require_models(self, allowed):
        if not allowed:
            raise ValueError('This explicit stage requires --allow-model-calls; default planning never starts inference')

    def wait_job(self, key, seconds=0):
        deadline = time.monotonic() + seconds
        while True:
            job = self.get('/api/jobs/' + quote(self.state['jobs'][key]['id'], safe=''))
            self.state['jobs'][key].update(status=job['status'], result=job.get('result'), providerTaskId=job.get('providerTaskId'))
            self.save()
            if job['status'] in TERMINAL or time.monotonic() >= deadline:
                return job
            time.sleep(min(2, max(0, deadline-time.monotonic())))

    def submit_job(self, key, path, payload, wait_seconds=0):
        if key in self.state['jobs']:
            return self.wait_job(key, wait_seconds)
        # Token is deterministic and saved within the intent before POST.
        token = 'acceptance-' + digest((self.state['configHash'] + ':' + key).encode())[:32]
        if self.done('submit-' + key):
            response = self.state['operations']['submit-' + key]['response']
        else:
            response = self.once('submit-' + key, 'POST', path, {**payload, 'clientToken': token})
        self.state['jobs'][key] = {'id': response.get('jobId') or response['id'], 'clientToken': token}
        self.save()
        return self.wait_job(key, wait_seconds)

    def prepare(self, allowed=False, wait_seconds=0):
        self.require_models(allowed)
        result = []
        for index, preparation in enumerate(self.config.get('preparation', [])):
            pack = self.pack()
            result.append(self.submit_job('prepare-' + str(index), f'/api/motion-packs/{quote(pack["id"], safe="")}/prepare',
                {**preparation, 'expectedRevision': pack['revision']}, wait_seconds))
        return {'jobs': [{'id': j['id'], 'status': j['status']} for j in result], 'adopted': False}

    def resume(self, key, allowed=False, wait_seconds=0):
        self.require_models(allowed)
        job = self.wait_job(key)
        if not key.startswith('prepare-') or not job.get('providerTaskId'):
            raise ValueError('Only an already known external preparation task may be resumed')
        pack = self.pack()
        self.api.request('POST', f'/api/motion-packs/{quote(pack["id"], safe="")}/prepare/{quote(job["id"], safe="")}/resume', payload={})
        return self.wait_job(key, wait_seconds)

    def reconcile(self, key):
        """Recover an accepted server job using read-only token/owner evidence."""
        operation = self.state['operations'].get('submit-' + str(key))
        kinds = {'script': 'script.generate', 'voice': 'tts.synthesize', 'sample': 'sample.generate', 'render': 'renders'}
        preparing = isinstance(key, str) and key.startswith('prepare-')
        kind = 'motion.prepare' if preparing else kinds.get(key)
        if not operation or not kind:
            raise ValueError('Reconcile needs an existing frozen submit operation and its job key')
        token = 'acceptance-' + digest((self.state['configHash'] + ':' + key).encode())[:32]
        owner = self.state.get('packId' if preparing else 'projectId')
        if not owner:
            raise ValueError('Original task owner is missing')
        query = {'ownerType': 'motionPack', 'ownerId': owner} if preparing else {'projectId': owner}
        jobs = self.get('/api/jobs?' + urlencode(query))
        matches = [job for job in jobs if job.get('clientToken') == token and job.get('kind') == kind
            and ((job.get('ownerType') == 'motionPack' and job.get('ownerId') == owner) if preparing else job.get('projectId') == owner)]
        if len(matches) != 1:
            return self.pending('Original server task was not uniquely found; submission remains unresolved and will not be repeated')
        job = matches[0]
        frozen = job.get('inputSnapshot', {})
        if preparing:
            frozen = frozen.get('request', {})
        for field, expected in (operation.get('payload') or {}).items():
            if field != 'clientToken' and field in frozen and frozen[field] != expected:
                raise ValueError('Matched task snapshot differs from the original frozen submission')
        operation.update(status='done', response={'jobId': job['id']}, httpStatus=None,
                         reconciledBy='GET jobs: exact clientToken/kind/owner', originalOutcome=operation['status'])
        self.state['jobs'][key] = {'id': job['id'], 'clientToken': token, 'status': job['status'],
                                  'providerTaskId': job.get('providerTaskId')}
        self.state['pending'] = []
        self.save()
        return {'reconciledJobId': job['id'], 'status': job['status'], 'newSubmission': False}

    def create_project(self):
        if not self.state.get('projectId') and self.done('create-project'):
            original = self.state['operations']['create-project']['response']
            self.state.update(projectId=original['id'], pin=original['motionPack'], shotStyle=original['shotStyle'])
            self.save()
        if self.state.get('projectId'):
            project = self.project()
            if project.get('motionPack') != self.state.get('pin') or project.get('shotStyle') != self.state.get('shotStyle'):
                raise ValueError('Existing project binding changed; no automatic repinning')
            return {'projectId': project['id'], 'pin': project['motionPack'], 'shotStyle': project['shotStyle']}
        pack = self.pack()
        style = self.config.get('shotStyle', 'twoShot')
        if style not in STYLE:
            raise ValueError('Invalid shotStyle')
        readiness = self.get(f'/api/motion-packs/{quote(pack["id"], safe="")}/readiness?' + urlencode({'style': style, 'version': pack['version']}))
        if not readiness['ready']:
            return self.pending('Pinned pack is not ready: ' + ','.join(readiness['missing']))
        project = self.once('create-project', 'POST', '/api/projects', {
            'id': self.config.get('projectId', 'motion-acceptance-' + self.state['configHash'][:12]),
            'title': self.config.get('projectTitle', '动作工作流验收'), 'generationMode': 'fast', 'aspect': 'landscape',
            'motionPack': {'id': pack['id'], 'version': pack['version']}, 'shotStyle': style})
        self.state.update(projectId=project['id'], pin=project['motionPack'], shotStyle=project['shotStyle'])
        self.save()
        return {'projectId': project['id'], 'pin': project['motionPack'], 'shotStyle': project['shotStyle']}

    @staticmethod
    def confirmed(project, kind):
        return any(a.get('kind') == kind and a.get('decision') == 'accepted' and not a.get('revokedAt')
                   and a.get('inputRevisionId') == project.get('currentDraftRevision') for a in project.get('approvals', []))

    def generate(self, stage, allowed=False, wait_seconds=0):
        self.require_models(allowed)
        project = self.project()
        prefix = '/api/projects/' + quote(project['id'], safe='')
        if stage == 'script':
            text = self.config.get('scriptText')
            if not isinstance(text, str) or not text.strip():
                raise ValueError('Explicit authored scriptText required')
            return self.submit_job('script', prefix + '/script/generate', {'sourceInput': {'kind': 'script', 'content': text}}, wait_seconds)
        gate = {'voice': 'script', 'sample': 'voice', 'render': 'sample'}[stage]
        if not self.confirmed(project, gate):
            return self.pending('Explicit current ' + gate + ' confirmation required before ' + stage)
        if project.get('motionPack') != self.state.get('pin') or project.get('shotStyle') != self.state.get('shotStyle'):
            raise ValueError('Project pin/style changed after acceptance setup')
        if stage == 'voice':
            payload = {'revisionId': project['currentDraftRevision'], **self.config.get('voiceOptions', {})}
            path = prefix + '/voice/synthesize'
        else:
            payload = {'revisionId': project['currentDraftRevision'], **self.config.get('renderOptions', {})}
            path = prefix + ('/visual/sample' if stage == 'sample' else '/render/generate')
        return self.submit_job(stage, path, payload, wait_seconds)

    def confirm(self, reviews, kind, apply=False):
        conclusions = [r for r in reviews.get('project', []) if r.get('kind') == kind]
        if not apply or len(conclusions) != 1 or not str(conclusions[0].get('note', '')).strip():
            return self.pending('Explicit --apply-reviews and one authored project confirmation note required')
        project = self.project()
        if self.confirmed(project, kind):
            return {'alreadyConfirmed': kind}
        conclusion = conclusions[0]
        spec = {}
        if kind not in {'script', 'voice', 'sample'}:
            raise ValueError('Confirmation kind must be script, voice or sample')
        if kind not in self.state['jobs']:
            return self.pending('No submitted ' + kind + ' task to confirm')
        job = self.wait_job(kind)
        if job['status'] != 'succeeded':
            return self.pending('Current ' + kind + ' task has not succeeded')
        actual_revision = ((job.get('result') or {}).get('meta') or {}).get('revisionId') if kind == 'script' else (job.get('inputSnapshot') or {}).get('revisionId')
        if job.get('projectId') != project['id'] or actual_revision != project.get('currentDraftRevision'):
            return self.pending('Task does not prove the current project/script revision; inspect or regenerate before confirmation')
        if kind in {'voice', 'sample'}:
            result = job.get('result') or {}
            if result.get('simulated') is not False or not result.get('artifactIds'):
                return self.pending('Completed real ' + kind + ' media required; simulated or unknown media cannot be accepted')
        if kind == 'sample':
            spec = {'sampleJobId': job['id'], 'sampleArtifactId': result['artifactIds'][0], 'checks': conclusion.get('checks', [])}
        entry = {'kind': kind, 'inputRevisionId': project['currentDraftRevision'], 'decision': 'accepted',
                 'note': conclusion['note'], 'spec': spec}
        result = self.once('confirm-' + kind, 'PATCH', '/api/projects/' + quote(project['id'], safe=''),
            {'expectedRevision': project['revision'], 'approvals': project.get('approvals', []) + [entry]})
        return {'confirmedKind': kind, 'projectRevision': result['revision'], 'fullAcceptancePassed': False}

    def verify(self, compare_state=None):
        output = self.path.parent / (self.path.stem + '-media')
        output.mkdir(exist_ok=True)
        verified = []
        for key in ('sample', 'render'):
            if key not in self.state['jobs']:
                continue
            job = self.wait_job(key)
            result = job.get('result') or {}
            if job['status'] != 'succeeded' or result.get('simulated') is not False or not result.get('artifactIds'):
                self.state['media'][key] = {'passed': False, 'reason': 'not_completed_real_media'}
                continue
            snapshot = job.get('inputSnapshot') or {}
            frozen_project = snapshot.get('projectSnapshot') or {}
            if (job.get('projectId') != self.state.get('projectId')
                    or job.get('kind') != ('sample.generate' if key == 'sample' else 'renders')
                    or frozen_project.get('motionPack') != self.state.get('pin')
                    or frozen_project.get('shotStyle') != self.state.get('shotStyle')):
                raise ValueError('Completed task does not belong to the frozen project/pin/style')
            artifact_id = result['artifactIds'][0]
            manifest = self.get('/api/artifacts')
            registered = next((entry for entry in manifest.get('artifacts', []) if entry.get('artifactId') == artifact_id), None)
            if not registered or registered.get('kind') != 'video' or not registered.get('fileHash'):
                raise ValueError('Successful task must reference an actually registered video')
            target = output / (key + '-' + digest(artifact_id.encode())[:16] + '.mp4')
            previous = self.state['media'].get(key)
            if target.exists():
                if not previous or previous.get('artifactId') != artifact_id or input_record(target)['sha256'] != previous.get('input', {}).get('sha256'):
                    raise ValueError('Previously downloaded evidence changed; refusing overwrite')
            else:
                self.api.download(artifact_id, target)
            if input_record(target)['sha256'] != registered['fileHash']:
                raise ValueError('Downloaded video differs from registered artifact hash')
            meta = result.get('meta') or {}
            resolution = meta.get('resolution')
            size = resolution.split('x') if isinstance(resolution, str) and resolution.count('x') == 1 else None
            media = inspect_media(target, duration=meta.get('durationSec'), fps=meta.get('fps'),
                                  width=int(size[0]) if size else None, height=int(size[1]) if size else None)
            self.state['media'][key] = {**media, 'artifactId': artifact_id, 'jobId': job['id'],
                                       'qualityApproved': False, 'pin': self.state.get('pin')}
            verified.append(key)
        # Sample and full render are comparable only when their actual audio
        # ranges have identical duration. Never assert equal for a short preview.
        if all(k in verified for k in ('sample', 'render')):
            sample, rendered = self.state['media']['sample'], self.state['media']['render']
            sample_duration = self.state['jobs']['sample']['result']['meta']['durationSec']
            render_duration = self.state['jobs']['render']['result']['meta']['durationSec']
            self.state['pairedAudio'] = (compare_audio(Path(sample['input']['path']), Path(rendered['input']['path']))
                if abs(sample_duration-render_duration) <= 1e-6 else {'compared': False, 'reason': 'sample_and_render_ranges_differ'})
        if compare_state is not None:
            other = json.loads(Path(compare_state).read_text(encoding='utf-8'))
            first, second = self.state['media'].get('render'), other.get('media', {}).get('render')
            if not first or not second or not first.get('passed') or not second.get('passed'):
                raise ValueError('Both render ledgers must contain verified real media')
            for record in (first, second):
                if input_record(Path(record['input']['path']))['sha256'] != record['input']['sha256']:
                    raise ValueError('Comparison evidence content changed')
            self.state['crossRenderAudio'] = compare_audio(Path(first['input']['path']), Path(second['input']['path']))
        self.save()
        return {'realMediaChecked': verified, 'fullAcceptancePassed': False, 'qualityApprovalStillRequired': True}


def execute_stage(args):
    # Called only under state_lock by CLI. Library users can manage their own
    # transaction lifetime rather than acquiring/releasing around individual IO.
    run = AcceptanceRun(json.loads(args.config.read_text(encoding='utf-8')), args.config.parent, args.state, args.base_url)
    reviews = json.loads(args.reviews.read_text(encoding='utf-8')) if args.reviews else {}
    if args.stage == 'plan': result = run.plan()
    elif args.stage == 'import': result = run.import_assets()
    elif args.stage == 'coordinates': result = run.coordinates()
    elif args.stage == 'check': result = run.check()
    elif args.stage == 'review': result = run.review(reviews, args.apply_reviews)
    elif args.stage == 'prepare': result = run.prepare(args.allow_model_calls, args.wait_seconds)
    elif args.stage == 'resume': result = run.resume(args.job_key, args.allow_model_calls, args.wait_seconds)
    elif args.stage == 'reconcile': result = run.reconcile(args.job_key)
    elif args.stage == 'project': result = run.create_project()
    elif args.stage == 'confirm': result = run.confirm(reviews, args.confirm_kind, args.apply_reviews)
    elif args.stage == 'verify': result = run.verify(args.compare_state)
    else: result = run.generate(args.stage, args.allow_model_calls, args.wait_seconds)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--base-url', default='http://127.0.0.1:8137')
    parser.add_argument('--stage', default='plan', choices=['plan', 'import', 'coordinates', 'check', 'review', 'prepare', 'resume', 'reconcile', 'project', 'script', 'voice', 'sample', 'render', 'confirm', 'verify'])
    parser.add_argument('--reviews', type=Path)
    parser.add_argument('--apply-reviews', action='store_true')
    parser.add_argument('--allow-model-calls', action='store_true')
    parser.add_argument('--confirm-kind', choices=['script', 'voice', 'sample'])
    parser.add_argument('--job-key')
    parser.add_argument('--compare-state', type=Path, help='Explicit second render evidence ledger for decoded PCM comparison')
    parser.add_argument('--wait-seconds', type=float, default=0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.wait_seconds) or not 0 <= args.wait_seconds <= 3600:
        parser.error('wait-seconds must be 0..3600')
    try:
        with state_lock(args.state):
            return execute_stage(args)
    except AcceptanceStateBusyError as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
