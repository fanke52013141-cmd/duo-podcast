"""端到端冒烟：走通 01 §3 五阶段全流程 + 资产 + 机器设置（对 127.0.0.1:8100）。"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8100/api"


def call(method: str, path: str, payload: dict | None = None):
    req = urllib.request.Request(f"{BASE}{path}", method=method,
                                 data=json.dumps(payload or {}).encode("utf-8") if payload is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_job(job_id: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = call("GET", f"/jobs/{job_id}")
        if j["status"] in ("succeeded", "failed", "cancelled"):
            return j
        time.sleep(0.4)
    raise TimeoutError(f"job {job_id} not finished")


def main() -> None:
    # 0) 机器设置（新端点）
    machine = call("GET", "/machine")
    assert "capabilities" in machine and "queueCaps" in machine, f"machine shape: {machine.keys()}"
    print("[ok] machine:", json.dumps({k: machine[k] for k in ("queueCaps",)}, ensure_ascii=False))

    pid = f"EP{int(time.time()) % 10000:04d}"
    proj = call("POST", "/projects", {"id": pid, "title": "冒烟测试"})
    print("[ok] project:", proj["id"], "rev", proj["revision"])

    # 1) 脚本生成
    j = call("POST", f"/projects/{pid}/script/generate",
             {"clientToken": "sm-gen", "sourceInput": {"kind": "article", "content": "第一段\n第二段\n第三段"}})
    j = wait_job(j["jobId"])
    assert j["status"] == "succeeded", j
    proj = call("GET", f"/projects/{pid}")
    rev = proj["currentDraftRevision"]
    assert len(proj["scriptRevisions"][-1]["turns"]) == 3, proj["scriptRevisions"][-1]["turns"]
    print("[ok] script.generate:", rev, "turns=3 stage=", proj["stageProgress"]["script"])

    # 2) 确认脚本
    proj = call("PATCH", f"/projects/{pid}",
                {"approvals": [*proj["approvals"], {"kind": "script", "inputRevisionId": rev, "spec": {},
                                                    "decision": "accepted", "reasonTarget": None, "note": "sm",
                                                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}],
                 "expectedRevision": proj["revision"]})
    print("[ok] confirm script: approvals=", len(proj["approvals"]), "stage=", proj["stageProgress"]["script"])

    # 3) 配音合成
    j = call("POST", f"/projects/{pid}/voice/synthesize",
             {"clientToken": "sm-tts", "revisionId": rev,
              "voiceBindings": {"A": {"id": "VB-A", "characterId": "A", "providerProfileId": "mock-tts", "modelId": "m1"},
                                "B": {"id": "VB-B", "characterId": "B", "providerProfileId": "mock-tts", "modelId": "m1"}}})
    j = wait_job(j["jobId"])
    assert j["status"] == "succeeded", j
    proj = call("GET", f"/projects/{pid}")
    tl = proj["audioTimeline"]
    assert tl and tl["sampleCount"] > 0 and len(tl["units"]) == 3, tl
    print("[ok] tts.synthesize:", tl["sampleCount"], "samples units=", len(tl["units"]))

    # 4) 确认配音
    proj = call("PATCH", f"/projects/{pid}",
                {"approvals": [*proj["approvals"], {"kind": "voice", "inputRevisionId": tl["revisionId"], "spec": {},
                                                    "decision": "accepted", "reasonTarget": None, "note": "sm",
                                                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}],
                 "expectedRevision": proj["revision"]})
    print("[ok] confirm voice: stage=", proj["stageProgress"]["voice"])

    # 5) 画面生成
    j = call("POST", f"/projects/{pid}/visual/generate", {"clientToken": "sm-img", "aspect": "landscape", "prompt": "p"})
    j = wait_job(j["jobId"])
    proj = call("GET", f"/projects/{pid}")
    assert len(proj["visualVariants"]) == 1, proj["visualVariants"]
    print("[ok] visual.generate:", proj["visualVariants"][0]["id"], "stage=", proj["stageProgress"]["visual"])

    # 6) 确认样片
    proj = call("PATCH", f"/projects/{pid}",
                {"approvals": [*proj["approvals"], {"kind": "sample", "inputRevisionId": rev, "spec": {},
                                                    "decision": "accepted", "reasonTarget": None, "note": "sm",
                                                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}],
                 "expectedRevision": proj["revision"]})
    print("[ok] confirm sample: stage=", proj["stageProgress"]["visual"])

    # 7) 渲染导出
    j = call("POST", f"/projects/{pid}/render/generate",
             {"clientToken": "sm-render", "revisionId": tl["revisionId"], "resolution": "1920x1080", "fps": 30})
    j = wait_job(j["jobId"])
    proj = call("GET", f"/projects/{pid}")
    assert j['result']['simulated'] is True, j
    assert proj["outputVersion"] is None, proj
    assert j['result']['artifactIds'] == [], j
    print("[ok] renders: demo completed without claiming real video")

    # 8) 资产登记
    arts = call("GET", "/artifacts")["artifacts"]
    kinds = {a["kind"] for a in arts}
    assert not any(a['artifactId'] in j['result']['artifactIds'] for a in arts)
    print("[ok] artifacts:", len(arts), "items kinds=", sorted(kinds))

    # 9) 机器设置保存
    saved = call("PATCH", "/machine", {"vramAllocation": {"textApi": 4, "imageApi": 4, "tts": 2, "video": 8}})
    assert saved["vramAllocation"]["textApi"] == 4, saved
    print("[ok] machine save:", json.dumps(saved["vramAllocation"], ensure_ascii=False))

    # 10) 提供方测试
    t = call("POST", "/providers/tts/test", {})
    assert t["ok"], t
    print("[ok] provider test:", t["provider"])

    print("\nALL PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: {exc}")
        sys.exit(1)
