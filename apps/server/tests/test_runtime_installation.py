"""Real temporary worker-only deployments; never touch the installed runtime."""
import importlib.util
import json
from pathlib import Path

import pytest


def test_running_runtime_lease_blocks_install_and_incomplete_release_blocks_render(deployment):
    import asyncio
    from duocast.services.fast_runtime import runtime_lease
    source, target = deployment
    before = original_state(target)

    async def scenario():
        async with runtime_lease(target):
            with pytest.raises(ValueError, match="active"):
                installer.install(source, target)
            assert original_state(target) == before
        (target / installer.PENDING).write_text('{"releaseId":"pending"}')
        with pytest.raises(ValueError, match="Incomplete"):
            async with runtime_lease(target):
                pytest.fail("Half-installed runtime was allowed to execute")
        (target / installer.PENDING).unlink()

    asyncio.run(scenario())

MODULE = Path(__file__).resolve().parents[3] / "scripts/fast-mode/install_runtime.py"
spec = importlib.util.spec_from_file_location("runtime_installer", MODULE)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def deployment(tmp_path):
    source = tmp_path / "repository/scripts/fast-mode"
    source.mkdir(parents=True)
    for name in installer.FILES:
        if name != "quality.py":
            (source / name).write_bytes(("new " + name).encode())
    quality = source.parents[1] / "apps/server/duocast/services/fast_quality.py"
    quality.parent.mkdir(parents=True)
    quality.write_bytes(b"new quality")
    target = tmp_path / "runtime"
    for name in ("models", "assets"):
        (target / name).mkdir(parents=True)
        (target / name / "do-not-touch.bin").write_bytes(b"private data")
    (target / "worker.py").write_bytes(b"original worker")
    (target / "quality.py").write_bytes(b"original quality")
    (target / "runtime_version.json").write_bytes(b'{"old":"manifest"}')
    (target / "fast_template.json").write_bytes(b'{"identity":"old-template"}')
    return source, target


def original_state(target):
    return {name: (target / name).read_bytes() if (target / name).exists() else None
            for name in (*installer.FILES, installer.MANIFEST, "fast_template.json")}


def test_full_install_manifest_backup_and_rollback_restore_every_hash(deployment):
    source, target = deployment
    before = original_state(target)
    with pytest.warns(RuntimeWarning, match="identity changed"):
        assert installer.install(source, target) == target
    version = json.loads((target / installer.MANIFEST).read_text())
    assert version["templateIdentityRewritten"] is False
    for name, entry in version["files"].items():
        assert installer._hash((target / name).read_bytes()) == entry["sha256"]
    assert (target / "fast_template.json").read_bytes() == before["fast_template.json"]
    installer.rollback(target, version["releaseId"])
    assert original_state(target) == before
    assert not (target / installer.PENDING).exists()
    installer.rollback(target, version["releaseId"])  # Safe idempotent replay.
    assert original_state(target) == before
    for name in ("models", "assets"):
        assert (target / name / "do-not-touch.bin").read_bytes() == b"private data"


def test_new_helper_release_can_rollback_then_restore_a_legacy_snapshot(deployment, monkeypatch):
    source, target = deployment
    original = original_state(target)
    with monkeypatch.context() as patch:
        patch.setattr(installer, 'FILES', installer.LEGACY_FILES)
        with pytest.warns(RuntimeWarning):
            installer.install(source, target)
    legacy_id = json.loads((target / installer.MANIFEST).read_text())['releaseId']
    snapshot_path = target / installer.BACKUPS / legacy_id / 'snapshot.json'
    snapshot = json.loads(snapshot_path.read_text())
    snapshot['schemaVersion'] = 1
    snapshot_path.write_text(json.dumps(snapshot))
    before_new = original_state(target)
    installer.install(source, target)
    current_id = json.loads((target / installer.MANIFEST).read_text())['releaseId']
    assert (target / 'prepare_expert_motion.py').is_file()
    installer.rollback(target, current_id)
    assert original_state(target) == before_new
    assert not (target / 'prepare_expert_motion.py').exists()
    installer.rollback(target, legacy_id)
    assert original_state(target) == original


