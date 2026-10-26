"""Isolated HTTP trust-boundary tests; no generation or network services."""
import hashlib
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from duocast.api import motion_packs
from duocast.storage.motion_packs import MotionPackStore


@pytest.fixture
def api(tmp_path):
    artifacts = {}
    for name, kind in (("image", "image"), ("video", "video")):
        path = tmp_path / (name + (".mp4" if kind == "video" else ".png"))
        path.write_bytes(name.encode())
        artifacts[name] = {"path": str(path), "kind": kind,
                           "fileHash": hashlib.sha256(path.read_bytes()).hexdigest()}
    app = FastAPI()
    store = MotionPackStore(tmp_path / "packs", artifacts.get)
    app.state.motion_pack_store = store
    app.include_router(motion_packs.router)
    with TestClient(app) as client:
        yield client, store, artifacts


def draft(artifacts):
    def ref(name):
        return {"artifactId": name, "fileHash": artifacts[name]["fileHash"]}
    return {"name": "Test", "characters": {role: {"characterId": role, "referenceImage": ref("image")} for role in "AB"},
            "cameras": [{"id": "wide", "masterImage": ref("image")}],
            "clips": [{"id": "A-talk", "role": "A", "cameraId": "wide", "state": "speaking",
                       "media": ref("video"), "nativeWidth": 160, "nativeHeight": 96,
                       "frameCount": 25, "usableEndFrame": 25}]}


def create(api):
    client, _, artifacts = api
    response = client.post("/api/motion-packs", json=draft(artifacts))
    assert response.status_code == 201, response.text
    return response.json()


def regions(revision=1):
    return {"expectedRevision": revision,
            "bodyRegions": [{"x": .2, "y": .3, "width": .4, "height": .5}],
            "exclusionRegions": [{"x": .3, "y": .1, "width": .2, "height": .2}],
            "backgroundRegion": {"x": 0, "y": 0, "width": .15, "height": 1}}


def test_create_list_update_versions_and_conflict(api):
    client, _, artifacts = api
    pack = create(api)
    base = "/api/motion-packs/" + pack["id"]
    assert client.get(base).json() == pack
    assert client.get("/api/motion-packs").json() == [pack]
    changed = draft(artifacts)
    changed["name"] = "Changed"
    update = {"expectedRevision": 1, "draft": changed}
    assert client.put(base, json=update).json()["revision"] == 2
    assert client.put(base, json=update).status_code == 409
    assert client.get(base + "/versions/" + pack["version"]).json()["name"] == "Test"
    assert client.get(base + "/readiness").json()["ready"] is False
    assert client.get(base + "/readiness?style=unknown").status_code == 422


@pytest.mark.parametrize("injection", [{"checks": {}}, {"status": "passed"}, {"path": "C:/private.mp4"}, {"revision": 8}, {"reviews": {}}])
def test_create_rejects_client_trust_injection(api, injection):
    client, _, artifacts = api
    assert client.post("/api/motion-packs", json={**draft(artifacts), **injection}).status_code == 422


def test_missing_resources_and_invalid_revision(api):
    client, _, artifacts = api
    assert client.get("/api/motion-packs/absent").status_code == 404
    assert client.put("/api/motion-packs/absent", json={"expectedRevision": 1, "draft": draft(artifacts)}).status_code == 404
    pack = create(api)
    base = "/api/motion-packs/" + pack["id"]
    for revision in (True, "1", 0):
        assert client.put(base, json={"expectedRevision": revision, "draft": draft(artifacts)}).status_code == 422
    assert client.get(base + "/versions/absent").status_code == 404
    assert client.post(base + "/clips/missing/inspect", json=regions()).status_code == 404


def test_check_and_review_gates_and_server_only_conclusion(api, monkeypatch):
    client, _, artifacts = api
    pack = create(api)
    base = f'/api/motion-packs/{pack["id"]}/clips/A-talk'
    review = {"expectedRevision": 1, "accepted": True, "reason": "Viewed"}
    assert client.post(base + "/review", json=review).status_code == 422
    assert client.post(base + "/inspect", json={**regions(), "report": {"status": "passed"}}).status_code == 422
    assert client.post(base + "/inspect", json={**regions(), "path": "private"}).status_code == 422
    observed = {}

    def detector(path, **kwargs):
        observed.update(kwargs)
        assert str(path) == artifacts["video"]["path"]
        return {"inputFileHash": artifacts["video"]["fileHash"], "checksVersion": "test-stub",
                "status": "needs_review", "native": {"width": 160, "height": 96, "fps": 25, "frames": 25, "durationSec": 1},
                "evidence": {"completeSourceFramesDecoded": 25, "analyzedStartFrame": 0, "analyzedEndFrame": 25}}

    monkeypatch.setattr(motion_packs, "inspect_motion_clip", detector)
    checked = client.post(base + "/inspect", json=regions())
    assert checked.status_code == 200, checked.text
    assert checked.json()["checks"]["A-talk"]["report"]["status"] == "needs_review"
    assert observed["expected_file_hash"] == artifacts["video"]["fileHash"]
    assert (observed["usable_start_frame"], observed["usable_end_frame"]) == (0, 25)
    assert client.post(base + "/review", json=review).status_code == 409
    review["expectedRevision"] = 2
    assert client.post(base + "/review", json={**review, "reason": ""}).status_code == 422
    assert client.post(base + "/review", json={**review, "accepted": "true"}).status_code == 422
    accepted = client.post(base + "/review", json=review)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["reviews"]["A-talk"]["accepted"] is True


