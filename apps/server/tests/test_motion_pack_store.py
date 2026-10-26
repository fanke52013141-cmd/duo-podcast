"""Storage-policy tests; media decode/detection belongs to quality tests."""
import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from duocast.domain.motion_pack import MotionPackDraft, readiness
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.motion_packs import InvalidMotionPack, MotionPackConflict, MotionPackStore


@pytest.fixture
def setup_pack(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")

    def media(id, kind):
        path = artifacts.root / (id + ".fixture")
        path.write_bytes(("storage fixture " + id).encode())
        entry = artifacts.register(id, kind, str(path), "unit-test")
        return {"artifactId": id, "fileHash": entry["fileHash"]}

    image_a, image_b = media("charA", "image"), media("charB", "image")
    camera_image = media("scene", "image")
    cameras = [{"id": "dual", "mode": "twoShot", "masterImage": camera_image}]
    cameras += [{"id": "ots" + s, "mode": "overShoulder", "masterImage": camera_image,
                 "subjectSpeaker": s, "foregroundSpeaker": "B" if s == "A" else "A"} for s in "AB"]
    clips = []
    for camera in cameras:
        roles = "AB" if camera["mode"] == "twoShot" else camera["subjectSpeaker"]
        for role in roles:
            for state in ("speaking", "listening"):
                id = camera["id"] + "-" + role + "-" + state
                clips.append({"id": id, "role": role, "cameraId": camera["id"], "state": state,
                              "media": media(id, "video"), "nativeWidth": 864, "nativeHeight": 480,
                              "frameCount": 100, "usableEndFrame": 100})
    draft = {"name": "物品动作包", "characters": {
        "A": {"characterId": "lamp", "referenceImage": image_a},
        "B": {"characterId": "signal", "referenceImage": image_b}}, "cameras": cameras, "clips": clips}
    store = MotionPackStore(tmp_path / "packs", artifacts.by_id)
    pack = store.create(draft, "test-pack")
    return store, artifacts, pack, draft


def report_for(clip, status="needs_review", **extra):
    return {"inputFileHash": clip.media.file_hash, "checksVersion": "motion-quality-v1", "status": status,
            "evidence": {"completeSourceFramesDecoded": clip.frame_count,
                         "analyzedStartFrame": clip.usable_start_frame, "analyzedEndFrame": clip.usable_end_frame},
            "native": {"width": 864, "height": 480, "fps": 25.0, "frames": 100, "durationSec": 4.0}, **extra}


def adopt(store, pack, camera_id):
    for clip in [c for c in pack.clips if c.camera_id == camera_id]:
        pack = store.record_checks(pack.id, clip.id, report_for(clip), pack.revision)
        pack = store.review(pack.id, clip.id, True, pack.revision, "完整审看：结构运动、五官和身份可用")
    return pack


def draft_of(pack):
    return pack.model_dump(mode="json", by_alias=True, include=set(MotionPackDraft.model_fields))


def test_structural_reports_never_replace_review_and_styles_are_independent(setup_pack):
    store, _, pack, _ = setup_pack
    clip = pack.clips[0]
    pack = store.record_checks(pack.id, clip.id, report_for(clip, "passed"), pack.revision)
    assert not store.readiness(pack.id, "twoShot")["ready"]
    assert any("human_review_required" in reason for reason in readiness(pack, "twoShot")["missing"])
    pack = adopt(store, pack, "dual")
    assert store.readiness(pack.id, "twoShot")["ready"]
    assert not store.readiness(pack.id, "overShoulder")["ready"]
    pack = adopt(store, pack, "otsA")
    assert not store.readiness(pack.id, "overShoulder")["ready"]
    pack = adopt(store, pack, "otsB")
    assert store.readiness(pack.id, "mixed")["ready"]


def test_uncertain_checks_require_reason_and_hard_failures_cannot_be_overridden(setup_pack):
    store, _, pack, _ = setup_pack
    clip = pack.clips[0]
    with pytest.raises(InvalidMotionPack, match="checks"):
        store.review(pack.id, clip.id, True, pack.revision, "looks fine")
    pack = store.record_checks(pack.id, clip.id, report_for(clip), pack.revision)
    with pytest.raises(InvalidMotionPack, match="explicit review reason"):
        store.review(pack.id, clip.id, True, pack.revision)
    pack = store.review(pack.id, clip.id, True, pack.revision, "逐帧审看")
    pack = store.record_checks(pack.id, clip.id, report_for(clip, "failed", hardFailures=["NO_BODY_MOTION"]), pack.revision)
    with pytest.raises(InvalidMotionPack, match="cannot be overridden"):
        store.review(pack.id, clip.id, True, pack.revision, "force adopt")


@pytest.mark.parametrize("native", [{}, {"width": 1920, "height": 1080, "fps": 25, "frames": 100, "durationSec": 4},
                                     {"width": 864, "height": 480, "fps": 30, "frames": 100, "durationSec": 4}])
def test_missing_or_false_native_evidence_is_not_human_overrideable(setup_pack, native):
    store, _, pack, _ = setup_pack
    clip = pack.clips[0]
    pack = store.record_checks(pack.id, clip.id, report_for(clip, native=native), pack.revision)
    assert pack.checks[clip.id].report.status == "failed"
    assert pack.checks[clip.id].report.hard_failures
    with pytest.raises(InvalidMotionPack):
        store.review(pack.id, clip.id, True, pack.revision, "trust declared dimensions")


@pytest.mark.parametrize("evidence", [{}, {"completeSourceFramesDecoded": True, "analyzedStartFrame": 0, "analyzedEndFrame": 100},
    {"completeSourceFramesDecoded": 100, "analyzedStartFrame": 0, "analyzedEndFrame": 90}])
def test_incomplete_or_different_analysis_range_cannot_be_adopted(setup_pack, evidence):
    store, _, pack, _ = setup_pack
    clip = pack.clips[0]
    pack = store.record_checks(pack.id, clip.id, report_for(clip, evidence=evidence), pack.revision)
    assert "ANALYSIS_RANGE_MISMATCH" in pack.checks[clip.id].report.hard_failures
    with pytest.raises(InvalidMotionPack):
        store.review(pack.id, clip.id, True, pack.revision, "override incomplete analysis")


def test_content_change_invalidates_only_related_records_and_old_pin_survives(setup_pack):
    store, _, pack, _ = setup_pack
    pack = adopt(store, pack, "dual")
    previous = pack.model_copy(deep=True)
    draft = draft_of(pack)
    target = draft["clips"][0]["id"]
    draft["clips"][0]["usableEndFrame"] = 90
    changed = store.update(pack.id, draft, pack.revision)
    assert target not in changed.checks and target not in changed.reviews
    assert len(changed.reviews) == len(previous.reviews) - 1
    assert not store.readiness(pack.id, "twoShot")["ready"]
    frozen = store.load_version(previous.id, previous.version)
    assert frozen == previous
    assert store.readiness(previous.id, "twoShot", previous.version)["ready"]


def test_role_change_invalidates_both_visible_two_shot_roles(setup_pack):
    store, _, pack, _ = setup_pack
    pack = adopt(store, pack, "dual")
    draft = draft_of(pack)
    draft["characters"]["B"]["referenceImage"] = draft["characters"]["A"]["referenceImage"]
    updated = store.update(pack.id, draft, pack.revision)
    assert all(c.id not in updated.reviews for c in updated.clips if c.camera_id == "dual" and c.role == "A")
    assert all(c.id not in updated.reviews for c in updated.clips if c.camera_id == "dual" and c.role == "B")


def test_check_rule_change_keeps_human_record_but_new_failure_blocks_use(setup_pack):
    store, _, pack, _ = setup_pack
    pack = adopt(store, pack, "dual")
    clip = pack.clips[0]
    previous_review = pack.reviews[clip.id]
    pack = store.record_checks(pack.id, clip.id, report_for(clip, checksVersion="motion-quality-v2"), pack.revision)
    assert pack.reviews[clip.id] == previous_review
    assert store.readiness(pack.id, "twoShot")["ready"]
    pack = store.record_checks(pack.id, clip.id, report_for(clip, "failed", hardFailures=["DECODE_FAILED"]), pack.revision)
    assert not store.readiness(pack.id, "twoShot")["ready"]


def test_physical_tampering_blocks_current_and_pinned_adoption(setup_pack):
    store, artifacts, pack, _ = setup_pack
    pack = adopt(store, pack, "dual")
    artifact = artifacts.by_id(pack.clips[0].media.artifact_id)
    from pathlib import Path
    Path(artifact["path"]).write_bytes(b"changed externally")
    assert not store.readiness(pack.id, "twoShot")["ready"]
    assert not store.readiness(pack.id, "twoShot", pack.version)["ready"]
    with pytest.raises(InvalidMotionPack, match="content changed"):
        store.review(pack.id, pack.clips[0].id, True, pack.revision, "ignore tampering")


def test_revision_conflict_and_restart_history(setup_pack):
    store, _, pack, draft = setup_pack
    old_version = pack.version
    draft["name"] = "第二版"
    changed = store.update(pack.id, draft, pack.revision)
    with pytest.raises(MotionPackConflict):
        store.update(pack.id, draft, pack.revision)
    for invalid in (True, 0, -1, "2"):
        with pytest.raises(InvalidMotionPack):
            store.update(pack.id, draft, invalid)
    restarted = MotionPackStore(store.root, store.artifact_lookup)
    assert restarted.load(pack.id).version == changed.version
    assert restarted.load_version(pack.id, old_version).name == "物品动作包"
    assert [p.id for p in restarted.list()] == [pack.id]


def test_two_concurrent_updates_have_one_winner(setup_pack):
    store, _, pack, draft = setup_pack

    def update(name):
        try:
            return store.update(pack.id, {**draft, "name": name}, pack.revision).name
        except MotionPackConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as executor:
        outcomes = list(executor.map(update, ["甲", "乙"]))
    assert outcomes.count("conflict") == 1


def test_two_store_instances_updates_and_create_have_one_winner(setup_pack):
    store, _, pack, draft = setup_pack
    other = MotionPackStore(store.root, store.artifact_lookup)
    import threading
    barrier = threading.Barrier(2)

    def write(item):
        instance, name = item
        barrier.wait()
        try:
            return instance.update(pack.id, {**draft, "name": name}, pack.revision).name
        except MotionPackConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(write, [(store, "one"), (other, "two")]))
    assert results.count("conflict") == 1
    assert store.load(pack.id).revision == pack.revision + 1

    def create(instance):
        barrier.wait()
        try:
            return instance.create(draft, "same-new-id").id
        except MotionPackConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as executor:
        assert list(executor.map(create, [store, other])).count("conflict") == 1


