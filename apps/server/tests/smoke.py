"""后端最小闭环冒烟测试（06 §11 起步）：健康检查 → 建工程 → 脚本生成 Job → 落盘 → 冲突保护。

存储落在临时目录，运行结束自动清理，不污染开发数据。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["DUOCAST_STORAGE"] = tempfile.mkdtemp(prefix="duocast-smoke-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "duocast"))

from fastapi.testclient import TestClient  # noqa: E402

from duocast.main import app  # noqa: E402


def wait_job_done(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.1)
    raise TimeoutError(f"job {job_id} not finished")


def test_smoke() -> None:
    with TestClient(app) as client:
        # 1. 健康检查
        assert client.get("/api/health").json()["ok"] is True

        # 2. 能力门控
        caps = client.get("/api/providers/capabilities").json()
        assert caps["textApi"]["ready"] and caps["tts"]["ready"]

        # 3. 创建工程
        created = client.post("/api/projects", json={"id": "EP-SMOKE", "title": "冒烟测试"}).json()
        assert created["revision"] == 0

        # 4. 生成脚本（Job 异步完成，结果登记为候选版本）
        resp = client.post("/api/projects/EP-SMOKE/script/generate", json={
            "clientToken": "smoke-1",
            "sourceInput": {"kind": "article", "content": "第一段内容。\n第二段内容。"},
        }).json()
        job = wait_job_done(client, resp["jobId"])
        assert job["status"] == "succeeded", job

        project = client.get("/api/projects/EP-SMOKE").json()
        assert project["currentDraftRevision"].startswith("R")
        assert len(project["scriptRevisions"]) == 1
        turns = project["scriptRevisions"][0]["turns"]
        assert len(turns) == 2 and turns[0]["speaker"] == "A" and turns[1]["speaker"] == "B"

        # 5. 幂等：相同 clientToken 只创建一项任务
        again = client.post("/api/projects/EP-SMOKE/script/generate", json={
            "clientToken": "smoke-1",
            "sourceInput": {"kind": "article", "content": "第一段内容。\n第二段内容。"},
        }).json()
        assert again["jobId"] == resp["jobId"]

        # 6. expectedRevision 冲突保护（旧 revision 覆盖 → 409）
        stale = client.patch("/api/projects/EP-SMOKE", json={"title": "旧页面写入", "expectedRevision": 0})
        assert stale.status_code == 409, stale.text
        ok = client.patch("/api/projects/EP-SMOKE", json={"title": "新标题", "expectedRevision": 1})
        assert ok.status_code == 200 and ok.json()["revision"] == 2

        print("SMOKE PASS: health / capabilities / project / script job / idempotency / revision conflict")


if __name__ == "__main__":
    try:
        test_smoke()
    finally:
        shutil.rmtree(os.environ["DUOCAST_STORAGE"], ignore_errors=True)
