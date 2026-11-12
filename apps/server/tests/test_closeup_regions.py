"""近景虚线框（显式近景裁切范围）回归。

默认（closeupRegions 为空）行为与旧版 crop_region 完全一致；
设置后切近景完全按框裁切；修改近景框须使画面/整份依赖哈希变化（样片与缓存失效）。
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from duocast.domain.visual import VisualVariant
from duocast.main import app, settings
from duocast.services.render_inputs import dependency_hash, visual_dependency_hash
from duocast.services.video_plans import closeup_box, crop_region


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, text_provider='mock', image_provider='mock',
        tts_provider='mock', video_provider='mock'))
    with TestClient(app) as c:
        yield c


PERSON = {"A": {"x": 0.05, "y": 0.1, "width": 0.4, "height": 0.8},
          "B": {"x": 0.55, "y": 0.1, "width": 0.4, "height": 0.8}}


# ---------- 裁切框推导 ----------

def test_default_closeup_matches_legacy_crop():
    visual = {"personRegions": PERSON}
    for s in ("A", "B"):
        assert closeup_box(visual, s) == crop_region(PERSON[s])
    assert closeup_box(visual, "A")["height"] == pytest.approx(0.8 * 0.55)


def test_empty_closeup_regions_keeps_legacy():
    assert closeup_box({"personRegions": PERSON, "closeupRegions": {}}, "A") == crop_region(PERSON["A"])


def test_explicit_closeup_used_verbatim():
    frame = {"x": 0.1, "y": 0.05, "width": 0.3, "height": 0.5}
    visual = {"personRegions": PERSON, "closeupRegions": {"A": frame}}
    assert closeup_box(visual, "A") == frame  # 不再乘 0.55，完全按框
    assert closeup_box(visual, "B") == crop_region(PERSON["B"])  # 未覆盖的一侧仍自动


def test_variant_default_is_empty():
    assert VisualVariant(id="V1").closeup_regions == {}


# ---------- 端点：保存 / 校验 / 清除 ----------

def _with_variant(client):
    p = client.post("/api/projects", json={"title": "近景框"}).json()
    store = client.app.state.project_store
    proj = store.load(p["id"])
    proj = store.apply(proj.id, {"visual_variants": [VisualVariant(id="V1")]}, proj.revision)
    return proj


def test_save_closeup_regions_persists(client):
    proj = _with_variant(client)
    frame = {"A": {"x": 0.1, "y": 0.0, "width": 0.3, "height": 0.6},
             "B": {"x": 0.6, "y": 0.0, "width": 0.3, "height": 0.6}}
    r = client.post(f"/api/projects/{proj.id}/visual/regions", json={
        "variantId": "V1", "regions": PERSON, "closeupRegions": frame,
        "expectedRevision": proj.revision})
    assert r.status_code == 200, r.text
    assert r.json()["visualVariants"][0]["closeupRegions"] == frame
    again = client.get(f"/api/projects/{proj.id}").json()
    assert again["visualVariants"][0]["closeupRegions"] == frame


def test_clear_closeup_regions_restores_auto(client):
    proj = _with_variant(client)
    frame = {"A": {"x": 0.1, "y": 0.0, "width": 0.3, "height": 0.6},
             "B": {"x": 0.6, "y": 0.0, "width": 0.3, "height": 0.6}}
    r1 = client.post(f"/api/projects/{proj.id}/visual/regions", json={
        "variantId": "V1", "regions": PERSON, "closeupRegions": frame,
        "expectedRevision": proj.revision})
    r2 = client.post(f"/api/projects/{proj.id}/visual/regions", json={
        "variantId": "V1", "regions": PERSON, "closeupRegions": {},
        "expectedRevision": r1.json()["revision"]})
    assert r2.status_code == 200, r2.text
    assert r2.json()["visualVariants"][0]["closeupRegions"] == {}


def test_regions_without_closeup_field_keeps_existing(client):
    proj = _with_variant(client)
    frame = {"A": {"x": 0.1, "y": 0.0, "width": 0.3, "height": 0.6},
             "B": {"x": 0.6, "y": 0.0, "width": 0.3, "height": 0.6}}
    r1 = client.post(f"/api/projects/{proj.id}/visual/regions", json={
        "variantId": "V1", "regions": PERSON, "closeupRegions": frame,
        "expectedRevision": proj.revision})
    # 旧前端只发 regions：不应清掉已保存的近景框
    r2 = client.post(f"/api/projects/{proj.id}/visual/regions", json={
        "variantId": "V1", "regions": PERSON, "expectedRevision": r1.json()["revision"]})
    assert r2.status_code == 200
    assert r2.json()["visualVariants"][0]["closeupRegions"] == frame


@pytest.mark.parametrize("bad", [
    {"A": {"x": 0.1, "y": 0, "width": 0.3, "height": 0.6}},                # 缺 B
    {"A": {"x": -0.1, "y": 0, "width": 0.3, "height": 0.6},
     "B": {"x": 0.6, "y": 0, "width": 0.3, "height": 0.6}},                # 越界
    {"A": {"x": 0.1, "y": 0, "width": 0.95, "height": 0.6},
     "B": {"x": 0.6, "y": 0, "width": 0.3, "height": 0.6}},                # x+w>1
    "notdict",
])
def test_invalid_closeup_regions_rejected(client, bad):
    proj = _with_variant(client)
    r = client.post(f"/api/projects/{proj.id}/visual/regions", json={
        "variantId": "V1", "regions": PERSON, "closeupRegions": bad,
        "expectedRevision": proj.revision})
    assert r.status_code == 422, r.text


# ---------- 依赖哈希：改近景框 → 样片/缓存失效 ----------

def test_closeup_change_invalidates_hashes(client):
    proj = _with_variant(client)
    before_v, before_d = visual_dependency_hash(proj), dependency_hash(proj)
    frame = {"A": {"x": 0.1, "y": 0.0, "width": 0.3, "height": 0.6},
             "B": {"x": 0.6, "y": 0.0, "width": 0.3, "height": 0.6}}
    r = client.post(f"/api/projects/{proj.id}/visual/regions", json={
        "variantId": "V1", "regions": PERSON, "closeupRegions": frame,
        "expectedRevision": proj.revision})
    after = client.app.state.project_store.load(proj.id)
    assert visual_dependency_hash(after) != before_v
    assert dependency_hash(after) != before_d