def test_all_sources_are_preflighted_before_first_target_change(deployment):
    source, target = deployment
    before = original_state(target)
    (source / "resident_worker.py").unlink()
    with pytest.raises(ValueError, match="Missing source"):
        installer.install(source, target)
    assert original_state(target) == before
    assert not (target / installer.BACKUPS).exists()


@pytest.mark.parametrize("failure", [OSError("injected disk failure"), KeyboardInterrupt()])
def test_mid_release_failure_restores_old_files_and_removes_new_ones(deployment, monkeypatch, failure):
    source, target = deployment
    before = original_state(target)
    atomic = installer._atomic_bytes
    failed = False
    def interrupted(path, data, mode=None):
        nonlocal failed
        if Path(path) == target / "motion_audio_cache.py" and not failed:
            failed = True
            raise failure
        return atomic(path, data, mode)
    monkeypatch.setattr(installer, "_atomic_bytes", interrupted)
    with pytest.raises(type(failure)):
        installer.install(source, target)
    assert failed
    assert original_state(target) == before
    assert not (target / installer.PENDING).exists()


def test_pending_hard_interruption_blocks_install_and_explicit_rollback_recovers(deployment):
    source, target = deployment
    before = original_state(target)
    with pytest.warns(RuntimeWarning):
        installer.install(source, target)
    release = json.loads((target / installer.MANIFEST).read_text())["releaseId"]
    # A durable pending marker and half-replaced files are the exact state left
    # by a killed process; advisory OS locks do not survive process death.
    installer._json_write(target / installer.PENDING, {"releaseId": release})
    (target / "worker.py").write_bytes(b"half-installed or corrupt worker")
    with pytest.raises(ValueError, match="Incomplete installation"):
        installer.install(source, target)
    installer.rollback(target, release)
    assert original_state(target) == before


def test_backup_tampering_and_unknown_or_escape_ids_rejected_before_restore(deployment):
    source, target = deployment
    with pytest.warns(RuntimeWarning):
        installer.install(source, target)
    release = json.loads((target / installer.MANIFEST).read_text())["releaseId"]
    current = original_state(target)
    for identifier in ("../escape", "unknown", "a" * 32):
        with pytest.raises(ValueError):
            installer.rollback(target, identifier)
        assert original_state(target) == current
    (target / installer.BACKUPS / release / "worker.py").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        installer.rollback(target, release)
    assert original_state(target) == current


@pytest.mark.parametrize("name", ["worker.py", "runtime_version.json", installer.BACKUPS])
def test_linked_write_destinations_are_rejected(deployment, tmp_path, name):
    source, target = deployment
    outside = tmp_path / "outside"
    is_directory = name == installer.BACKUPS
    if is_directory:
        outside.mkdir()
    else:
        outside.write_bytes(b"outside unchanged")
    path = target / name
    path.unlink(missing_ok=True)
    try:
        path.symlink_to(outside, target_is_directory=is_directory)
    except OSError:
        pytest.skip("Creating symlinks is unavailable")
    with pytest.raises(ValueError, match="linked"):
        installer.install(source, target)
    if not is_directory:
        assert outside.read_bytes() == b"outside unchanged"


def test_requires_existing_runtime_and_preserves_linked_read_only_dependencies(deployment, tmp_path):
    source, target = deployment
    with pytest.raises(ValueError, match="existing runtime"):
        installer.install(source, tmp_path / "new-runtime")
    shared = tmp_path / "shared-models"
    shared.mkdir()
    (shared / "weights.bin").write_bytes(b"shared model")
    linked = target / "models/vendor"
    try:
        linked.symlink_to(shared, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable")
    with pytest.warns(RuntimeWarning):
        installer.install(source, target)
    assert (linked / "weights.bin").read_bytes() == b"shared model"
