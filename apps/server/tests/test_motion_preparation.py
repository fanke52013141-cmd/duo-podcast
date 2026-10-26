"""Contract tests use an explicit fake HTTP engine; no mock generation is claimed."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_motion_pack_store import setup_pack
from duocast.api.motion_preparation import router
from duocast.core.eventbus import EventBus
from duocast.domain.job import Job
from duocast.jobs.manager import JobManager
from duocast.services.motion_preparation import MotionPreparationService, PreparationRequest
from duocast.storage.motion_packs import MotionPackConflict, InvalidMotionPack


@pytest.fixture
def service(setup_pack, tmp_path):
    store, artifacts, pack, _ = setup_pack
    provider = SimpleNamespace(capabilities={"simulated": False}, base_url="http://fake-engine",
                               input_dir=tmp_path / "engine-input")
    manager = JobManager(tmp_path / "jobs", EventBus(tmp_path / "events.jsonl"))
    return MotionPreparationService(store, artifacts, manager, provider, poll_interval=0, timeout_sec=.02), pack


def request(pack, **kwargs):
    return PreparationRequest(expectedRevision=pack.revision, clientToken="request-one", cameraId="dual",
                              role="A", prompt="Whole rigid lampshade rolls five degrees", **kwargs)


def test_frozen_i2v_owner_idempotency_and_local_camera_job(service):
    svc, pack = service
    payload = request(pack)
    job = svc.submit(pack.id, payload)
    assert job.owner_type == "motionPack" and job.owner_id == pack.id and not job.project_id
    assert svc.submit(pack.id, payload).id == job.id
    workflow = job.input_snapshot["workflow"]
    assert "WanVideoImageToVideoEncode" in {n["class_type"] for n in workflow.values()}
    assert not any("multitalk" in key for node in workflow.values() for key in node["inputs"])
    assert "audio" not in workflow["229"]["inputs"]
    assert "Whole rigid lampshade" in workflow["135"]["inputs"]["positive_prompt"]
    changed = payload.model_copy(update={"seed": 20})
    with pytest.raises(MotionPackConflict, match="TOKEN_INPUT_MISMATCH"):
        svc.submit(pack.id, changed)
    local = payload.model_copy(update={"camera_id": "otsB", "role": "B", "client_token": "retry-B"})
    assert svc.submit(pack.id, local).id != job.id
    assert svc.store.load(pack.id).revision == pack.revision


def test_invalid_subject_stale_revision_and_mock_provider_rejected(service):
    svc, pack = service
    with pytest.raises(InvalidMotionPack):
        svc.submit(pack.id, request(pack).model_copy(update={"camera_id": "otsB"}))
    with pytest.raises(MotionPackConflict):
        svc.submit(pack.id, request(pack).model_copy(update={"expected_revision": 999}))
    svc.provider.capabilities = {"simulated": True}
    with pytest.raises(RuntimeError, match="模拟"):
        svc.submit(pack.id, request(pack))


def test_restart_known_prompt_never_resubmits_and_unknown_submission_blocks(service):
    svc, pack = service
    job = svc.submit(pack.id, request(pack))
    calls = []
    def engine(req):
        calls.append(req.method + " " + req.url.path)
        if req.url.path == "/object_info":
            return httpx.Response(200, json={n["class_type"]: {} for n in job.input_snapshot["workflow"].values()})
        if req.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "persisted-task"})
        return httpx.Response(200, json={})
    svc.transport = httpx.MockTransport(engine)
    with pytest.raises(TimeoutError):
        asyncio.run(svc.run(job))
    assert job.provider_task_id == "persisted-task"
    recovered = Job.model_validate_json((svc.manager.root / job.id / "job.json").read_text(encoding="utf-8"))
    with pytest.raises(TimeoutError):
        asyncio.run(svc.run(recovered))
    assert calls.count("POST /prompt") == 1
    recovered.completed_calls["motionPreparation"].pop("promptId")
    with pytest.raises(RuntimeError, match="SUBMISSION_UNKNOWN"):
        asyncio.run(svc.run(recovered))
    assert calls.count("POST /prompt") == 1


def test_transport_ambiguity_persists_intent_without_resample(service):
    svc, pack = service
    job = svc.submit(pack.id, request(pack))
    calls = []
    def engine(req):
        if req.url.path == "/object_info":
            return httpx.Response(200, json={n["class_type"]: {} for n in job.input_snapshot["workflow"].values()})
        calls.append(req.url.path)
        raise httpx.ReadTimeout("lost response", request=req)
    svc.transport = httpx.MockTransport(engine)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(svc.run(job))
    with pytest.raises(RuntimeError, match="SUBMISSION_UNKNOWN"):
        asyncio.run(svc.run(job))
    assert calls == ["/prompt"]


def test_missing_real_workflow_fails_before_submission(service):
    svc, pack = service
    job = svc.submit(pack.id, request(pack))
    svc.transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    with pytest.raises(RuntimeError, match="WORKFLOW_UNAVAILABLE"):
        asyncio.run(svc.run(job))
    assert not job.provider_task_id and not job.completed_calls


@pytest.mark.parametrize("output", [{}, {"filename": "../escape.mp4", "type": "output"},
                                  {"filename": "real.mp4", "type": "output", "subfolder": "../outside"}])
def test_missing_or_unsafe_output_never_becomes_completed_candidate(service, output):
    svc, pack = service
    job = svc.submit(pack.id, request(pack))
    from duocast.domain.motion_pack import content_hash
    record = {"submissionIntent": True, "workflowHash": content_hash(job.input_snapshot["workflow"]), "promptId": "saved"}
    job.completed_calls["motionPreparation"] = record
    def engine(req):
        return httpx.Response(200, json={"saved": {"status": {"status_str": "success"},
            "outputs": {"229": {"gifs": [output]}}}})
    svc.transport = httpx.MockTransport(engine)
    with pytest.raises(RuntimeError):
        asyncio.run(svc.run(job))
    assert not record.get("result")
    assert svc.artifacts.by_id("VID-MOTION-" + job.id) is None


def test_api_queues_real_owned_job_and_rejects_injected_ready(service):
    svc, pack = service
    app = FastAPI()
    app.include_router(router)
    app.state.motion_preparation_service = svc
    with TestClient(app) as client:
        payload = request(pack).model_dump(mode="json", by_alias=True)
        result = client.post(f"/api/motion-packs/{pack.id}/prepare", json=payload)
        assert result.status_code == 202, result.text
        assert result.json()["ownerType"] == "motionPack"
        assert client.post(f"/api/motion-packs/{pack.id}/prepare", json={**payload, "ready": True}).status_code == 422


def test_actual_video_decode_registration_and_checkpoint_recovery(service, tmp_path):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real media tools required")
    svc, pack = service
    payload = request(pack).model_copy(update={"duration_sec": 2})
    job = svc.submit(pack.id, payload)
    source = tmp_path / "real-fixture.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x54:rate=25",
                    "-t", "2.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True)
    media = source.read_bytes()
    calls = []
    def engine(req):
        calls.append(req.url.path)
        if req.url.path == "/object_info":
            return httpx.Response(200, json={n["class_type"]: {} for n in job.input_snapshot["workflow"].values()})
        if req.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "real-fixture-task"})
        if req.url.path == "/view":
            return httpx.Response(200, content=media)
        return httpx.Response(200, json={"real-fixture-task": {"status": {"status_str": "success"},
            "outputs": {"229": {"gifs": [{"filename": "actual.mp4", "type": "output", "subfolder": "motion"}]}}}})
    svc.transport = httpx.MockTransport(engine)
    result = asyncio.run(svc.run(job))
    assert result["adopted"] is False
    assert result["candidate"]["native"]["frames"] >= 50
    assert result["candidate"]["provenance"]["promptId"] == "real-fixture-task"
    assert svc.store.load(pack.id).revision == pack.revision
    assert len(svc.artifacts._manifest()["artifacts"]) == 12
    # Reproduce crash after artifact registration but before job result persisted.
    job.completed_calls["motionPreparation"].pop("result")
    recovered = Job.model_validate(job.model_dump())
    assert asyncio.run(svc.run(recovered)) == result
    assert calls.count("/prompt") == 1 and calls.count("/view") == 1
    assert len(svc.artifacts._manifest()["artifacts"]) == 12


def test_actual_manager_failure_and_restart_resume_via_api(service):
    from duocast.domain.job import JobStatus
    from duocast.jobs.recovery import recover_on_startup
    svc, pack = service
    job = svc.submit(pack.id, request(pack))
    calls = []
    def engine(req):
        calls.append(req.method + " " + req.url.path)
        if req.url.path == "/object_info":
            return httpx.Response(200, json={n["class_type"]: {} for n in job.input_snapshot["workflow"].values()})
        if req.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "paid-task"})
        return httpx.Response(200, json={})
    svc.transport = httpx.MockTransport(engine)

    async def fail_real_worker():
        svc.manager.set_runner(svc.run)
        svc.manager.start()
        for _ in range(200):
            if job.status == JobStatus.FAILED:
                break
            await asyncio.sleep(.005)
        await svc.manager.stop()
        assert job.status == JobStatus.FAILED and job.provider_task_id == "paid-task"
    asyncio.run(fail_real_worker())

    # Startup reload preserves the same paid external task and explicit API
    # recovery requeues it without relaxing ordinary FAILED job semantics.
    restored = recover_on_startup(svc.manager.root)
    manager = JobManager(svc.manager.root, svc.manager.bus)
    manager.restore(restored)
    svc.manager = manager
    app = FastAPI()
    app.include_router(router)
    app.state.motion_preparation_service = svc
    with TestClient(app) as client:
        endpoint = f"/api/motion-packs/{pack.id}/prepare/{job.id}/resume"
        result = client.post(endpoint)
        assert result.status_code == 202, result.text
        assert result.json()["id"] == job.id and result.json()["status"] == "queued"
        assert client.post(endpoint).json()["id"] == job.id
        assert client.post(f"/api/motion-packs/other/prepare/{job.id}/resume").status_code == 404

    async def resumed_worker():
        current = manager.get(job.id)
        svc.timeout_sec = 1000
        manager.set_runner(svc.run)
        manager.start()
        for _ in range(200):
            if current.status == JobStatus.RUNNING:
                break
            await asyncio.sleep(.005)
        await manager.stop()
        assert current.status == JobStatus.UNKNOWN
    asyncio.run(resumed_worker())
    assert calls.count("POST /prompt") == 1
    manager = JobManager(manager.root, manager.bus)
    manager.restore(recover_on_startup(manager.root))
    svc.manager = manager
    with TestClient(app) as client:
        assert client.post(endpoint).status_code == 202
    current = manager.get(job.id)
    current.status = JobStatus.FAILED
    current.completed_calls["motionPreparation"].pop("promptId")
    with TestClient(app) as client:
        assert client.post(endpoint).status_code == 409