def test_other_visible_character_tampering_blocks_two_shot_review(setup_pack):
    store, artifacts, pack, _ = setup_pack
    clip = pack.clips[0]
    assert clip.role == "A"
    pack = store.record_checks(pack.id, clip.id, report_for(clip), pack.revision)
    from pathlib import Path
    Path(artifacts.by_id(pack.characters["B"].reference_image.artifact_id)["path"]).write_bytes(b"tampered")
    with pytest.raises(InvalidMotionPack, match="content changed"):
        store.review(pack.id, clip.id, True, pack.revision, "reviewed")


def test_separate_processes_cannot_publish_same_expected_revision(setup_pack, tmp_path):
    import json
    import subprocess
    import sys
    import time
    store, _, pack, draft = setup_pack
    payload = tmp_path / "draft.json"
    payload.write_text(json.dumps(draft), encoding="utf-8")
    signal = tmp_path / "start"
    code = """
import json, sys, time
from pathlib import Path
from duocast.storage.motion_packs import MotionPackStore, MotionPackConflict
root, payload, signal, identity, revision = sys.argv[1:]
store = MotionPackStore(Path(root))
original = store._current
def slow_current(*args):
    current = original(*args)
    time.sleep(.3)
    return current
store._current = slow_current
print('ready', flush=True)
while not Path(signal).exists():
    time.sleep(.01)
try:
    store.update(identity, json.loads(Path(payload).read_text(encoding='utf-8')), int(revision))
    print('winner')
except MotionPackConflict:
    print('conflict')
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(store.root), str(payload), str(signal),
                                   pack.id, str(pack.revision)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == "ready"
        signal.write_text("go")
        outcomes = [process.communicate(timeout=20) for process in processes]
        assert all(process.returncode == 0 for process in processes), outcomes
        assert sorted(out.strip() for out, _ in outcomes) == ["conflict", "winner"]
        assert store.load(pack.id).revision == pack.revision + 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


@pytest.mark.parametrize("key,value", [("ready", True), ("status", "ready"), ("reviews", {}), ("checks", {}),
                                     ("version", "injected"), ("runtimeRoot", "C:/untrusted"), ("id", "forged")])
def test_draft_rejects_status_review_and_runtime_injection(setup_pack, key, value):
    store, _, pack, draft = setup_pack
    with pytest.raises(ValidationError):
        store.update(pack.id, {**draft, key: value}, pack.revision)


def test_media_reference_rejects_path_and_reports_require_matching_hash(setup_pack):
    store, _, pack, draft = setup_pack
    draft["clips"][0]["media"]["path"] = "C:/untrusted/script.py"
    with pytest.raises(ValidationError):
        store.update(pack.id, draft, pack.revision)
    with pytest.raises(InvalidMotionPack, match="report does not match"):
        store.record_checks(pack.id, pack.clips[0].id,
                            report_for(pack.clips[0], inputFileHash=hashlib.sha256(b"other").hexdigest()), pack.revision)


@pytest.mark.parametrize("id", ["../escape", "CON", "foo/bar", "foo\\bar", "a.", " a", "", "NUL"])
def test_storage_ids_reject_traversal_devices_and_aliases(tmp_path, id):
    store = MotionPackStore(tmp_path)
    with pytest.raises(InvalidMotionPack):
        store.load(id)


def test_no_artifact_verifier_cannot_produce_ready_pack(setup_pack):
    store, _, pack, _ = setup_pack
    unverified = MotionPackStore(store.root)
    with pytest.raises(InvalidMotionPack, match="verifier"):
        unverified.record_checks(pack.id, pack.clips[0].id, report_for(pack.clips[0]), pack.revision)


def test_draft_binding_and_frame_range_validation(setup_pack):
    _, _, _, draft = setup_pack
    draft["clips"][0]["usableStartFrame"] = 100
    with pytest.raises(ValidationError, match="range"):
        MotionPackDraft.model_validate(draft)
    draft["clips"][0]["usableStartFrame"] = 0
    draft["clips"][0]["cameraId"] = "missing"
    with pytest.raises(ValidationError, match="missing"):
        MotionPackDraft.model_validate(draft)


def test_failed_head_publication_preserves_old_state_and_retry_can_succeed(setup_pack, monkeypatch):
    store, _, pack, draft = setup_pack
    original_write = store._write

    def fail_head(path, value):
        if path.name == "head.json":
            raise OSError("simulated atomic replacement failure")
        original_write(path, value)

    monkeypatch.setattr(store, "_write", fail_head)
    with pytest.raises(OSError):
        store.update(pack.id, {**draft, "name": "interrupted"}, pack.revision)
    assert store.load(pack.id) == pack
    assert store.load_version(pack.id, pack.version) == pack
    monkeypatch.setattr(store, "_write", original_write)
    updated = store.update(pack.id, {**draft, "name": "retry succeeds"}, pack.revision)
    assert updated.revision == pack.revision + 1
    assert store.load(pack.id).name == "retry succeeds"


def test_linked_storage_directory_is_rejected(tmp_path):
    store = MotionPackStore(tmp_path / "packs")
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = store.root / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    if not alias.is_symlink():
        pytest.skip("directory symlink was not created")
    with pytest.raises(InvalidMotionPack, match="linked"):
        store.load("alias")


def test_corrupt_metadata_has_a_controlled_error(setup_pack):
    store, _, pack, _ = setup_pack
    (store.root / pack.id / "head.json").write_text("{invalid", encoding="utf-8")
    with pytest.raises(InvalidMotionPack, match="metadata"):
        store.load(pack.id)
