"""模板库与读音词典持久化回归（第四轮审查遗留项落地验证）。

只使用临时目录与隔离 app.state，不启动生产 lifespan。
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from duocast.main import app as production_app
from duocast.services.voice_svc import apply_pronunciation, build_units
from duocast.storage.machine import MachineSettings
from duocast.storage.project_store import ProjectStore
from duocast.storage.templates import TemplateStore


def _flatten_api_routes(routes):
    flat = []
    for route in routes:
        if isinstance(route, APIRoute):
            flat.append(route)
        elif type(route).__name__ == "_IncludedRouter":
            flat.extend(_flatten_api_routes(route.original_router.routes))
        elif hasattr(route, "router"):
            flat.extend(_flatten_api_routes(route.router.routes))
    return flat


def make_client(tmp_path: Path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=60_000)
    machine = MachineSettings(tmp_path / "machine")
    templates = TemplateStore(tmp_path / "templates")

    @asynccontextmanager
    async def isolated_lifespan(app):
        try:
            yield
        finally:
            task = store._debounce_task
            if task is not None:
                task.cancel()
                import asyncio
                with suppress(asyncio.CancelledError):
                    await task
            store.flush()

    app = FastAPI(lifespan=isolated_lifespan, exception_handlers=production_app.exception_handlers)
    app.router.routes.extend(_flatten_api_routes(production_app.routes))
    app.state.project_store = store
    app.state.machine = machine
    app.state.template_store = templates
    app.state.job_manager = __import__("types").SimpleNamespace(list=lambda project_id=None: [])
    return TestClient(app), store, machine, templates


# ---------- 模板库 ----------

def test_builtin_templates_seeded(tmp_path):
    client, *_ = make_client(tmp_path)
    tpls = client.get("/api/templates").json()
    names = [t["name"] for t in tpls]
    assert "科技周报" in names and "人物访谈" in names
    assert all(t["builtin"] for t in tpls)


def test_save_template_from_project_extracts_config(tmp_path):
    client, store, *_ = make_client(tmp_path)
    proj = store.create("EP-TPL", "访谈第一期")
    store.apply(proj.id, {
        "aspect": "portrait",
        "voiceBindings": [{"id": "VB-A", "characterId": "A", "providerProfileId": "minimax", "modelId": "m1"}],
    }, proj.revision)
    resp = client.post("/api/templates", json={"projectId": "EP-TPL", "name": "我的访谈模板", "desc": "竖屏版式"})
    assert resp.status_code == 200, resp.text
    tpl = resp.json()
    assert tpl["builtin"] is False
    assert tpl["config"]["aspect"] == "portrait"
    assert tpl["config"]["voiceBindings"][0]["providerProfileId"] == "minimax"
    assert any(t["id"] == tpl["id"] for t in client.get("/api/templates").json())


def test_delete_template_rules(tmp_path):
    client, *_ = make_client(tmp_path)
    seeded = client.get("/api/templates").json()
    assert client.delete(f"/api/templates/{seeded[0]['id']}").status_code == 403
    created = client.post("/api/templates", json={"name": "临时模板"}).json()
    assert client.delete(f"/api/templates/{created['id']}").status_code == 200
    assert client.delete(f"/api/templates/{created['id']}").status_code == 404


def test_create_project_with_template_applies_config(tmp_path):
    client, store, *_ = make_client(tmp_path)
    tpl = next(t for t in client.get("/api/templates").json() if t["name"] == "科技周报")
    proj = client.post("/api/projects", json={"title": "套模板", "templateId": tpl["id"]}).json()
    assert proj["aspect"] == tpl["config"]["aspect"]
    bindings = {b["characterId"]: b for b in proj["voiceBindings"]}
    assert bindings["A"]["providerProfileId"] == tpl["config"]["voiceBindings"][0]["providerProfileId"]
    assert store.load("EP001").aspect == proj["aspect"]


def test_create_project_with_unknown_template_rejected(tmp_path):
    client, *_ = make_client(tmp_path)
    resp = client.post("/api/projects", json={"title": "x", "templateId": "tpl-nope"})
    assert resp.status_code == 422


# ---------- 读音词典 ----------

def test_project_pronunciation_dict_persists(tmp_path):
    client, store, *_ = make_client(tmp_path)
    store.create("EP-DICT")
    patch = {"pronunciationDict": [{"term": "KV", "read": "K-V"}, {"term": "", "read": "忽略"}]}
    resp = client.patch("/api/projects/EP-DICT", json={**patch, "expectedRevision": 0})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pronunciationDict"][0]["read"] == "K-V"
    # 非法形状被 pydantic 拒绝
    bad = client.patch("/api/projects/EP-DICT", json={"pronunciationDict": "not-a-list", "expectedRevision": 1})
    assert bad.status_code == 422


def test_global_pronunciation_dict_persists(tmp_path):
    client, _, machine, _ = make_client(tmp_path)
    entries = [{"term": "MiniMax", "read": "米泥麦克斯"}, {"term": " ", "read": "空词条应被清洗"}]
    resp = client.patch("/api/machine", json={"pronunciationDict": entries})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pronunciationDict"] == [{"term": "MiniMax", "read": "米泥麦克斯"}]
    assert machine.get_global_pronunciation() == [{"term": "MiniMax", "read": "米泥麦克斯"}]


def test_apply_pronunciation_replaces_terms():
    texts = ["KV 缓存很稳，MiniMax 也不错。"]
    out = apply_pronunciation(texts, [{"term": "KV", "read": "K-V"}, {"term": "MiniMax", "read": "米尼麦"}])
    assert out == ["K-V 缓存很稳，米尼麦 也不错。"]
    assert apply_pronunciation(texts, None) == texts


def test_unit_fingerprint_changes_with_pronunciation(tmp_path):
    from duocast.domain.project import Project, ScriptRevision, Turn, Line
    rev = ScriptRevision(id="R1", turns=[Turn(id="T1", speaker="A", lines=[Line(id="L1", display_text="KV 缓存", spoken_text="KV 缓存")])])
    project = Project(id="EP-FP")
    u1 = build_units(project, rev, {}, pronunciation=[])[0]
    u2 = build_units(project, rev, {}, pronunciation=[{"term": "KV", "read": "K-V"}])[0]
    assert u1.pronunciation_revision != u2.pronunciation_revision
