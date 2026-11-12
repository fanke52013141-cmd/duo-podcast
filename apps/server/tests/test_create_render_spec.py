"""创建工程时选定导出分辨率（标准模式按需求出片）回归。

创建与 PATCH 共用 _validate_render_spec；创建时 renderSpec 落盘，
⑤渲染页读取 renderSpec 初始化分辨率选择，链路无需额外改动。
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from duocast.main import app, settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, text_provider='mock', image_provider='mock',
        tts_provider='mock', video_provider='mock'))
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("resolution", ["854x480", "1280x720", "1920x1080"])
def test_create_with_resolution_persists(client, resolution):
    r = client.post("/api/projects", json={
        "title": "分辨率工程", "generationMode": "standard",
        "renderSpec": {"resolution": resolution}})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["renderSpec"]["resolution"] == resolution
    # 重新读取仍保持（落盘而非仅响应注入）
    assert client.get(f"/api/projects/{p['id']}").json()["renderSpec"]["resolution"] == resolution


def test_create_without_render_spec_keeps_default(client):
    p = client.post("/api/projects", json={"title": "默认"}).json()
    assert p["renderSpec"] == {}


def test_create_with_fps(client):
    p = client.post("/api/projects", json={
        "title": "帧率", "renderSpec": {"resolution": "1280x720", "fps": 30}}).json()
    assert p["renderSpec"] == {"resolution": "1280x720", "fps": 30}


@pytest.mark.parametrize("spec", [
    {"resolution": "3840x2160"},      # 超出支持档位
    {"resolution": "1920X1080"},      # 大小写敏感
    {"fps": 60},                      # 帧率不支持
    {"fps": "25"},                    # 帧率类型错
    {"width": 1920},                  # 未知字段
    "1080p",                          # 非对象
])
def test_create_rejects_invalid_spec(client, spec):
    r = client.post("/api/projects", json={"title": "坏规格", "renderSpec": spec})
    assert r.status_code == 422, r.text


def test_patch_still_validates_after_helper_extraction(client):
    p = client.post("/api/projects", json={"title": "改规格"}).json()
    ok = client.patch(f"/api/projects/{p['id']}", json={
        "expectedRevision": p["revision"], "renderSpec": {"resolution": "1920x1080"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["renderSpec"]["resolution"] == "1920x1080"
    bad = client.patch(f"/api/projects/{p['id']}", json={
        "expectedRevision": ok.json()["revision"], "renderSpec": {"resolution": "4k"}})
    assert bad.status_code == 422


def test_create_resolution_survives_template_apply(client):
    tpl = client.post("/api/templates", json={
        "name": "横屏模板", "config": {"aspect": "landscape"}}).json()
    r = client.post("/api/projects", json={
        "title": "模板+分辨率", "templateId": tpl["id"],
        "renderSpec": {"resolution": "1280x720"}})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["aspect"] == "landscape"
    assert p["renderSpec"]["resolution"] == "1280x720"