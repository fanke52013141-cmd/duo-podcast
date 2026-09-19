"""真实 TTS 路径契约测试：provider 返回 audioPath 时母轨拼接、产物登记、文件端点可用。

FakeRealTTS 模拟 ComfyUITTSProvider 的输出契约（48kHz PCM16 单声道 + audioAssetId/audioPath），
不依赖 ComfyUI 在线；字节级正确性由 链路实测/test_master_track.py 覆盖。
"""

import time
import wave
from array import array
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from duocast.main import app, settings


class FakeRealTTS:
    capabilities = {"simulated": False, "engine": "fake-comfyui"}

    def __init__(self, base_url="", input_dir=None, refs=None,
                 artifacts_root=None, artifact_store=None, **kw):
        self.artifacts_root = Path(artifacts_root)
        self.store = artifact_store

    async def synthesize(self, payload):
        return self._make_wav()

    async def audition(self, payload):
        return self._make_wav()

    def _make_wav(self):
        n = 4800  # 0.1s @48k
        asset_id = f"AUD-F{len(self.store._manifest()['artifacts']):04d}"
        path = self.artifacts_root / "audio" / f"{asset_id}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(48000)
            wf.writeframes(array("h", [1000] * n).tobytes())
        self.store.register(asset_id, "audio", str(path), f"dep-{asset_id}")
        return {"sampleRate": 48000, "sampleCount": n, "durationMs": 100,
                "audioAssetId": asset_id, "audioPath": str(path)}


def wait_job(client, job_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = client.get(f'/api/jobs/{job_id}').json()
        if result['status'] in ('succeeded', 'failed', 'cancelled'):
            assert result['status'] == 'succeeded', result
            return result
        time.sleep(0.03)
    raise AssertionError('job timed out')


def test_real_tts_master_track_and_file_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, tts_provider='comfyui'))
    monkeypatch.setattr('duocast.main.ComfyUITTSProvider', FakeRealTTS)
    with TestClient(app) as client:
        project_id = client.post('/api/projects', json={'title': '母轨测试'}).json()['id']
        base = f'/api/projects/{project_id}'
        gen = client.post(base + '/script/generate', json={
            'sourceInput': {'kind': 'article', 'content': '你好。\n欢迎来聊一聊。'},
            'clientToken': 's1'})
        wait_job(client, gen.json()['jobId'])
        job = wait_job(client, client.post(base + '/voice/synthesize', json={
            'transitionGapMs': [500]}).json()['jobId'])

        assert job['result']['simulated'] is False  # 真实通道不标模拟
        tl = client.get(base).json()['audioTimeline']
        units = tl['units']
        assert len(units) >= 2
        master_id = tl['masterAudioAssetId']
        assert master_id and master_id in job['result']['artifactIds']
        assert all(u['adoptedAudioAssetId'] in job['result']['artifactIds'] for u in units)

        r = client.get(f'/api/artifacts/{master_id}/file')
        assert r.status_code == 200
        assert r.content[:4] == b'RIFF'
        import io
        with wave.open(io.BytesIO(r.content), 'rb') as wf:
            assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, 48000)
            assert wf.getnframes() == tl['sampleCount']

        manifest = {a['artifactId']: a for a in client.get('/api/artifacts').json()['artifacts']}
        assert manifest[master_id]['kind'] == 'mixed'
        assert manifest[units[0]['adoptedAudioAssetId']]['kind'] == 'audio'

        assert client.get('/api/artifacts/AUD-NOT-EXIST/file').status_code == 404


def test_mock_tts_has_no_master_track(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(settings, storage_root=tmp_path))
    with TestClient(app) as client:
        project_id = client.post('/api/projects', json={'title': '模拟一期'}).json()['id']
        base = f'/api/projects/{project_id}'
        gen = client.post(base + '/script/generate', json={
            'sourceInput': {'kind': 'article', 'content': '你好。\n欢迎来聊一聊。'},
            'clientToken': 's1'})
        wait_job(client, gen.json()['jobId'])
        job = wait_job(client, client.post(base + '/voice/synthesize', json={}).json()['jobId'])
        assert job['result']['simulated'] is True
        assert job['result']['artifactIds'] == []
        tl = client.get(base).json()['audioTimeline']
        assert tl['masterAudioAssetId'] is None
        assert client.get('/api/artifacts/AUD-NOT-EXIST/file').status_code == 404
