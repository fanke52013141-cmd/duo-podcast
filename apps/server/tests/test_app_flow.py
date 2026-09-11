import time
from dataclasses import replace

from fastapi.testclient import TestClient

from duocast.main import app, settings


def wait_job(client, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = client.get(f'/api/jobs/{job_id}').json()
        if result['status'] in ('succeeded', 'failed', 'cancelled'):
            assert result['status'] == 'succeeded', result
            return result
        time.sleep(0.03)
    raise AssertionError('job timed out')


def test_demo_flow_and_restart_do_not_invent_media(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(settings, storage_root=tmp_path))
    with TestClient(app) as client:
        first = client.post('/api/projects', json={'title': '测试一期'}).json()
        second = client.post('/api/projects', json={'title': '测试二期'}).json()
        assert first['id'] != second['id']
        project_id = first['id']
        base = f'/api/projects/{project_id}'
        submitted = client.post(base + '/script/generate', json={
            'sourceInput': {'kind': 'article', 'content': '你好。\n欢迎来聊一聊。'},
            'clientToken': 'script-token',
        })
        script_job = wait_job(client, submitted.json()['jobId'])
        assert script_job['result']['simulated'] is True
        project = client.get(base).json()
        assert project['currentDraftRevision']
        voice_job = client.post(base + '/voice/synthesize', json={'transitionGapMs': [500]}).json()['jobId']
        wait_job(client, voice_job)
        project = client.get(base).json()
        audio = project['audioTimeline']
        assert audio['sampleCount'] == sum(u['sampleCount'] for u in audio['units']) + 24000
        render_job = client.post(base + '/render/generate', json={}).json()['jobId']
        result = wait_job(client, render_job)
        assert result['result']['simulated'] is True
        assert result['result']['artifactIds'] == []
        assert client.get(base).json()['outputVersion'] is None
        assert client.get('/api/artifacts').json()['artifacts'] == []

    with TestClient(app) as client:
        assert client.get(f'/api/jobs/{render_job}').json()['status'] == 'succeeded'
        assert client.get(base).json()['audioTimeline']['sampleCount'] == audio['sampleCount']
        duplicate = client.post(base + '/script/generate', json={'clientToken': 'script-token'})
        assert duplicate.json()['jobId'] == submitted.json()['jobId']
