from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from duocast.api import projects
from duocast.domain.project import Project
from duocast.services.motion_binding import motion_binding_dependency, validate_project_motion_binding
from duocast.storage.project_store import ProjectStore
from test_motion_pack_store import setup_pack, adopt, draft_of


@pytest.fixture
def api(setup_pack, tmp_path, monkeypatch):
    store, artifacts, pack, draft = setup_pack
    pack = adopt(store, pack, "dual")
    project_store = ProjectStore(tmp_path / "projects")
    app = FastAPI()
    app.state.project_store = project_store
    app.state.motion_pack_store = store
    app.include_router(projects.router)
    monkeypatch.setattr(projects, "_inject_states", lambda request, project: project.model_dump(mode="json", by_alias=True))
    with TestClient(app) as client:
        yield client, store, project_store, pack, artifacts


def create(client, pack, **extra):
    return client.post("/api/projects", json={"id": "episode", "generationMode": "fast",
                       "motionPack": {"id": pack.id}, **extra})


def test_create_pins_exact_version_and_head_update_does_not_upgrade(api):
    client, store, projects_store, pack, _ = api
    response = create(client, pack)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["motionPack"] == {"id": pack.id, "version": pack.version}
    assert data["fastTemplate"] == {} and data["generationMode"] == "fast"
    changed = draft_of(pack)
    changed["name"] = "new head"
    latest = store.update(pack.id, changed, pack.revision)
    assert latest.version != pack.version
    project = projects_store.load("episode")
    assert validate_project_motion_binding(project, store).version == pack.version
    assert motion_binding_dependency(project)["motionPack"]["version"] == pack.version


def test_missing_reverse_angle_rejected_without_creating_project(api):
    client, _, projects_store, pack, _ = api
    assert create(client, pack, shotStyle="overShoulder").status_code == 422
    assert projects_store.load("episode") is None


def test_tampered_media_rejected(api):
    client, _, projects_store, pack, artifacts = api
    Path(artifacts.by_id(pack.clips[0].media.artifact_id)["path"]).write_bytes(b"tampered")
    assert create(client, pack).status_code == 422
    assert projects_store.load("episode") is None


@pytest.mark.parametrize("binding", [{"id": "test-pack", "path": "C:/runtime"},
    {"id": "test-pack", "checks": {"status": "passed"}}, {"id": "../escape"}])
def test_binding_payload_cannot_inject_paths_or_checks(api, binding):
    client, _, _, pack, _ = api
    assert create(client, pack, motionPack=binding).status_code == 422


def test_patch_style_uses_pinned_version_and_rejects_missing_camera(api):
    client, _, projects_store, pack, _ = api
    data = create(client, pack).json()
    response = client.patch("/api/projects/episode", json={"expectedRevision": data["revision"], "shotStyle": "mixed"})
    assert response.status_code == 422
    assert projects_store.load("episode").shot_style == "twoShot"


def test_legacy_project_load_remains_unbound_and_standard_rejects_motion(api):
    client, _, projects_store, pack, _ = api
    old = Project.model_validate({"id": "old", "fastTemplate": {"id": "legacy"}})
    assert old.motion_pack is None
    assert validate_project_motion_binding(old, None) is None
    assert old.fast_template == {"id": "legacy"}
    assert create(client, pack, generationMode="standard").status_code == 422
    assert projects_store.load("episode") is None
