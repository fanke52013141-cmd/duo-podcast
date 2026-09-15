"""工程安全回归：只使用临时目录，不启动生产 lifespan 或默认 smoke。"""

import asyncio
import errno
import json
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError
from setuptools.config.pyprojecttoml import read_configuration

from duocast.domain.project import Project, ScriptRevision
from duocast.main import app as production_app
from duocast.storage.project_store import (
    InvalidProjectInput,
    ProjectAlreadyExists,
    ProjectRevisionConflict,
    ProjectStore,
)


INVALID_IDS = [
    None, 0, 1, True, 0.5, [], {}, "", ".", "..",
    "../outside", "..\\outside", "nested/project", "nested\\project",
    "/tmp/outside", "C:\\outside", "C:/outside", "C:outside",
    "\\\\server\\share", "//server/share", "\\rooted",
    "EP001.", "EP001 ", " EP001", "EP 001", "\x00", "\n", "EP001\n",
    "a:b", "a?b", "a*b", '"EP001"', "a|b", "a<b", "a>b",
    "CON", "con", "PrN", "AUX", "nul", "COM1", "com9", "LPT1", "lpt9",
    "CON.txt", "NUL.json", "COM1.txt", "CONIN$", "CONOUT$", "COM¹", "LPT²",
    pytest.param("a" * 256, id="too-long"),
]
VALID_IDS = [
    "EP001", "a17a96de-58a0-4c34-81a0-840b88a98922", "abcXYZ019",
    "episode_1-part-2", "test", "COM0", "COM10", "LPT0", "LPT10", "_", "-",
]


@pytest.fixture
def store(tmp_path):
    return ProjectStore(tmp_path / "projects", debounce_ms=60_000)


@pytest.fixture
def client(store):
    @asynccontextmanager
    async def isolated_lifespan(app):
        try:
            yield
        finally:
            task = store._debounce_task
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            store.flush()

    # 复用真实路由及异常映射，但不访问生产 app.state、启动恢复或任务执行器。
    app = FastAPI(
        lifespan=isolated_lifespan,
        exception_handlers=production_app.exception_handlers,
    )
    app.router.routes.extend(_flatten_api_routes(production_app.routes))
    app.state.project_store = store
    app.state.job_manager = SimpleNamespace(list=lambda project_id=None: [])
    with TestClient(app) as test_client:
        yield test_client


def make_symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP) or getattr(exc, "winerror", None) == 1314:
            pytest.skip("当前系统不允许创建符号链接")
        raise
    # 此环境 Python 3.13 可能不抛错但静默未创建链接；此时链接相关用例无意义。
    if not (link.is_symlink() or link.exists()):
        pytest.skip("符号链接静默创建失败（无权限或平台限制）")


def _flatten_api_routes(routes):
    """FastAPI 0.141+ 的 include_router 生成 _IncludedRouter 包装，需递归展开。"""
    flat = []
    for route in routes:
        if isinstance(route, APIRoute):
            flat.append(route)
        elif type(route).__name__ == "_IncludedRouter":
            flat.extend(_flatten_api_routes(route.original_router.routes))
        elif hasattr(route, "router"):
            flat.extend(_flatten_api_routes(route.router.routes))
    return flat


@pytest.mark.parametrize("project_id", INVALID_IDS)
@pytest.mark.parametrize("operation", ["_path", "load", "create", "apply"])
def test_store_rejects_invalid_ids_before_access(store, project_id, operation):
    with pytest.raises(InvalidProjectInput):
        if operation == "apply":
            store.apply(project_id, {"title": "不能写入"}, 0)
        else:
            getattr(store, operation)(project_id)
    assert store._projects == {}
    assert store._dirty == set()
    assert list(store.root.iterdir()) == []


@pytest.mark.parametrize("project_id", [b"EP001", Path("EP001")])
def test_store_does_not_coerce_non_string_ids(store, project_id):
    with pytest.raises(InvalidProjectInput):
        store.load(project_id)


