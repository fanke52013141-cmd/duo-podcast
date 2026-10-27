"""Regression for binding the server runtime to the actual sample job."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from duocast.adapters.musetalk_video import MuseTalkVideoProvider
from duocast.api import visual
from duocast.core.eventbus import EventBus
from duocast.domain.project import Project
from duocast.jobs.manager import JobManager
from duocast.services import motion_binding, motion_runtime_identity


@pytest.fixture
def setup(tmp_path, monkeypatch):
    project = Project.model_validate({
        'id': 'episode', 'generationMode': 'fast',
        'motionPack': {'id': 'pack', 'version': 'v1'},
        'currentDraftRevision': 'R1', 'scriptRevisions': [{'id': 'R1'}],
        'audioTimeline': {'revisionId': 'R1', 'sampleRate': 48000, 'sampleCount': 480000},
    })
    manager = JobManager(tmp_path / 'jobs', EventBus(tmp_path / 'events.jsonl'))
    app = FastAPI()
    app.state.project_store = SimpleNamespace(load=lambda id: project if id == project.id else None)
    app.state.job_manager = manager
    app.state.motion_pack_store = object()
    app.state.artifacts = SimpleNamespace(root=tmp_path)
    app.state.capabilities = SimpleNamespace(video={'simulated': False})
    app.include_router(visual.router)
    monkeypatch.setattr(motion_binding, 'validate_project_motion_binding', lambda *_: None)
    monkeypatch.setattr(visual, 'validate_render_options', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(visual, '_merged_pronunciation', lambda *_args: [])
    async def freeze():
        return {'root': 'C:/server-runtime', 'identity': 'a' * 64, 'protocol': 'motion-runtime-v1'}
    monkeypatch.setattr(motion_runtime_identity, 'freeze_motion_runtime', freeze)
    with TestClient(app) as client:
        yield client, manager, project


def test_sample_records_server_runtime_not_a_client_supplied_identity(setup):
    client, manager, project = setup
    response = client.post(f'/api/projects/{project.id}/visual/sample', json={
        'rangeStartSec': 0, 'rangeEndSec': 8, 'clientToken': 'sample-1',
        'fastRuntime': {'root': 'C:/untrusted', 'identity': 'b' * 64, 'protocol': 'motion-runtime-v1'},
    })
    assert response.status_code == 200, response.text
    job = manager.get(response.json()['jobId'])
    assert job.kind == 'sample.generate'
    assert job.input_snapshot['fastRuntime'] == {
        'root': 'C:/server-runtime', 'identity': 'a' * 64, 'protocol': 'motion-runtime-v1'}
    assert job.input_snapshot['projectSnapshot']['motionPack'] == {'id': 'pack', 'version': 'v1'}


def test_visual_image_job_never_freezes_or_requires_lip_sync_runtime(setup, monkeypatch):
    client, manager, project = setup
    async def unavailable():
        raise ValueError('runtime unavailable')
    monkeypatch.setattr(motion_runtime_identity, 'freeze_motion_runtime', unavailable)
    response = client.post(f'/api/projects/{project.id}/visual/generate', json={'clientToken': 'image-1'})
    assert response.status_code == 200, response.text
    job = manager.get(response.json()['jobId'])
    assert job.kind == 'visual.generate'
    assert 'fastRuntime' not in job.input_snapshot
    rejected = client.post(f'/api/projects/{project.id}/visual/sample', json={
        'rangeStartSec': 0, 'rangeEndSec': 8, 'clientToken': 'sample-no-runtime'})
    assert rejected.status_code == 422
    assert all(job.kind != 'sample.generate' for job in manager.list())


def test_motion_sample_worker_rejects_a_snapshot_without_server_runtime_identity(setup):
    _, _, project = setup
    with pytest.raises(ValueError, match='固定运行时身份'):
        asyncio.run(MuseTalkVideoProvider(None).render({'projectSnapshot': project.model_dump(by_alias=True)}))
