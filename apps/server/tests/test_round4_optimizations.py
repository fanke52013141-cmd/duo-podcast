"""第四轮全流程优化回归（2026-10-06 实测发现的修复）。

覆盖：mock 双人脚本生成（三入口 A/B 交替 + ContentBrief）、mock 改写非空差异、
全局角色库 API（播种 / 新建 / 图片上传）、阶段④底图上传 → 主图变体。
只使用临时目录与隔离 app.state，不启动生产 lifespan、不访问外部服务。
"""

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from duocast.adapters.mock import MockTextProvider
from duocast.main import app as production_app
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.characters import CharacterStore
from duocast.storage.project_store import ProjectStore

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"rest-of-fake-png"


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
    characters = CharacterStore(tmp_path / "characters")
    artifacts = ArtifactStore(tmp_path / "artifacts")

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
    app.state.character_store = characters
    app.state.artifacts = artifacts
    app.state.job_manager = __import__("types").SimpleNamespace(list=lambda project_id=None: [])
    return TestClient(app), store, characters, artifacts


def _has_aba(turns) -> bool:
    seq = [t["speaker"] for t in turns]
    return any(seq[i] == "A" and seq[i + 1] == "B" and seq[i + 2] == "A"
               for i in range(len(seq) - 2))


# ---------- mock 脚本生成 ----------

def test_mock_topic_generates_alternating_dialogue():
    r = MockTextProvider()._build_script("AI 会不会取代真人主播？我们聊聊。", "", "topic")
    assert len(r["turns"]) >= 3
    assert r["turns"][0]["speaker"] == "A"
    assert _has_aba(r["turns"])
    speakers = {t["speaker"] for t in r["turns"]}
    assert speakers == {"A", "B"}


def test_mock_article_long_content_alternates():
    content = "。".join(f"这是第{i}句话，讲一个具体的观点。" for i in range(1, 9))
    r = MockTextProvider()._build_script(content, "", "article")
    assert len(r["turns"]) >= 3
    assert _has_aba(r["turns"])


def test_mock_script_kind_parses_speaker_markers():
    r = MockTextProvider()._build_script("A: 大家好。\nB: 聊点具体的。\nA: 先说更新。", "", "script")
    seq = [(t["speaker"], t["lines"][0]["displayText"]) for t in r["turns"]]
    assert seq[0] == ("A", "大家好。")
    assert seq[1] == ("B", "聊点具体的。")
    assert _has_aba(r["turns"])
    # brief 不带说话人标记
    assert not r["contentBrief"]["coreQuestion"].startswith("A:")


def test_mock_content_brief_filled():
    r = MockTextProvider()._build_script("核心问题是什么？第一个观点。第二个观点。含数字 3 的事实。", "", "article")
    brief = r["contentBrief"]
    assert brief["coreQuestion"]
    assert brief["keyPoints"]
    assert brief["speakerDuties"]


def test_build_script_revision_carries_content_brief(tmp_path):
    """提供方返回的 contentBrief 必须落进 ScriptRevision（此前被丢弃，检查器全显示「—」）。"""
    from duocast.services.script_svc import build_script_revision
    from duocast.storage.project_store import ProjectStore as PS

    store = PS(tmp_path / "projects", debounce_ms=60_000)
    proj = store.create("EPBRIEF01", title="brief 贯通")
    p = MockTextProvider()
    result = p._build_script("核心问题是什么？观点一。观点二。", "", "article")
    rev = build_script_revision(proj, p, result)
    assert rev.content_brief.core_question
    assert rev.content_brief.key_points
    assert rev.content_brief.speaker_duties


def test_mock_rewrite_all_modes_nonempty_diff():
    p = MockTextProvider()

    async def run():
        for mode in ("rewrite", "casual", "probe", "dedupe", "trim"):
            out = await p.rewrite({
                "turns": [{"id": "T01", "lines": [{"id": "L001", "displayText": "一句普通的话。", "spokenText": "一句普通的话。"}]}],
                "turnIds": ["T01"], "mode": mode,
            })
            assert out["changedTurnIds"], f"mode={mode} 返回空差异"

    asyncio.run(run())


# ---------- 角色库 API ----------

def test_characters_seeded_and_create(tmp_path):
    client, _, characters, _ = make_client(tmp_path)
    listed = client.get("/api/characters").json()
    assert [c["id"] for c in listed][:2] == ["char-lilei", "char-hanmeimei"]
    created = client.post("/api/characters", json={"name": "新角色", "speaker": "B", "role": "嘉宾"}).json()
    assert created["id"].startswith("char-")
    assert created["speaker"] == "B"
    assert client.get("/api/characters").json()[-1]["name"] == "新角色"


def test_character_image_upload_and_reject(tmp_path):
    client, _, characters, _ = make_client(tmp_path)
    bad = client.post("/api/characters/char-lilei/image", files={"file": ("x.txt", b"not-an-image")})
    assert bad.status_code == 422
    ok = client.post("/api/characters/char-lilei/image", files={"file": ("a.png", PNG_BYTES)})
    assert ok.status_code == 200, ok.text
    web_path = ok.json()["imagePath"]
    assert web_path.startswith("/api/assets/images/")
    # 图片可回读
    got = client.get(web_path)
    assert got.status_code == 200 and got.content == PNG_BYTES
    # 落库
    stored = [c for c in client.get("/api/characters").json() if c["id"] == "char-lilei"][0]
    assert stored["imagePath"] == web_path


def test_read_image_rejects_bad_names(tmp_path):
    client, *_ = make_client(tmp_path)
    assert client.get("/api/assets/images/..%5Cevil").status_code in (404, 422)
    assert client.get("/api/assets/images/not-exist.png").status_code == 404


# ---------- 阶段④底图上传 ----------

def test_upload_master_creates_variant(tmp_path):
    client, store, _, artifacts = make_client(tmp_path)
    proj = client.post("/api/projects", json={}).json()
    pid = proj["id"]
    before = client.get(f"/api/projects/{pid}").json()["visualVariants"]
    resp = client.post(f"/api/projects/{pid}/visual/upload-master",
                       files={"file": ("two-shot.png", PNG_BYTES)})
    assert resp.status_code == 200, resp.text
    variant_id = resp.json()["variantId"]
    project = client.get(f"/api/projects/{pid}").json()
    variants = project["visualVariants"]
    assert len(variants) == len(before) + 1
    v = next(v for v in variants if v["id"] == variant_id)
    assert v["imageApiConfigRef"] == "upload"
    assert v["masterImage"]["path"]
    assert v["masterImage"]["artifactId"].startswith("IMG-up-")
    # 资产登记可查
    arts = client.get("/api/artifacts").json()["artifacts"]
    assert any(a["artifactId"] == v["masterImage"]["artifactId"] for a in arts)
    # 非图片内容拒绝
    bad = client.post(f"/api/projects/{pid}/visual/upload-master", files={"file": ("x.png", b"junk")})
    assert bad.status_code == 422