@pytest.mark.parametrize("project_id", VALID_IDS)
def test_regular_ids_round_trip(store, project_id):
    created = store.create(project_id, "原工程")
    reloaded = ProjectStore(store.root).load(project_id)
    assert reloaded == created
    assert reloaded.id == project_id
    assert store.list_ids() == [project_id]


@pytest.mark.parametrize("cached", [False, True])
def test_duplicate_store_create_never_returns_or_changes_old_project(store, monkeypatch, cached):
    if cached:
        monkeypatch.setattr(store, "_schedule_flush", lambda: None)
    original = store.create("EP002", "原工程")
    reader = store if cached else ProjectStore(store.root)
    with pytest.raises(ProjectAlreadyExists):
        reader.create("EP002", "不能覆盖")
    assert reader.load("EP002") == original
    assert reader.load("EP002").title == "原工程"


def test_unflushed_ids_respect_platform_path_case_rules(store, monkeypatch):
    monkeypatch.setattr(store, "_schedule_flush", lambda: None)
    store.create("EP002", "原工程")
    if store.root / "EP002" == store.root / "ep002":
        with pytest.raises(ProjectAlreadyExists):
            store.create("ep002", "不能覆盖")
    else:
        assert store.create("ep002").id == "ep002"
    assert store.load("EP002").title == "原工程"


