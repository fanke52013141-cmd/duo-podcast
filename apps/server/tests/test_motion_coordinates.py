import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from duocast.api.motion_coordinates import router
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.motion_packs import MotionPackStore
from duocast.services.motion_render_inputs import coordinates_for_clip


@pytest.fixture
def api(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"registered test video")
    entry = artifacts.register("video", "video", str(source), "input")
    store = MotionPackStore(tmp_path / "packs", artifacts.by_id)
    pack = store.create({"name": "coordinate test", "characters": {"A": {"characterId": "A"}},
                         "cameras": [{"id": "wide"}], "clips": [{"id": "talk", "role": "A", "cameraId": "wide", "state": "speaking",
                         "media": {"artifactId": "video", "fileHash": entry["fileHash"]}, "nativeWidth": 100, "nativeHeight": 80,
                         "frameCount": 4, "usableStartFrame": 1, "usableEndFrame": 3}]})
    app = FastAPI()
    app.state.motion_pack_store, app.state.artifacts = store, artifacts
    app.include_router(router)
    with TestClient(app) as client:
        yield client, store, artifacts, pack


def url(pack, clip="talk"):
    return f"/api/motion-packs/{pack.id}/clips/{clip}/coordinates"


def test_registered_bound_coordinates_round_trip_and_revision(api):
    client, store, artifacts, pack = api
    boxes = [None, [10, 10, 30, 40], [11, 10, 31, 40], None]
    response = client.post(url(pack), json={"expectedRevision": 1, "boxes": boxes})
    assert response.status_code == 200, response.text
    current = store.load(pack.id)
    assert current.revision == 2
    ref = current.clips[0].face_coordinates
    assert len(ref.file_hash) == 64
    assert coordinates_for_clip(artifacts, current.clips[0]) == boxes
    entry = artifacts.by_id(ref.artifact_id)
    assert entry["kind"] == "mixed"
    assert entry["paramsSnapshot"]["automaticDetection"] is False
    from pathlib import Path
    data = Path(entry["path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == ref.file_hash
    assert json.loads(data)["videoFileHash"] == pack.clips[0].media.file_hash
    assert json.loads(data)["role"] == "A"
    assert store.load_version(pack.id, pack.version).clips[0].face_coordinates is None
    assert client.post(url(pack), json={"expectedRevision": 1, "boxes": boxes}).status_code == 409


@pytest.mark.parametrize("boxes", [[], [None] * 4, [[0, 0, 5, 5]] * 3,
    [[0, 0, 5, 5], [True, 1, 5, 5], [0, 0, 5, 5], None],
    [None, [0.0, 0, 5, 5], [0, 0, 5, 5], None],
    [None, [0, 0, 101, 5], [0, 0, 5, 5], None],
    [None, [4, 0, 4, 5], [0, 0, 5, 5], None],
    [None, [0, 0, 5], [0, 0, 5, 5], None]])
def test_invalid_or_missing_selected_frames_never_publish(api, boxes):
    client, store, artifacts, pack = api
    assert client.post(url(pack), json={"expectedRevision": 1, "boxes": boxes}).status_code == 422
    assert store.load(pack.id).revision == 1
    assert len(artifacts._manifest()["artifacts"]) == 1


def test_injection_missing_and_stream_limit(api):
    client, _, _, pack = api
    payload = {"expectedRevision": 1, "boxes": [[0, 0, 10, 10]] * 4}
    for extra in ({"path": "C:/private"}, {"passed": True}, {"role": "B"}, {"videoFileHash": "fake"}):
        assert client.post(url(pack), json={**payload, **extra}).status_code == 422
    assert client.post(url(pack, "absent"), json=payload).status_code == 404
    assert client.post("/api/motion-packs/absent/clips/talk/coordinates", json=payload).status_code == 404
    assert client.post(url(pack), content=b" " * (8 * 1024 * 1024 + 1)).status_code == 413
    assert client.post(url(pack), json={"expectedRevision": True, "boxes": payload["boxes"]}).status_code == 422
    assert client.post(url(pack), json={"expectedRevision": 1, "boxes": [None] * 36001}).status_code == 422


def test_coordinate_change_invalidates_existing_checks_and_review(api):
    from duocast.domain.motion_pack import MotionCheckRecord, MotionQualityReport, MotionReviewRecord, clip_fingerprint
    client, store, _, pack = api
    fingerprint = clip_fingerprint(pack, pack.clips[0])
    pack.checks["talk"] = MotionCheckRecord(content_hash=fingerprint, checked_at="test", report=MotionQualityReport(input_file_hash=pack.clips[0].media.file_hash, checks_version="test", status="needs_review"))
    pack.reviews["talk"] = MotionReviewRecord(content_hash=fingerprint, accepted=True, reviewed_at="test")
    store._write(store._path(pack.id), pack)
    response = client.post(url(pack), json={"expectedRevision": 1, "boxes": [[0, 0, 10, 10]] * 4})
    assert response.status_code == 200
    assert response.json()["checks"] == {}
    assert response.json()["reviews"] == {}


def test_revision_race_never_overwrites_newer_draft(api, monkeypatch):
    from duocast.domain.motion_pack import MotionPackDraft
    client, store, artifacts, pack = api
    register = artifacts.register
    def racing_register(*args, **kwargs):
        entry = register(*args, **kwargs)
        draft = MotionPackDraft.model_validate(pack.model_dump(include=set(MotionPackDraft.model_fields)))
        draft.description = "concurrent edit"
        store.update(pack.id, draft, 1)
        return entry
    monkeypatch.setattr(artifacts, "register", racing_register)
    response = client.post(url(pack), json={"expectedRevision": 1, "boxes": [[0, 0, 10, 10]] * 4})
    assert response.status_code == 409
    current = store.load(pack.id)
    assert current.description == "concurrent edit"
    assert current.clips[0].face_coordinates is None
