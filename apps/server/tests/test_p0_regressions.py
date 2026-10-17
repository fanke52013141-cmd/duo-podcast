"""P0 回归：产物清单并发登记、删除工程时任务队列清理与「任务复活」防护。"""

import asyncio
import json
import threading

from duocast.core.eventbus import EventBus
from duocast.domain.job import JobStatus
from duocast.jobs.manager import JobManager
from duocast.storage.artifacts import ArtifactStore


def _manager(tmp_path) -> JobManager:
    bus = EventBus(log_path=tmp_path / "events" / "events.jsonl", cap=1000)
    return JobManager(tmp_path / "jobs", bus)


def test_artifact_register_is_thread_safe(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    payload = tmp_path / "frame.bin"
    payload.write_bytes(b"pixels")

    def register(index: int) -> None:
        store.register(f"A{index:03d}", "image", str(payload), f"dep-{index}")

    threads = [threading.Thread(target=register, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    manifest = json.loads((tmp_path / "artifacts" / "manifest.json").read_text(encoding="utf-8"))
    ids = {entry["artifactId"] for entry in manifest["artifacts"]}
    assert ids == {f"A{i:03d}" for i in range(24)}
    # 唯一临时文件：并发写完后不留残渣
    assert not list((tmp_path / "artifacts").glob("*.tmp"))


def test_purge_clears_queues_and_blocks_persist(tmp_path):
    manager = _manager(tmp_path)
    first = manager.submit("script.generate", "EPDEL", "api", "tok-1", {})
    second = manager.submit("visual.generate", "EPDEL", "api", "tok-2", {})
    keeper = manager.submit("script.generate", "EPKEEP", "api", "tok-3", {})
    assert len(manager._queues["api"]) == 3

    moved = manager.purge_project("EPDEL", tmp_path / "trash" / "EPDEL")

    assert moved == 2
    assert manager.list("EPDEL") == []
    assert [entry[3].id for entry in manager._queues["api"]._heap] == [keeper.id]
    assert {first.id, second.id} <= manager._discarded

    # 收尾状态机不得把已删工程的 job.json 写回来
    manager.transition(first, JobStatus.CANCELLED)
    assert not (tmp_path / "jobs" / first.id).exists()


def test_worker_skips_job_after_project_deleted(tmp_path):
    async def scenario() -> list[str]:
        manager = _manager(tmp_path)
        executed: list[str] = []

        async def runner(job):
            executed.append(job.id)
            return {"artifactIds": []}

        manager.set_runner(runner)
        job = manager.submit("script.generate", "EPGONE", "api", "tok-9", {})
        manager.purge_project("EPGONE", tmp_path / "trash" / "EPGONE")
        # 模拟入队/删除竞态：purge 之后堆里又出现同一个 Job
        manager._queues["api"].put(job)
        manager.start()
        await asyncio.sleep(0.2)
        await manager.stop()
        return executed

    assert asyncio.run(scenario()) == []
