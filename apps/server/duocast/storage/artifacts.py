"""不可变产物登记（06 §7.2）：manifest.json 追加 {artifactId, kind, path, fileHash, dependencyHash, paramsSnapshot, workflowVersion}。

正式产物使用不可变版本路径，不覆盖同名文件（02 §6.2）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

KINDS = {"audio", "image", "video", "subtitle", "mixed"}


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_path = root / "manifest.json"

    def _manifest(self) -> dict[str, Any]:
        if self._manifest_path.exists():
            return json.loads(self._manifest_path.read_text(encoding="utf-8"))
        return {"artifacts": []}

    def register(
        self,
        artifact_id: str,
        kind: str,
        path: str,
        dependency_hash: str,
        params_snapshot: dict | None = None,
        workflow_version: Optional[str] = None,
        file_hash: Optional[str] = None,
    ) -> dict[str, Any]:
        assert kind in KINDS, f"unknown kind: {kind}"
        entry = {
            "artifactId": artifact_id,
            "kind": kind,
            "path": path,
            "fileHash": file_hash or self._hash_file(path),
            "dependencyHash": dependency_hash,
            "paramsSnapshot": params_snapshot or {},
            "workflowVersion": workflow_version,
        }
        manifest = self._manifest()
        manifest["artifacts"].append(entry)
        tmp = self._manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        import os

        os.replace(tmp, self._manifest_path)
        return entry

    def by_hash(self, dependency_hash: str) -> Optional[dict[str, Any]]:
        for entry in self._manifest()["artifacts"]:
            if entry["dependencyHash"] == dependency_hash:
                return entry
        return None

    def by_id(self, artifact_id: str) -> Optional[dict[str, Any]]:
        for entry in self._manifest()["artifacts"]:
            if entry["artifactId"] == artifact_id:
                return entry
        return None

    @staticmethod
    def _hash_file(path: str) -> str:
        try:
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return ""
