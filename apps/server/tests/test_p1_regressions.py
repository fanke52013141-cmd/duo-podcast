"""P1 回归：空读法、输出指针同步、合成未生效标记、损坏任务的占位保留。"""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from duocast.api.render import select_version
from duocast.core.eventbus import EventBus
from duocast.domain.job import Job, JobStatus
from duocast.domain.project import Line, Project, ScriptRevision, Turn
from duocast.jobs.manager import JobManager
from duocast.main import app as production_app
from duocast.services.render_svc import apply_output_version
from duocast.services.voice_svc import apply_pronunciation, synthesize_timeline
from duocast.storage.project_store import ProjectStore


class AssetTTS:
    capabilities = {"simulated": True}

    def __init__(self) -> None:
        self.index = 0

    async def synthesize(self, request):
        self.index += 1
        return {"sampleRate": 48000, "sampleCount": 48000 + self.index,
                "audioAssetId": f"AUD-{self.index}"}


def _fixture(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    project = store.create("p1-cases")
    revision = ScriptRevision(id="R1", turns=[
        Turn(id="T1", speaker="A", lines=[Line(id="L1", spoken_text="第一句")]),
        Turn(id="T2", speaker="B", lines=[Line(id="L2", spoken_text="第二句")]),
    ])
    saved = store.apply(project.id, {"script_revisions": [revision], "current_draft_revision": "R1"}, 0)
    return store, saved, revision


def test_empty_read_is_ignored_instead_of_deleting_text():
    texts = ["我们用 AI 做播客"]
    assert apply_pronunciation(texts, [{"term": "AI", "read": ""}]) == texts
    assert apply_pronunciation(texts, [{"term": "AI", "read": "诶爱"}]) == ["我们用 诶爱 做播客"]
    assert apply_pronunciation(texts, [{"term": "", "read": "空"}]) == texts


def test_select_version_keeps_all_output_pointers_in_sync(tmp_path):
    store, project, _ = _fixture(tmp_path)
    first = apply_output_version(store, project, "R1", {"artifactIds": ["VID-1"], "meta": {"path": "a.mp4"}})
    second = apply_output_version(store, store.load(project.id), "R1", {"artifactIds": ["VID-2"], "meta": {"path": "b.mp4"}})
    assert first != second

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_store=store)))
    asyncio.run(select_version(project.id, {"version": first}, request))

    saved = store.load(project.id)
    assert saved.selected_output_version == first
    # 关键：过期判断读 output_version，导出读 output_manifest，三者必须指向同一版本
    assert saved.output_version == first
    assert saved.output_manifest["version"] == first
    assert saved.output_manifest["artifactIds"] == ["VID-1"]


def test_full_synthesis_is_marked_not_applied_when_script_changed(tmp_path):
    store, project, revision = _fixture(tmp_path)
    # 合成开始后脚本被改动：当前草稿已推进到 R2，任务仍按 R1 快照提交
    store.apply(project.id, {"current_draft_revision": "R2"}, store.load(project.id).revision)
    timeline = asyncio.run(synthesize_timeline(store, AssetTTS(), project, revision, {}))
    assert timeline.applied is False
    # 结果仍进历史，但不替换当前时间轨
    saved = store.load(project.id)
    assert saved.audio_timeline is None
    assert saved.audio_history and saved.audio_history[-1].revision_id == "R1"


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


def test_jobs_list_survives_placeholder_job_with_broken_project_id(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    store.create("EPOK")
    manager = JobManager(tmp_path / "jobs", EventBus(log_path=tmp_path / "events.jsonl", cap=100))
    manager.restore([
        Job(id="JGOOD", project_id="EPOK", kind="renders", status=JobStatus.SUCCEEDED),
        # 损坏清单的占位任务：project_id 为空，store.load 会抛 InvalidProjectInput
        Job(id="JBROKEN", project_id="", kind="unknown", status=JobStatus.UNKNOWN),
    ])

    app = FastAPI(exception_handlers=production_app.exception_handlers)
    app.router.routes.extend(_flatten_api_routes(production_app.routes))
    app.state.project_store = store
    app.state.job_manager = manager
    with TestClient(app) as client:
        response = client.get("/api/jobs")
        assert response.status_code == 200
        ids = [item["id"] for item in response.json()]
        # 占位任务不能把整表打挂，也不能凭空消失
        assert set(ids) == {"JGOOD", "JBROKEN"}
