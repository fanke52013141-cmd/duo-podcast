import asyncio
from types import SimpleNamespace
from duocast.core.eventbus import EventBus
from duocast.jobs.manager import JobManager
from duocast.jobs.recovery import recover_on_startup
from duocast.domain.job import JobStatus
from duocast.main import _job_runner


def test_owned_expert_job_dispatch_and_restart_recovery(tmp_path):
    manager = JobManager(tmp_path/'jobs', EventBus(tmp_path/'events.jsonl'))
    job = manager.submit(kind='expert.generate', project_id='p', queue_class='gpu',
        client_token='request-123', input_snapshot={'projectSnapshot':{'id':'p'}, 'resumableVideo':True})
    class NoModelExecutor:
        capabilities = {'simulated': True}
        async def render(self, request, progress):
            assert request == {**job.input_snapshot, 'jobId':job.id}
            progress('fixture execution, no inference')
            return {'artifactIds':[], 'simulated':True}
    runner = _job_runner(manager, SimpleNamespace(flush=lambda:None), None, None, None, None, expert_executor=NoModelExecutor())
    assert asyncio.run(runner(job))['simulated'] is True
    assert job.stage == 'fixture execution, no inference'
    job.status = JobStatus.RUNNING
    manager._persist(job)
    restored = recover_on_startup(tmp_path/'jobs')
    assert len(restored) == 1 and restored[0].status == JobStatus.QUEUED
    assert restored[0].client_token == 'request-123'
