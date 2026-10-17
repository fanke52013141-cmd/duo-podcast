"""软删除回归：工程与任务记录同处一个回收站目录，且不留下幽灵任务。

覆盖 `DELETE /api/projects/{id}` 与 `JobManager.purge_project`；
工程 ID 复用后阶段①误显示「已生成」的根因即任务残留，故同时验证任务随迁与列表过滤。
"""

import asyncio
from contextlib import asynccontextmanager, suppress

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from duocast.core.eventbus import EventBus
from duocast.jobs.manager import JobManager
from duocast.main import app as production_app
from duocast.storage.project_store import ProjectStore


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


@pytest.fixture
def wired(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=60_000)
    bus = EventBus(log_path=tmp_path / "events" / "events.jsonl", cap=1000)
    manager = JobManager(tmp_path / "jobs", bus)

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

    app = FastAPI(lifespan=isolated_lifespan, exception_handlers=production_app.exception_handlers)
    app.router.routes.extend(_flatten_api_routes(production_app.routes))
    app.state.project_store = store
    app.state.job_manager = manager
    app.state.template_store = None
    with TestClient(app) as client:
        yield client, store, manager, tmp_path


def test_delete_moves_project_and_jobs_to_trash(wired):
    client, store, manager, tmp_path = wired
    store.create("EPDEL", "删除测试")
    job = manager.submit("script.generate", "EPDEL", "api", "ctok-1", {"kind": "topic", "content": "带饭"})

    res = client.delete("/api/projects/EPDEL")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["deleted"] == "EPDEL"
    assert body["jobsMoved"] == 1

    assert client.get("/api/projects/EPDEL").status_code == 404
    trash = tmp_path / "trash"
    assert trash.is_dir()
    moved_dirs = list(trash.iterdir())
    assert len(moved_dirs) == 1
    assert (moved_dirs[0] / "project.json").is_file()
    assert (moved_dirs[0] / "jobs" / job.id).is_dir()
    # 内存与磁盘都不再残留
    assert manager.get(job.id) is None
    assert client.delete("/api/projects/EPDEL").status_code == 404


def test_list_jobs_filters_orphan_jobs(wired):
    client, store, manager, _ = wired
    store.create("EPKEEP", "保留")
    alive = manager.submit("script.generate", "EPKEEP", "api", "ctok-a", {"kind": "topic", "content": "a"})
    orphan = manager.submit("script.generate", "EPGHOST", "api", "ctok-b", {"kind": "topic", "content": "b"})

    ids = [j["id"] for j in client.get("/api/jobs").json()]
    assert alive.id in ids
    assert orphan.id not in ids
