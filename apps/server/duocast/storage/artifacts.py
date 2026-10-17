"""不可变产物登记（06 §7.2）：manifest.json 追加 {artifactId, kind, path, fileHash, dependencyHash, paramsSnapshot, workflowVersion}。

正式产物使用不可变版本路径，不覆盖同名文件（02 §6.2）。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

KINDS = {"audio", "image", "video", "subtitle", "mixed"}


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_path = root / "manifest.json"
        # 清单是「读-改-写」：任务在线程池里登记（to_thread），同一进程内必须串行，
        # 否则并发登记会互相覆盖条目、产物从清单消失（导出报成片文件缺失）。
        self._lock = threading.RLock()

    def _manifest(self) -> dict[str, Any]:
        with self._lock:
            return self._read_locked()

    def _read_locked(self) -> dict[str, Any]:
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
        with self._lock:
            manifest = self._read_locked()
            manifest["artifacts"].append(entry)
            self._write_locked(manifest)
        return entry

    def _write_locked(self, manifest: dict[str, Any]) -> None:
        """原子写：唯一临时文件 + fsync + os.replace。

        固定临时名（manifest.json.tmp）在并发下会互相截断，必须每次新建。
        """
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._manifest_path.parent), prefix=".manifest-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._manifest_path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

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
