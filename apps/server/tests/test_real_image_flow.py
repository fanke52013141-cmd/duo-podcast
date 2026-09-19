"""ToAPIs 图片通道接线契约测试：真实提供方产物登记进变体 + mock 回退。"""

import time
from dataclasses import replace

from fastapi.testclient import TestClient

from duocast.main import app, settings


class FakeToAPIs:
    capabilities = {"simulated": False, "textToImage": True}

    def __init__(self, api_key="", artifacts_root=None, artifact_store=None, **kw):
        self.store = artifact_store

    async def generate(self, req):
        if not req.get("prompt"):
            raise ValueError("IMAGE_PROMPT_REQUIRED: 缺少画面描述，无法生成图片")
        entry = self.store.register("IMG-FIXTURE", "image", "/tmp/none.png", "dep-img")
        return {"artifactId": "IMG-FIXTURE", "path": entry["path"],
                "fileHash": "abc123", "provider": "toapis-image"}


def wait_job(client, job_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = client.get(f'/api/jobs/{job_id}').json()
        if result['status'] in ('succeeded', 'failed', 'cancelled'):
            assert result['status'] == 'succeeded', result
            return result
        time.sleep(0.03)
    raise AssertionError('job timed out')


def test_visual_generate_with_real_image_provider(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, image_provider='toapis', toapis_key='sk-test'))
    monkeypatch.setattr('duocast.main.ToAPIsImageProvider', FakeToAPIs)
    with TestClient(app) as client:
        project_id = client.post('/api/projects', json={'title': '图片通道'}).json()['id']
        base = f'/api/projects/{project_id}'
        job = wait_job(client, client.post(base + '/visual/generate', json={
            'aspect': 'portrait', 'prompt': '象牙白外套时装广告图'}).json()['jobId'])
        assert job['result']['simulated'] is False
        assert job['result']['artifactIds'] == ['IMG-FIXTURE']
        variant = client.get(base).json()['visualVariants'][-1]
        assert variant['masterImage']['artifactId'] == 'IMG-FIXTURE'
        assert variant['masterImage']['fileHash'] == 'abc123'
        assert variant['imageApiConfigRef'] == 'toapis-image'


def test_toapis_selected_without_key_falls_back_to_mock(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, image_provider='toapis', toapis_key=''))
    with TestClient(app) as client:
        caps = client.get('/api/providers/capabilities').json()
        assert caps['imageApi']['mode'] == 'mock'
        project_id = client.post('/api/projects', json={'title': '回退'}).json()['id']
        job = wait_job(client, client.post(
            f'/api/projects/{project_id}/visual/generate', json={}).json()['jobId'])
        assert job['result']['simulated'] is True
        assert job['result']['artifactIds'] == []
