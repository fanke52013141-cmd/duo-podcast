from types import SimpleNamespace
from copy import deepcopy
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from duocast.api import expert_previews as api
from duocast.domain.project import Project
from duocast.domain.job import JobStatus
from duocast.jobs.manager import JobManager
from duocast.core.eventbus import EventBus
from duocast.services.render_inputs import dependency_hash
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.expert_plans import ExpertPlanStore
from duocast.storage.expert_versions import ExpertVersionStore


@pytest.fixture
def setup(tmp_path):
    project = Project.model_validate({'id': 'p'})
    app = FastAPI(); app.include_router(api.router)
    state = app.state
    state.project_store = SimpleNamespace(load=lambda id: project if id == 'p' else None)
    state.job_manager = JobManager(tmp_path/'jobs', EventBus(tmp_path/'events.jsonl'))
    state.expert_plan_store = ExpertPlanStore(tmp_path/'plans')
    state.expert_version_store = ExpertVersionStore(tmp_path/'versions')
    state.artifacts = ArtifactStore(tmp_path/'artifacts')
    state.expert_executor = SimpleNamespace(engine_fingerprint='c'*64)  # Never start workers or access any model.
    plan = {'planId': 'EP-'+'a'*24, 'projectId': 'p', 'dependencyHash': dependency_hash(project),
            'snapshotId': 'snapshot', 'engineFingerprint':'c'*64, 'executionProfile':'expert-sage-roi-v1', 'windows': [{'windowId': 'W1'}]}
    state.expert_plan_store.save(plan)
    with TestClient(app) as client:
        yield client, state, project, plan


def submit(client, plan, token='request-123'):
    response = client.post('/api/projects/p/expert-video/generate', json={'planId': plan['planId'], 'clientToken': token})
    assert response.status_code == 200, response.text
    return response.json()['jobId']


def complete(state, job_id):
    job = state.job_manager.get(job_id)
    file = state.artifacts.root/(job_id+'.mp4')
    file.write_bytes(b'isolated candidate fixture, not model output')
    state.artifacts.register(job_id, 'video', str(file), job.input_snapshot['dependencyHash'])
    job.status = JobStatus.SUCCEEDED
    job.result = {'artifactIds': [job_id], 'simulated': False}
    return file


def adopt(client, job, revision):
    return client.post(f'/api/projects/p/expert-video/candidates/{job}/adopt', json={
        'expectedRevision': revision, 'checks': ['sync', 'stable', 'blend', 'boundary']})


def test_idempotent_submit_and_no_execution_when_not_configured(setup):
    client, state, _, plan = setup
    first = submit(client, plan)
    assert submit(client, plan) == first
    assert len(state.job_manager.list('p')) == 1
    state.expert_executor = None
    assert submit(client, plan) == first  # Uncertain submit still recovers the original ID.
    assert client.post('/api/projects/p/expert-video/generate', json={
        'planId': plan['planId'], 'clientToken': 'different-123'}).status_code == 503


def test_adopt_replace_restore_and_revision_conflict(setup):
    client, state, _, plan = setup
    first = submit(client, plan); complete(state, first)
    assert adopt(client, first, 0).json()['selected'] == {'W1': first}
    second = submit(client, plan, 'second-123'); complete(state, second)
    assert adopt(client, second, 0).status_code == 409
    assert adopt(client, second, 1).json()['selected'] == {'W1': second}
    response = client.post('/api/projects/p/expert-video/restore', json={'expectedRevision': 2, 'version': 1})
    assert response.status_code == 200
    assert response.json()['selected'] == {'W1': first}
    assert ExpertVersionStore(state.expert_version_store.root).read('p')['revision'] == 3


def test_unfinished_simulated_corrupt_and_stale_candidates_cannot_replace_current(setup):
    client, state, project, plan = setup
    first = submit(client, plan); complete(state, first)
    assert adopt(client, first, 0).status_code == 200
    second = submit(client, plan, 'second-123')
    assert adopt(client, second, 1).status_code == 422
    file = complete(state, second)
    state.job_manager.get(second).result['simulated'] = True
    assert adopt(client, second, 1).status_code == 422
    state.job_manager.get(second).result['simulated'] = False
    file.write_bytes(b'changed')
    assert adopt(client, second, 1).status_code == 422
    project.camera_mode = 'twoShot'
    assert adopt(client, first, 1).status_code == 409
    assert client.post('/api/projects/p/expert-video/restore', json={'expectedRevision': 1, 'version': 1}).status_code == 409
    assert state.expert_version_store.read('p')['selected'] == {'W1': first}


def test_missing_review_and_token_collision_are_rejected(setup):
    client, state, _, plan = setup
    job = submit(client, plan); complete(state, job)
    assert client.post(f'/api/projects/p/expert-video/candidates/{job}/adopt', json={
        'expectedRevision': 0, 'checks': []}).status_code == 422
    another = deepcopy(plan); another['planId'] = 'EP-'+'b'*24
    state.expert_plan_store.save(another)
    assert client.post('/api/projects/p/expert-video/generate', json={
        'planId': another['planId'], 'clientToken': 'request-123'}).status_code == 409


def test_continuation_rejects_changed_selection_before_submit_and_adoption(setup):
    client,state,_,plan=setup
    plan.update(operation='continue',reuseSelectionRevision=0,generateWindowCount=1)
    state.expert_plan_store.save(plan)
    job=submit(client,plan);complete(state,job)
    state.expert_version_store.select('p',expected_revision=0,snapshot_id='snapshot',job_id='another',windows=['W0'])
    assert client.post('/api/projects/p/expert-video/generate',json={
        'planId':plan['planId'],'clientToken':'new-request'}).status_code==409
    assert adopt(client,job,1).status_code==409
    assert state.expert_version_store.read('p')['selected']=={'W0':'another'}


def test_fully_adopted_continuation_never_queues_model_task(setup):
    client,state,_,plan=setup
    plan.update(operation='continue',reuseSelectionRevision=0,generateWindowCount=0)
    state.expert_plan_store.save(plan)
    response=client.post('/api/projects/p/expert-video/generate',json={'planId':plan['planId'],'clientToken':'request-123'})
    assert response.status_code==422
    assert not state.job_manager.list('p')
