"""Asset jobs have their own durable owner and cannot collide with project jobs."""
import pytest

from duocast.core.eventbus import EventBus
from duocast.domain.job import Job, JobStatus
from duocast.jobs.manager import JobManager
from duocast.jobs.recovery import recover_on_startup


def manager_at(root):
    return JobManager(root / "jobs", EventBus(root / "events.jsonl"))


def submit_pack(manager, pack_id, token="same-click"):
    return manager.submit("motion.checks", "", "cpu", token, {"packId": pack_id},
                          owner_type="motionPack", owner_id=pack_id)


def test_click_is_idempotent_within_owner_but_not_across_asset_packs(tmp_path):
    manager = manager_at(tmp_path)
    first = submit_pack(manager, "pack-A")
    assert submit_pack(manager, "pack-A").id == first.id
    second = submit_pack(manager, "pack-B")
    assert second.id != first.id
    assert {j.id for j in manager.list(owner_type="motionPack")} == {first.id, second.id}
    assert manager.list(owner_type="motionPack", owner_id="pack-A") == [first]


def test_legacy_project_tokens_still_match_after_restore(tmp_path):
    manager = manager_at(tmp_path)
    old = Job(id="JOLD", project_id="EP1", kind="renders", client_token="old-click",
              status=JobStatus.SUCCEEDED)
    manager.restore([old])
    assert manager.submit("renders", "EP1", "gpu", "old-click", {}).id == old.id
    assert old.owner_type == "project"
    assert manager.list("EP1") == [old]


def test_asset_job_owner_survives_disk_recovery_and_is_not_a_fake_project(tmp_path):
    manager = manager_at(tmp_path)
    job = submit_pack(manager, "pack-A")
    recovered = recover_on_startup(tmp_path / "jobs")
    assert len(recovered) == 1
    assert recovered[0].owner_type == "motionPack"
    assert recovered[0].owner_id == "pack-A"
    assert recovered[0].project_id == ""
    restored = manager_at(tmp_path)
    restored.restore(recovered)
    assert submit_pack(restored, "pack-A").id == job.id
    assert restored.list("EP1") == []


def test_purge_of_project_jobs_does_not_remove_asset_jobs(tmp_path):
    manager = manager_at(tmp_path)
    asset_job = submit_pack(manager, "pack-A")
    project_job = manager.submit("tts.synthesize", "EP1", "gpu", "voice", {})
    assert manager.purge_project("EP1", tmp_path / "trash") == 1
    assert manager.get(project_job.id) is None
    assert manager.get(asset_job.id) is asset_job
    assert (tmp_path / "jobs" / asset_job.id / "job.json").exists()


@pytest.mark.parametrize("owner_type,owner_id,project_id", [
    ("other", "pack-A", ""), ("motionPack", "", ""),
    ("motionPack", "../escape", ""), ("motionPack", "pack-A", "EP1"),
    ("project", "EP2", "EP1"),
])
def test_invalid_owner_is_rejected_before_persistence(tmp_path, owner_type, owner_id, project_id):
    manager = manager_at(tmp_path)
    with pytest.raises(ValueError):
        manager.submit("motion.checks", project_id, "cpu", "bad", {},
                       owner_type=owner_type, owner_id=owner_id)
    assert manager.list() == []
    assert list((tmp_path / "jobs").iterdir()) == []


def test_job_events_identify_asset_owner_without_project_alias(tmp_path):
    manager = manager_at(tmp_path)
    job = submit_pack(manager, "pack-A")
    manager.transition(job, JobStatus.RUNNING)
    manager.transition(job, JobStatus.SUCCEEDED)
    events = manager.bus.replay(0)
    assert events
    for event in events:
        assert event["payload"]["ownerType"] == "motionPack"
        assert event["payload"]["ownerId"] == "pack-A"
        assert event["payload"]["projectId"] == ""


def test_api_owner_filters_preserve_global_asset_jobs(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from duocast.api.jobs import router
    from duocast.storage.project_store import ProjectStore

    app = FastAPI()
    app.state.job_manager = manager_at(tmp_path)
    app.state.project_store = ProjectStore(tmp_path / "projects")
    app.include_router(router)
    asset = submit_pack(app.state.job_manager, "pack-A")
    submit_pack(app.state.job_manager, "pack-B")
    with TestClient(app) as client:
        response = client.get("/api/jobs?ownerType=motionPack&ownerId=pack-A")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [asset.id]
        assert client.get("/api/jobs?projectId=EP1").json() == []
        assert client.get("/api/jobs?ownerType=invalid").status_code == 422
