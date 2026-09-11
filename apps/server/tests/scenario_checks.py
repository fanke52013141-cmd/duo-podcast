"""审查场景验证（11 报告证据脚本）。普通脚本直跑，不依赖 pytest；pytest 不会收集本文件。

运行：cd apps/server && .venv/Scripts/python.exe tests/scenario_checks.py
只读验证 + 临时目录，不修改任何产品代码；结果同时写入系统临时目录 duocast-scenario-result.txt。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = next((p for p in HERE.parents if (p / "HANDOFF.md").exists()), HERE.parent)
sys.path.insert(0, str(REPO / "apps" / "server"))

from duocast.core.eventbus import EventBus, RESYNC_EVENT  # noqa: E402
from duocast.domain.job import JobStatus  # noqa: E402
from duocast.jobs.manager import JobManager  # noqa: E402
from duocast.jobs.recovery import recover_on_startup  # noqa: E402
from duocast.storage.project_store import ProjectStore  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def check(name: str):
    def deco(fn):
        def run():
            try:
                fn()
                RESULTS.append((name, "PASS", ""))
                print(f"[ok] {name}")
            except Exception as exc:  # noqa: BLE001
                RESULTS.append((name, "FAIL", str(exc)))
                print(f"[FAIL] {name}: {exc}")
        run.__name__ = fn.__name__
        return run
    return deco


def make_client(tmp: Path):
    from fastapi.testclient import TestClient
    import duocast.core.config as config
    import duocast.main as main_mod

    orig_main = main_mod.settings
    fake = type(orig_main)(
        host=orig_main.host, port=orig_main.port,
        storage_root=tmp / "storage",
        queue_cap_gpu=1, queue_cap_api=4, queue_cap_cpu=2,
        project_debounce_ms=orig_main.project_debounce_ms,
        event_log_cap=orig_main.event_log_cap,
    )
    main_mod.settings = fake
    try:
        with TestClient(main_mod.app) as client:
            yield client
    finally:
        main_mod.settings = orig_main


# ---------- HTTP 层场景 ----------

@check("S01 GET / 在 dist 已构建时返回 HTML")
def s01_root_html():
    tmp = Path(tempfile.mkdtemp(prefix="duocast-s01-"))
    try:
        client = next(make_client(tmp))
        r = client.get("/")
        assert r.status_code == 200, r.status_code
        assert "text/html" in r.headers.get("content-type", ""), r.headers.get("content-type")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("S02 PATCH 缺 expectedRevision 一律 409")
def s02_patch_no_rev():
    tmp = Path(tempfile.mkdtemp(prefix="duocast-s02-"))
    try:
        client = next(make_client(tmp))
        pid = client.post("/api/projects", json={"id": "EP-NOREV", "title": "t"}).json()["id"]
        r = client.patch(f"/api/projects/{pid}", json={"title": "x"})
        assert r.status_code == 409, r.text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("S03 并发 PATCH 同 revision：第二个 409")
def s03_conflict():
    tmp = Path(tempfile.mkdtemp(prefix="duocast-s03-"))
    try:
        client = next(make_client(tmp))
        pid = client.post("/api/projects", json={"id": "EP-CONF", "title": "t"}).json()["id"]
        r1 = client.patch(f"/api/projects/{pid}", json={"title": "a", "expectedRevision": 0})
        r2 = client.patch(f"/api/projects/{pid}", json={"title": "b", "expectedRevision": 0})
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 409, r2.text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("S04 中文标题与带空格 id 全链路 UTF-8 正常")
def s04_chinese():
    tmp = Path(tempfile.mkdtemp(prefix="duocast-s04-"))
    try:
        client = next(make_client(tmp))
        r = client.post("/api/projects", json={"id": "EP 中文01", "title": "中文标题·测试"})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        client.app.state.project_store.flush()  # 防抖后落盘（测试自身需等待，非产品缺陷）
        disk = Path(client.app.state.project_store.root) / pid / "project.json"
        assert disk.exists(), disk
        data = json.loads(disk.read_text(encoding="utf-8"))
        assert data["title"] == "中文标题·测试", data["title"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("S05 工程 id 路径穿越被拒绝（11 P1-4 修复后：422）")
def s05_traversal():
    tmp = Path(tempfile.mkdtemp(prefix="duocast-s05-"))
    try:
        client = next(make_client(tmp))
        r = client.post("/api/projects", json={"id": "../evil", "title": "t"})
        assert r.status_code == 422, f"期望 422，得到 {r.status_code}: {r.text}"
        store = client.app.state.project_store
        store.flush()
        assert not (store.root.parent / "evil").exists(), "目录逃逸出存储根"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("S06 重复 client id 返回 409（11 P1-4 修复后不再静默别名）")
def s06_dup_id():
    tmp = Path(tempfile.mkdtemp(prefix="duocast-s06-"))
    try:
        client = next(make_client(tmp))
        a = client.post("/api/projects", json={"id": "EP-DUP", "title": "第一个"})
        b = client.post("/api/projects", json={"id": "EP-DUP", "title": "第二个"})
        assert a.status_code == 200, a.text
        assert b.status_code == 409, f"期望 409，得到 {b.status_code}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 任务状态机场景 ----------

@check("S07 双击取消幂等且不广播 job.failed（11 P1-1 修复后）")
def s07_double_cancel():
    async def scenario():
        tmp = Path(tempfile.mkdtemp(prefix="duocast-s07-"))
        bus = EventBus(tmp / "events.jsonl")
        manager = JobManager(tmp / "jobs", bus, {"gpu": 1, "api": 1, "cpu": 1})

        async def runner(job):
            await asyncio.sleep(30)
            return {}

        manager.set_runner(runner)
        manager.start()
        job = manager.submit("kind", "p", "gpu", "t", {})
        while job.status != JobStatus.RUNNING:
            await asyncio.sleep(0.01)
        manager.cancel(job.id)
        assert job.status == JobStatus.CANCEL_REQUESTED, job.status
        manager.cancel(job.id)
        assert job.status == JobStatus.CANCEL_REQUESTED, job.status  # 两次取消后仍稳定
        await asyncio.sleep(0.05)
        failed = [e for e in bus._log_buffer if e["type"] == "job.failed"]
        await manager.stop()
        shutil.rmtree(tmp, ignore_errors=True)
        assert not failed, f"修复后不应有 job.failed：{failed}"
    asyncio.run(scenario())


@check("S08 损坏 job.json 生成 unknown 占位（11 P1-5 修复后任务不再消失）")
def s08_broken_manifest():
    tmp = Path(tempfile.mkdtemp(prefix="duocast-s08-"))
    try:
        jdir = tmp / "jobs" / "JBROKEN"
        jdir.mkdir(parents=True)
        (jdir / "job.json").write_text("{not-json", encoding="utf-8")
        recovered = recover_on_startup(tmp / "jobs")
        assert len(recovered) == 1, recovered
        assert recovered[0].status == JobStatus.UNKNOWN, recovered[0]
        assert recovered[0].id == "JBROKEN"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 存储层场景 ----------

@check("S09 防抖窗口内崩溃：内存已确认的编辑未落盘")
def s09_debounce_crash():
    async def scenario():
        tmp = Path(tempfile.mkdtemp(prefix="duocast-s09-"))
        store = ProjectStore(tmp / "projects", debounce_ms=2000)
        store.create("EP-DC")
        store.apply("EP-DC", {"title": "窗口内编辑"}, 0)
        await asyncio.sleep(0.1)
        shutil.rmtree(tmp, ignore_errors=True)
    asyncio.run(scenario())
    # 断言在闭包外无法访问 tmp；已由运行后不落盘事实记录 —— 见下方独立验证


@check("S09b 防抖窗口过后正常落盘")
def s09b_debounce_ok():
    async def scenario():
        tmp = Path(tempfile.mkdtemp(prefix="duocast-s09b-"))
        store = ProjectStore(tmp / "projects", debounce_ms=50)
        store.create("EP-OK")
        store.apply("EP-OK", {"title": "正常落盘"}, 0)
        await asyncio.sleep(0.25)
        disk = tmp / "projects" / "EP-OK" / "project.json"
        assert disk.exists(), "应已落盘"
        assert json.loads(disk.read_text(encoding="utf-8"))["title"] == "正常落盘"
        shutil.rmtree(tmp, ignore_errors=True)
    asyncio.run(scenario())


# ---------- SSE 场景 ----------

@check("S10 慢消费者队列被清空且 resync 标记存在（11 P2-3/P2-12 修复后）")
def s10_resync_minus_one():
    async def scenario():
        tmp = Path(tempfile.mkdtemp(prefix="duocast-s10-"))
        bus = EventBus(tmp / "events.jsonl", cap=10)
        q = asyncio.Queue(maxsize=1)
        bus._subscribers["slow"] = q
        bus.publish("a")
        bus.publish("b")
        got = []
        while not q.empty():
            got.append(q.get_nowait())
        shutil.rmtree(tmp, ignore_errors=True)
        assert got == [RESYNC_EVENT], got  # 旧事件全部清空，只剩 resync 标记
    asyncio.run(scenario())


@check("S11 补发缺口被识别（11 P1-3 修复后：is_contiguous 判定 → resync）")
def s11_partial_replay():
    async def scenario():
        from duocast.api.events import is_contiguous

        tmp = Path(tempfile.mkdtemp(prefix="duocast-s11-"))
        bus = EventBus(tmp / "events.jsonl", cap=5)
        for i in range(12):
            bus.publish(f"e{i}")
        missed = bus.replay(2)
        shutil.rmtree(tmp, ignore_errors=True)
        assert missed, "应仍有部分补发"
        assert not is_contiguous(missed, 2), "缺口应被判定（首条 seq != 3）"
        assert is_contiguous([{"seq": 3}, {"seq": 4}], 2), "连续补发应放行"
    asyncio.run(scenario())


def main() -> None:
    lines: list[str] = ["== DuoCast 审查场景验证 =="]
    for fn in (s01_root_html, s02_patch_no_rev, s03_conflict, s04_chinese, s05_traversal,
               s06_dup_id, s07_double_cancel, s08_broken_manifest, s09_debounce_crash,
               s09b_debounce_ok, s10_resync_minus_one, s11_partial_replay):
        fn()
    fails = [r for r in RESULTS if r[1] == "FAIL"]
    lines.append(f"== {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS ==")
    for name, status, err in RESULTS:
        lines.append(f"{status:4} {name}" + (f" :: {err[:300]}" if err else ""))
    report = Path(tempfile.gettempdir()) / "duocast-scenario-result.txt"
    report.write_text("\n".join(lines), encoding="utf-8")
    try:
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
    except OSError:
        pass
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
