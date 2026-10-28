"""Immutable server-derived plans. This ledger never claims media was generated."""
import json
import os
import re
import tempfile
from pathlib import Path
from hashlib import sha256


class ExpertPlanStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, plan_id):
        if not isinstance(plan_id, str) or re.fullmatch(r'EP-[a-f0-9]{24}', plan_id) is None:
            raise ValueError('Invalid expert plan ID')
        return self.root / (plan_id + '.json')

    def save(self, plan):
        path = self._path(plan['planId'])
        # The same window plan can belong to different projects. Keep ownership in its filename.
        owner = sha256(plan['projectId'].encode()).hexdigest()
        path = path.with_name(owner + '-' + path.name)
        fd, temporary = tempfile.mkstemp(dir=self.root, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(plan, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return plan

    def list_for(self, project_id, limit=50):
        owner = sha256(project_id.encode()).hexdigest()
        plans = []
        paths = sorted(self.root.glob(owner + '-EP-*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for path in paths[:limit]:
            value = json.loads(path.read_text(encoding='utf-8'))
            if value['projectId'] == project_id:
                plans.append(value)
        return plans

    def get(self, project_id, plan_id):
        name = self._path(plan_id).name
        path = self.root / (sha256(project_id.encode()).hexdigest()+'-'+name)
        if not path.is_file():
            return None
        plan = json.loads(path.read_text(encoding='utf-8'))
        return plan if plan['projectId'] == project_id else None
