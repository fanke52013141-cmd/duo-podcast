"""Portable backups never carry executable runtime bindings into a new machine."""
import pytest
from fastapi import HTTPException

from duocast.services.backup import import_backup, make_backup
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.project_store import ProjectStore


def _project(store, runtime_root):
    project = store.create("SOURCE")
    return store.apply(project.id, {
        "generation_mode": "fast",
        "fast_template": {"id": "studio-test", "version": "v1", "runtimeRoot": runtime_root},
    }, project.revision)


def test_import_clears_external_fast_runtime_and_requires_reselection(tmp_path):
    source = ProjectStore(tmp_path / "source-projects", debounce_ms=0)
    project = _project(source, r"D:\local\runtime")
    source_artifacts = ArtifactStore(tmp_path / "source-artifacts")
    package = make_backup(project, source_artifacts)
    target = ProjectStore(tmp_path / "target-projects", debounce_ms=0)
    target_artifacts = ArtifactStore(tmp_path / "target-artifacts")

    with package.open("rb") as stream:
        result = import_backup(stream, target, target_artifacts)
    imported = target.load(result["projectId"])
    assert imported.generation_mode == "standard"
    assert imported.fast_template == {}
    assert result["summary"]["runtimeRebound"] is True
    assert any("重新选择" in item for item in result["summary"]["recheck"])


def test_import_rejects_unc_runtime_path(tmp_path):
    source = ProjectStore(tmp_path / "source-projects", debounce_ms=0)
    project = _project(source, r"\\attacker\share\runtime")
    source_artifacts = ArtifactStore(tmp_path / "source-artifacts")
    package = make_backup(project, source_artifacts)
    target = ProjectStore(tmp_path / "target-projects", debounce_ms=0)
    target_artifacts = ArtifactStore(tmp_path / "target-artifacts")

    with package.open("rb") as stream:
        with pytest.raises(HTTPException) as caught:
            import_backup(stream, target, target_artifacts)
    assert caught.value.status_code == 422
    assert "UNC" in str(caught.value.detail)
