"""Atomic, revision-checked window selections; failed jobs never replace selections."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import threading
from datetime import datetime, timezone


class ExpertVersionStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def _path(self, project_id):
        return self.root / (sha256(project_id.encode()).hexdigest()+'.json')

    def read(self, project_id):
        with self.lock:
            path = self._path(project_id)
            return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
                'revision': 0, 'selected': {}, 'versions': [], 'snapshotId': None}

    def select(self, project_id, *, expected_revision, snapshot_id, job_id=None, windows=None, restore=None, checks=None):
        with self.lock:
            state = self.read(project_id)
            if state['revision'] != expected_revision:
                raise ValueError('版本已改变，请刷新后重新操作')
            if restore is not None:
                old = next((v for v in state['versions'] if v['version'] == restore), None)
                if not old or old['snapshotId'] != snapshot_id:
                    raise ValueError('历史版本不存在或已过期')
                selected = deepcopy(old['selected'])
            else:
                selected = deepcopy(state['selected']) if state['snapshotId'] == snapshot_id else {}
                selected.update({window: job_id for window in windows})
            version = {'version': state['revision']+1, 'snapshotId': snapshot_id, 'selected': selected,
                'at': datetime.now(timezone.utc).isoformat(), 'action': 'restore' if restore is not None else 'adopt',
                'restoredFrom': restore, 'candidateJobId': job_id, 'reviewChecks': list(checks or [])}
            state.update(revision=version['version'], snapshotId=snapshot_id, selected=selected)
            state['versions'].append(version)
            fd, temporary = tempfile.mkstemp(dir=self.root, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(state, stream, ensure_ascii=False, allow_nan=False)
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, self._path(project_id))
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
            return state
