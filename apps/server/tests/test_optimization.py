"""方案关键契约回归：使用临时工程，不依赖运行中的服务或付费 API。"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from duocast.adapters.base import CapabilityRegistry
from duocast.adapters.mock import MockTTSProvider
from duocast.api.render import generate
from duocast.api.voice import synthesize
from duocast.core.eventbus import EventBus
from duocast.domain.audio import AudioTimeline
from duocast.domain.job import JobStatus
from duocast.domain.project import Approval, ScriptRevision, StageState, Turn, Line
from duocast.jobs.manager import JobManager
from duocast.jobs.recovery import recover_on_startup
from duocast.services.script_svc import apply_script_revision
from duocast.services.stage_svc import derive_stage_states
from duocast.services.voice_svc import build_units, synthesize_timeline
from duocast.storage.project_store import ProjectStore


def project_fixture(tmp_path):
    store = ProjectStore(tmp_path / 'projects', debounce_ms=0)
    project = store.create('test')
    revision = ScriptRevision(id='R1', turns=[
        Turn(id='A-turn', speaker='A', lines=[Line(id='a', spoken_text='你好')]),
        Turn(id='B-turn', speaker='B', lines=[Line(id='b', spoken_text='你好呀')]),
    ])
    project = store.apply(project.id, {'script_revisions': [revision], 'current_draft_revision': 'R1'}, 0)
    return store, project, revision


class AudioProvider:
    def __init__(self, sample_rate=48000):
        self.requests = []
        self.sample_rate = sample_rate

    async def synthesize(self, request):
        self.requests.append(request)
        return {'sampleRate': self.sample_rate, 'sampleCount': 48000}


def test_audio_includes_all_gaps_and_passes_voice_parameters(tmp_path):
    store, project, revision = project_fixture(tmp_path)
    revision.turns[0].tone = '惊讶'
    revision.turns[0].speed_ratio = 1.2
    provider = AudioProvider()
    timeline = asyncio.run(synthesize_timeline(store, provider, project, revision, {}, [500], 100, 200))
    assert timeline.unit_offsets == {'U-A-turn': 4800, 'U-B-turn': 76800}
    assert timeline.sample_count == 134400  # 2s 发言 + 0.5s 间隔 + 0.3s 首尾
    assert provider.requests[0]['speedRatio'] == 1.2
    assert provider.requests[0]['emotion']['label'] == '惊讶'


@pytest.mark.parametrize('gaps', [[], [-1], [1, 2], [True], [1.5]])
def test_invalid_gaps_fail_before_provider_call(tmp_path, gaps):
    store, project, revision = project_fixture(tmp_path)
    provider = AudioProvider()
    with pytest.raises(ValueError, match='INVALID_GAPS'):
        asyncio.run(synthesize_timeline(store, provider, project, revision, {}, gaps))
    assert provider.requests == []


def test_sample_rate_mismatch_cannot_silently_change_duration(tmp_path):
    store, project, revision = project_fixture(tmp_path)
    with pytest.raises(ValueError, match='INVALID_SAMPLE_RATE'):
        asyncio.run(synthesize_timeline(store, AudioProvider(24000), project, revision, {}))
    assert store.load(project.id).audio_timeline is None


def test_old_audio_is_retained_without_overwriting_new_draft(tmp_path):
    store, project, revision = project_fixture(tmp_path)
    newer = ScriptRevision(id='R2')
    store.apply(project.id, {'script_revisions': [revision, newer], 'current_draft_revision': 'R2'}, project.revision)
    asyncio.run(synthesize_timeline(store, AudioProvider(), project, revision, {}))
    latest = store.load(project.id)
    assert latest.current_draft_revision == 'R2'
    assert latest.audio_timeline is None
    assert latest.audio_history[0].revision_id == 'R1'


def test_generated_script_does_not_replace_edited_draft(tmp_path):
    store, project, revision = project_fixture(tmp_path)
    store.apply(project.id, {'title': '新编辑'}, project.revision)
    result = apply_script_revision(store, project.id, ScriptRevision(id='candidate'), project.revision)
    assert result.current_draft_revision == 'R1'
    assert result.script_revisions[-1].id == 'candidate'


def test_unit_ids_stay_stable_after_reordering(tmp_path):
    _, project, revision = project_fixture(tmp_path)
    before = {u.turn_id: u.id for u in build_units(project, revision, {})}
    revision.turns.reverse()
    after = {u.turn_id: u.id for u in build_units(project, revision, {})}
    assert before == after


def manager_fixture(tmp_path):
    return JobManager(tmp_path / 'jobs', EventBus(tmp_path / 'events.jsonl'), {'gpu': 1, 'api': 2, 'cpu': 1})


async def wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout=3)


def test_job_snapshot_and_idempotency_are_scoped(tmp_path):
    manager = manager_fixture(tmp_path)
    snapshot = {'nested': {'text': 'original'}}
    first = manager.submit('test', 'one', 'gpu', 'token', snapshot)
    snapshot['nested']['text'] = 'edited'
    assert first.input_snapshot['nested']['text'] == 'original'
    assert manager.submit('test', 'one', 'gpu', 'token', {}).id == first.id
    assert manager.submit('test', 'two', 'gpu', 'token', {}).id != first.id
    assert manager.submit('another-kind', 'one', 'gpu', 'token', {}).id != first.id
    for i in range(10):
        manager.submit('test', 'one', 'gpu', str(i), {})  # 排队长度不等于 GPU 并发


def test_api_can_run_while_gpu_busy_and_gpu_remains_serial(tmp_path):
    async def scenario():
        manager = manager_fixture(tmp_path)
        release = asyncio.Event()
        entered = []
        async def runner(job):
            entered.append(job.kind)
            if job.queue_class == 'gpu':
                await release.wait()
            return {}
        manager.set_runner(runner)
        manager.submit('gpu1', 'p', 'gpu', '1', {})
        manager.submit('gpu2', 'p', 'gpu', '2', {})
        api = manager.submit('api', 'p', 'api', '3', {})
        manager.start()
        try:
            await wait_until(lambda: api.status == JobStatus.SUCCEEDED)
            assert 'gpu1' in entered and 'gpu2' not in entered
            release.set()
            await wait_until(lambda: all(j.status == JobStatus.SUCCEEDED for j in manager.list()))
        finally:
            await manager.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('action,expected', [('pause', JobStatus.PAUSED), ('cancel', JobStatus.CANCELLED)])
def test_running_controls_reach_final_state_without_regeneration(tmp_path, action, expected):
    async def scenario():
        manager = manager_fixture(tmp_path)
        release = asyncio.Event()
        calls = []
        async def runner(job):
            calls.append(job.id)
            await release.wait()
            return {'artifactIds': ['kept']}
        manager.set_runner(runner)
        job = manager.submit('test', 'p', 'gpu', 'token', {})
        manager.start()
        try:
            await wait_until(lambda: job.status == JobStatus.RUNNING)
            getattr(manager, action)(job.id)
            release.set()
            await wait_until(lambda: job.status == expected)
            assert job.result['artifactIds'] == ['kept']
            if action == 'pause':
                manager.resume(job.id)
                assert job.status == JobStatus.SUCCEEDED
            assert len(calls) == 1
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_restore_queued_history_and_unknown(tmp_path):
    async def scenario():
        old = manager_fixture(tmp_path)
        queued = old.submit('queued', 'p', 'gpu', 'q', {})
        running = old.submit('running', 'p', 'gpu', 'r', {})
        old.transition(running, JobStatus.RUNNING)
        old.pause(running.id)
        finished = old.submit('finished', 'p', 'api', 'f', {})
        old.cancel(finished.id)
        restored = manager_fixture(tmp_path)
        restored.restore(recover_on_startup(tmp_path / 'jobs'))
        assert restored.get(running.id).status == JobStatus.UNKNOWN
        assert restored.get(finished.id).status == JobStatus.CANCELLED
        assert restored.submit('finished', 'p', 'api', 'f', {}).id == finished.id
        calls = []
        async def runner(job):
            calls.append(job.id)
            return {}
        restored.set_runner(runner)
        restored.start()
        try:
            await wait_until(lambda: restored.get(queued.id).status == JobStatus.SUCCEEDED)
            assert calls == [queued.id]
        finally:
            await restored.stop()
    asyncio.run(scenario())


def test_voice_api_freezes_revision_at_submission(tmp_path):
    store, project, revision = project_fixture(tmp_path)
    manager = manager_fixture(tmp_path)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_store=store, job_manager=manager)))
    result = asyncio.run(synthesize(project.id, {}, request))
    job = manager.get(result['jobId'])
    assert job.input_snapshot['revisionId'] == 'R1'
    assert job.input_snapshot['projectSnapshot']['scriptRevisions'][0]['id'] == 'R1'


@pytest.mark.parametrize('seconds,expected', [(300, None), (301, 422)])
def test_render_duration_limit_includes_tail(tmp_path, seconds, expected):
    store, project, revision = project_fixture(tmp_path)
    project = store.apply(project.id, {'audio_timeline': AudioTimeline(revision_id='R1', sample_count=seconds * 48000)}, project.revision)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_store=store, job_manager=manager_fixture(tmp_path))))
    if expected:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(generate(project.id, {}, request))
        assert caught.value.status_code == expected
    else:
        assert asyncio.run(generate(project.id, {}, request))['jobId']


def test_old_approval_cannot_confirm_new_revision(tmp_path):
    _, project, _ = project_fixture(tmp_path)
    project.approvals = [Approval(kind='script', input_revision_id='old')]
    project.audio_timeline = AudioTimeline(revision_id='old', sample_count=48000)
    states = derive_stage_states(project)
    assert states['script'] == StageState.PENDING_CONFIRM
    assert states['voice'] == StageState.STALE


def test_mock_capabilities_never_claim_production_ready():
    result = CapabilityRegistry(tts=MockTTSProvider().capabilities).snapshot()['tts']
    assert result['ready'] is True
    assert result['productionReady'] is False
    assert result['mode'] == 'mock'


def test_new_audio_requires_new_voice_and_sample_confirmation(tmp_path):
    store, project, revision = project_fixture(tmp_path)
    project = store.apply(project.id, {'approvals': [
        Approval(kind=kind, input_revision_id='R1') for kind in ('script', 'voice', 'sample')
    ]}, project.revision)
    asyncio.run(synthesize_timeline(store, AudioProvider(), project, revision, {}))
    assert [a.kind for a in store.load(project.id).approvals] == ['script']
