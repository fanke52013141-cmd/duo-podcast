"""Read known external IDs only. This module never submits or resumes inference."""
from urllib.parse import quote
import time

import httpx

from ..domain.motion_pack import content_hash


def external_receipts(job):
    if job.kind == 'motion.prepare':
        record = job.completed_calls.get('motionPreparation', {})
        if (not isinstance(record, dict) or job.owner_type != 'motionPack' or not job.input_snapshot.get('resumableMotionPreparation')
                or not isinstance(job.input_snapshot.get('workflow'), dict)
                or record.get('promptId') != job.provider_task_id
                or record.get('workflowHash') != content_hash(job.input_snapshot['workflow'])):
            return []
        records = [('prepare', record)]
    else:
        record = job.completed_calls.get('expertMotion', {})
        project = job.input_snapshot.get('projectSnapshot', {})
        if (not isinstance(record, dict) or not isinstance(project, dict)
                or job.kind not in {'renders', 'sample.generate'} or job.owner_type != 'project'
                or project.get('id') != job.project_id or not project.get('motionPack')
                or record.get('snapshotHash') != content_hash(job.input_snapshot)):
            return []
        segments = record.get('segments', {})
        if not isinstance(segments, dict):
            return []
        records = segments.items()
    return [(str(key), value['promptId']) for key, value in records
            if isinstance(value, dict) and isinstance(value.get('promptId'), str)
            and 0 < len(value['promptId']) <= 128 and not any(c in value['promptId'] for c in '/\\\r\n')]


async def inspect_external(job, base_url, *, transport=None):
    receipts = external_receipts(job)
    if not receipts:
        return {'jobId': job.id, 'receipts': [], 'canInspect': False,
                'notice': '没有可核对的已知外部任务ID；请保留记录并人工核实，不要重复提交。'}
    result = []
    deadline = time.monotonic() + 12
    async with httpx.AsyncClient(base_url=base_url.rstrip('/'), timeout=3, transport=transport, trust_env=False) as client:
        for segment, prompt_id in receipts[:20]:
            if time.monotonic() >= deadline:
                result.append({'segmentId':segment,'promptId':prompt_id,'status':'unavailable'})
                continue
            status = 'unknown'
            try:
                response = await client.get('/history/' + quote(prompt_id, safe=''))
                response.raise_for_status()
                payload = response.json().get(prompt_id)
                if isinstance(payload, dict):
                    state = payload.get('status', {})
                    if state.get('status_str') == 'error':
                        status = 'failed'
                    elif state.get('completed') is True:
                        status = 'completed'
                else:
                    queue = await client.get('/queue')
                    queue.raise_for_status()
                    data = queue.json()
                    for key, value in (('queue_running', 'running'), ('queue_pending', 'queued')):
                        if any(isinstance(row, list) and len(row) > 1 and row[1] == prompt_id for row in data.get(key, [])):
                            status = value
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                status = 'unavailable'
            result.append({'segmentId': segment, 'promptId': prompt_id, 'status': status})
    return {'jobId': job.id, 'receipts': result, 'canInspect': True, 'truncated': len(receipts) > 20,
            'notice': '只读核对原任务，没有提交新推理或采用结果；引擎完成不代表媒体或画质验收通过。'}