@pytest.mark.parametrize("occupied", ["directory", "file", "broken-json"])
def test_create_rejects_occupied_paths_without_reading_them(store, occupied):
    path = store.root / "EP001"
    if occupied == "file":
        path.write_text("占位", encoding="utf-8")
    else:
        path.mkdir()
        if occupied == "broken-json":
            (path / "project.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ProjectAlreadyExists):
        store.create("EP001")
    assert store._projects == {}
    assert store._dirty == set()


def test_invalid_cached_id_is_still_checked(store):
    store._projects["../outside"] = Project(id="../outside")
    with pytest.raises(InvalidProjectInput):
        store.load("../outside")
    assert store.list_ids() == []


def test_resolved_path_must_stay_inside_root_even_on_cache_hit(store, tmp_path, monkeypatch):
    original = store.create("EP001")
    candidate = store.root / original.id / "project.json"
    outside = tmp_path / "projects-other" / "project.json"
    real_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        return outside if path == candidate else real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(InvalidProjectInput):
        store.load(original.id)
    with pytest.raises(InvalidProjectInput):
        store.create(original.id)
    with pytest.raises(InvalidProjectInput):
        store.apply(original.id, {"title": "不能写入"}, 0)
    store._dirty.add(original.id)
    with pytest.raises(InvalidProjectInput):
        store.flush()
    assert not outside.exists()
    assert store.list_ids() == []


@pytest.mark.parametrize("error", [OSError("无法解析"), RuntimeError("链接循环")])
def test_resolution_errors_are_controlled(store, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(Path, "resolve", fail)
    with pytest.raises(InvalidProjectInput):
        store.load("EP001")


@pytest.mark.parametrize("inside", [False, True])
def test_project_directory_symlinks_are_not_loaded_or_created(store, tmp_path, inside):
    target = (store.root if inside else tmp_path) / "target"
    target.mkdir()
    sentinel = target / "project.json"
    sentinel.write_text(Project(id="target").model_dump_json(), encoding="utf-8")
    before = sentinel.read_bytes()
    make_symlink(store.root / "EP001", target, directory=True)
    for operation in (store.load, store.create):
        with pytest.raises(InvalidProjectInput):
            operation("EP001")
    assert "EP001" not in store.list_ids()
    assert sentinel.read_bytes() == before


def test_cached_directory_replacement_cannot_escape_on_read_apply_or_flush(store, tmp_path):
    original = store.create("EP001")
    directory = store.root / original.id
    saved = store.root / "saved"
    outside = tmp_path / "outside"
    outside.mkdir()
    directory.rename(saved)
    make_symlink(directory, outside, directory=True)
    with pytest.raises(InvalidProjectInput):
        store.load(original.id)
    with pytest.raises(InvalidProjectInput):
        store.apply(original.id, {"title": "不能写入"}, 0)
    store._dirty.add(original.id)
    with pytest.raises(InvalidProjectInput):
        store.flush()
    assert list(outside.iterdir()) == []


def test_project_json_symlink_is_not_read_or_written(store, tmp_path):
    original = store.create("EP001")
    target = store.root / original.id / "project.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(target.read_bytes())
    before = outside.read_bytes()
    target.unlink()
    make_symlink(target, outside)
    with pytest.raises(InvalidProjectInput):
        store.load(original.id)
    store._dirty.add(original.id)
    with pytest.raises(InvalidProjectInput):
        store.flush()
    assert outside.read_bytes() == before


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_flush_does_not_follow_preexisting_temporary_file_links(store, tmp_path, link_kind):
    store.create("EP001")
    outside = tmp_path / "outside.txt"
    outside.write_text("不能截断", encoding="utf-8")
    temporary = store.root / "EP001" / "project.json.tmp"
    if link_kind == "symlink":
        make_symlink(temporary, outside)
    else:
        os.link(outside, temporary)
    store.apply("EP001", {"title": "正常更新"}, 0)
    assert outside.read_text(encoding="utf-8") == "不能截断"
    assert ProjectStore(store.root).load("EP001").title == "正常更新"
    assert list(temporary.parent.glob(".project-*.json.tmp")) == []


def test_failed_atomic_replace_keeps_old_file_and_cleans_temporary(store, monkeypatch):
    store.create("EP001")
    target = store.root / "EP001" / "project.json"
    before = target.read_bytes()

    def fail(*args):
        raise OSError("模拟替换失败")

    monkeypatch.setattr("duocast.storage.project_store.os.replace", fail)
    with pytest.raises(OSError, match="模拟替换失败"):
        store.apply("EP001", {"title": "等待重试"}, 0)
    assert target.read_bytes() == before
    assert store._dirty == {"EP001"}
    assert list(target.parent.glob(".project-*.json.tmp")) == []


def test_legacy_file_keeps_defaults_and_rejects_mismatched_identity(store):
    directory = store.root / "EP001"
    directory.mkdir()
    target = directory / "project.json"
    target.write_text(json.dumps({"id": "EP001", "title": "旧工程", "revision": 3}), encoding="utf-8")
    project = store.load("EP001")
    assert project.updated_at == ""
    assert project.revision == 3
    target.write_text(json.dumps({"id": "../outside"}), encoding="utf-8")
    with pytest.raises(InvalidProjectInput):
        ProjectStore(store.root).load("EP001")


@pytest.mark.parametrize("project_id", INVALID_IDS)
def test_create_api_rejects_explicit_invalid_id(client, store, project_id):
    response = client.post("/api/projects", json={"id": project_id})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]
    assert store._projects == {}
    assert list(store.root.iterdir()) == []


@pytest.mark.parametrize("method,suffix", [
    ("GET", ""), ("PATCH", ""), ("POST", "/script/generate"),
    ("POST", "/script/rewrite"), ("POST", "/voice/synthesize"),
    ("POST", "/visual/generate"), ("POST", "/render/generate"),
])
@pytest.mark.parametrize("encoded_id", ["NUL", "a%5Cb", "C%3Atemp", "%20", "%00", "%2E%2E"])
def test_all_project_routes_map_invalid_id_to_422(client, method, suffix, encoded_id):
    response = client.request(method, f"/api/projects/{encoded_id}{suffix}", json={"expectedRevision": 0})
    assert response.status_code == 422, response.text
    assert response.json()["detail"] in ("invalid project id", "invalid project path")


def test_symlink_id_maps_to_422_in_api(client, store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    make_symlink(store.root / "EP001", outside, directory=True)
    assert client.get("/api/projects/EP001").status_code == 422
    assert client.post("/api/projects", json={"id": "EP001"}).status_code == 422
    assert client.patch("/api/projects/EP001", json={"expectedRevision": 0}).status_code == 422
    assert client.get("/api/projects").json() == []
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("initial,expected", [
    ([], "EP001"), (["EP002"], "EP003"), (["EP002", "EP010"], "EP011"),
    (["EP009", "EP999"], "EP1000"), (["custom", "uuid-like_id"], "EP001"),
])
def test_default_number_uses_largest_numeric_id_not_count(client, store, initial, expected):
    for project_id in initial:
        assert client.post("/api/projects", json={"id": project_id, "title": "保留"}).status_code == 200
    # 防抖尚未落盘，编号也必须包含内存中的工程。
    assert list(store.root.iterdir()) == []
    response = client.post("/api/projects", json={"title": "新工程"})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == expected
    assert response.json()["revision"] == 0
    assert response.json()["title"] == "新工程"
    for project_id in initial:
        assert store.load(project_id).title == "保留"


def test_default_number_accounts_for_disk_files_and_empty_directories(client, store):
    store.create("EP002", "磁盘工程")
    (store.root / "EP007").mkdir()
    (store.root / "EP020").write_text("占位", encoding="utf-8")
    response = client.post("/api/projects", json={})
    assert response.status_code == 200
    assert response.json()["id"] == "EP021"


def test_default_number_skips_symlink_occupancy(client, store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    make_symlink(store.root / "EP002", outside, directory=True)
    response = client.post("/api/projects", json={})
    assert response.status_code == 200
    assert response.json()["id"] == "EP003"
    assert list(outside.iterdir()) == []


def test_default_number_retries_a_creation_collision(client, store, monkeypatch):
    store.create("EP002")
    create = store.create
    attempted = []

    def collide_once(project_id, title):
        attempted.append(project_id)
        if len(attempted) == 1:
            raise ProjectAlreadyExists("模拟编号已被占用")
        return create(project_id, title)

    monkeypatch.setattr(store, "create", collide_once)
    response = client.post("/api/projects", json={})
    assert response.status_code == 200
    assert response.json()["id"] == "EP004"
    assert attempted == ["EP003", "EP004"]


@pytest.mark.parametrize("from_disk", [False, True])
def test_explicit_duplicate_api_returns_409_without_modification(client, store, from_disk):
    if from_disk:
        store.create("EP002", "原工程")
        store._projects.clear()
    else:
        assert client.post("/api/projects", json={"id": "EP002", "title": "原工程"}).status_code == 200
    before = client.get("/api/projects/EP002").json()
    response = client.post("/api/projects", json={"id": "EP002", "title": "不能覆盖"})
    assert response.status_code == 409
    assert client.get("/api/projects/EP002").json() == before


@pytest.mark.parametrize("changed_id", ["EP002", "../outside", None, False, [], {}])
def test_patch_cannot_change_identity_or_partially_apply(client, store, changed_id):
    original = store.create("EP001", "原工程")
    target = store.root / "EP001" / "project.json"
    before = target.read_bytes()
    response = client.patch("/api/projects/EP001", json={
        "expectedRevision": 0, "id": changed_id, "title": "不能写入",
    })
    assert response.status_code == 422
    assert store.load("EP001") == original
    assert store._dirty == set()
    assert target.read_bytes() == before
    assert store.list_ids() == ["EP001"]


def test_same_id_and_both_field_naming_styles_remain_compatible(client, store):
    store.create("EP001", "原工程")
    internal = store.apply("EP001", {
        "id": "EP001", "script_revisions": [ScriptRevision(id="R1")],
        "current_draft_revision": "R1",
    }, 0)
    assert internal.current_draft_revision == "R1"
    response = client.patch("/api/projects/EP001", json={
        "expectedRevision": 1, "id": "EP001", "title": "改名",
        "currentDraftRevision": "R2", "revision": 99, "updatedAt": "不能伪造",
    })
    assert response.status_code == 200, response.text
    project = response.json()
    assert project["id"] == "EP001"
    assert project["title"] == "改名"
    assert project["currentDraftRevision"] == "R2"
    assert project["revision"] == 2
    assert project["updatedAt"] != "不能伪造"
    assert project["scriptRevisions"][0]["id"] == "R1"
    assert "stageProgress" in project


@pytest.mark.parametrize("expected", [None, -1, True, False, 0.0, 1.0, "0", [], {}, "missing"])
def test_invalid_expected_revision_returns_422_not_conflict(client, store, expected):
    original = store.create("EP001")
    payload = {"title": "不能写入"}
    if expected != "missing":
        payload["expectedRevision"] = expected
    response = client.patch("/api/projects/EP001", json=payload)
    assert response.status_code == 422
    assert store.load("EP001") == original
    assert store._dirty == set()


@pytest.mark.parametrize("patch", [
    {"title": None}, {"title": []}, {"aspect": "square"}, {"voiceBindings": {}},
    {"scriptRevisions": [{"id": "R1", "turns": [{"id": "T1", "speaker": "C"}]}]},
    {"audioTimeline": {"revisionId": "R1", "sampleRate": "invalid"}},
])
def test_patch_validation_errors_are_422_and_atomic(client, store, patch):
    original = store.create("EP001")
    response = client.patch("/api/projects/EP001", json={"expectedRevision": 0, **patch})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"]
    assert store.load("EP001") == original
    assert store._dirty == set()


@pytest.mark.parametrize("title", [None, False, 42, [], {}])
def test_create_title_validation_returns_422_without_consuming_id(client, store, title):
    response = client.post("/api/projects", json={"title": title})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert store.list_ids() == []
    assert client.post("/api/projects", json={}).json()["id"] == "EP001"


@pytest.mark.parametrize("payload", [None, [], "invalid", 12])
def test_non_object_request_bodies_return_422(client, store, payload):
    store.create("EP001")
    assert client.post("/api/projects", json=payload).status_code == 422
    assert client.patch("/api/projects/EP001", json=payload).status_code == 422
    assert store.load("EP001").revision == 0


def test_store_validation_preserves_existing_exception_contract(store):
    project = store.create("EP001")
    with pytest.raises(ValidationError):
        store.apply(project.id, {"aspect": "square"}, 0)
    with pytest.raises(ProjectRevisionConflict):
        store.apply(project.id, {"title": "不能写入"}, 1)
    with pytest.raises(KeyError):
        store.apply("missing", {}, 0)
    with pytest.raises(InvalidProjectInput):
        store.apply(project.id, [], 0)
    assert store.load(project.id) == project


def test_missing_projects_and_revision_conflicts_keep_existing_statuses(client, store):
    assert client.get("/api/projects/missing").status_code == 404
    assert client.patch("/api/projects/missing", json={"expectedRevision": 0}).status_code == 404
    store.create("EP001")
    response = client.patch("/api/projects/EP001", json={"expectedRevision": 1, "title": "不能写入"})
    assert response.status_code == 409
    assert store.load("EP001").revision == 0


def test_setuptools_discovers_root_and_all_duocast_subpackages():
    config = read_configuration(Path(__file__).resolve().parents[1] / "pyproject.toml")
    packages = set(config["tool"]["setuptools"]["packages"])
    assert {
        "duocast", "duocast.api", "duocast.storage", "duocast.domain",
        "duocast.services", "duocast.jobs", "duocast.adapters", "duocast.core",
    } <= packages
    assert all(name == "duocast" or name.startswith("duocast.") for name in packages)
    assert not any(name.startswith("tests") for name in packages)