def test_changed_pack_during_detection_rejects_stale_report(api, monkeypatch):
    client, store, artifacts = api
    pack = create(api)
    def detector(*args, **kwargs):
        store.update(pack["id"], draft(artifacts), 1)
        return {}
    monkeypatch.setattr(motion_packs, "inspect_motion_clip", detector)
    result = client.post(f'/api/motion-packs/{pack["id"]}/clips/A-talk/inspect', json=regions())
    assert result.status_code == 409
    assert store.load(pack["id"]).checks == {}


def test_regions_are_explicit_bounded_and_nested_extra_forbidden(api):
    client, _, _ = api
    pack = create(api)
    url = f'/api/motion-packs/{pack["id"]}/clips/A-talk/inspect'
    for key in ("bodyRegions", "exclusionRegions", "backgroundRegion"):
        body = regions()
        del body[key]
        assert client.post(url, json=body).status_code == 422
    body = regions()
    body["bodyRegions"] *= 9
    assert client.post(url, json=body).status_code == 422
    body = regions()
    body["bodyRegions"][0]["path"] = "injected"
    assert client.post(url, json=body).status_code == 422


def test_actual_static_media_is_inspected_and_cannot_be_approved(api):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg tools unavailable")
    client, _, artifacts = api
    path = artifacts["video"]["path"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x96:r=25:d=1", "-c:v", "libx264", path], check=True)
    from pathlib import Path
    artifacts["video"]["fileHash"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    pack = create(api)
    base = f'/api/motion-packs/{pack["id"]}/clips/A-talk'
    response = client.post(base + "/inspect", json=regions())
    assert response.status_code == 200, response.text
    report = response.json()["checks"]["A-talk"]["report"]
    assert "NO_STRUCTURAL_MOTION" in report["hardFailures"]
    assert client.post(base + "/review", json={"expectedRevision": 2, "accepted": True, "reason": "Ignore failure"}).status_code == 422


def test_selected_static_tail_does_not_borrow_motion_from_unselected_head(api):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg tools unavailable")
    client, _, artifacts = api
    path = artifacts["video"]["path"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x96:r=25:d=1",
                    "-f", "lavfi", "-i", "color=c=blue:s=160x96:r=25:d=1",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[out]", "-map", "[out]", "-c:v", "libx264", path], check=True)
    from pathlib import Path
    artifacts["video"]["fileHash"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    content = draft(artifacts)
    content["clips"][0].update(frameCount=50, usableStartFrame=25, usableEndFrame=50)
    pack = client.post("/api/motion-packs", json=content).json()
    base = f'/api/motion-packs/{pack["id"]}/clips/A-talk'
    response = client.post(base + "/inspect", json=regions())
    assert response.status_code == 200, response.text
    report = response.json()["checks"]["A-talk"]["report"]
    assert report["native"]["frames"] == 50
    assert report["evidence"]["analyzedStartFrame"] == 25
    assert report["evidence"]["analyzedEndFrame"] == 50
    assert "NO_STRUCTURAL_MOTION" in report["hardFailures"]


def test_changed_physical_media_during_detection_is_not_recorded(api, monkeypatch):
    client, store, artifacts = api
    pack = create(api)
    def detector(*args, **kwargs):
        from pathlib import Path
        Path(artifacts["video"]["path"]).write_bytes(b"changed during check")
        return {"inputFileHash": artifacts["video"]["fileHash"], "checksVersion": "test-stub", "status": "needs_review"}
    monkeypatch.setattr(motion_packs, "inspect_motion_clip", detector)
    response = client.post(f'/api/motion-packs/{pack["id"]}/clips/A-talk/inspect', json=regions())
    assert response.status_code == 422
    assert store.load(pack["id"]).checks == {}


def test_inspection_concurrency_bound_and_cancel_waits_for_worker(api, monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace
    _, store, _ = api
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    active, maximum = 0, 0
    lock = threading.Lock()
    def inspection(*args):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        started.set()
        assert release.wait(3)
        with lock:
            active -= 1
        finished.set()
        return {}
    monkeypatch.setattr(motion_packs, "_inspect", inspection)
    async def scenario():
        state = SimpleNamespace(motion_pack_store=store)
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        payload = motion_packs.InspectClip.model_validate(regions())
        tasks = [asyncio.create_task(motion_packs.inspect_clip("pack", "clip", payload, request)) for _ in range(4)]
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(.03)
        assert maximum == 2
        tasks[0].cancel()
        await asyncio.sleep(.02)
        assert not tasks[0].done()
        tasks[0].cancel()
        await asyncio.sleep(.02)
        assert not tasks[0].done()
        assert not finished.is_set()
        release.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert maximum == 2
        assert finished.is_set()
    asyncio.run(scenario())
