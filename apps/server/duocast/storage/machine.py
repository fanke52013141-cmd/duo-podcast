"""机器级设置与凭据引用（06 §7.3）：工程与打包只含 providerProfileId，密钥存系统凭据管理器。

当前实现为占位：凭据存储（keyring）在关口 B 起接入，profile → 引用结构已就位。
"""

from __future__ import annotations

import json
from pathlib import Path


class MachineSettings:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._file = root / "machine.json"

    def _data(self) -> dict:
        if self._file.exists():
            return json.loads(self._file.read_text(encoding="utf-8"))
        return {"providerProfiles": {}}

    def save(self, data: dict) -> None:
        self._file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_profile(self, profile_id: str) -> dict:
        return self._data().get("providerProfiles", {}).get(profile_id, {})

    def set_profile(self, profile_id: str, ref: dict) -> None:
        data = self._data()
        data.setdefault("providerProfiles", {})[profile_id] = ref
        self.save(data)

    def set_credential_ref(self, profile_id: str, secret_key: str) -> None:
        """只存引用，不落密钥明文（06 §7.3）。"""
        self.set_profile(profile_id, {"credentialRef": secret_key})


class CacheStore:
    """输入哈希 → artifactId 复用（06 §7.2 / 02 §5.9：命中即复用，不重复推理与计费）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._file = root / "cache.json"

    def _data(self) -> dict:
        if self._file.exists():
            return json.loads(self._file.read_text(encoding="utf-8"))
        return {"entries": {}}

    def lookup(self, dependency_hash: str) -> str | None:
        return self._data()["entries"].get(dependency_hash)

    def put(self, dependency_hash: str, artifact_id: str) -> None:
        data = self._data()
        data["entries"][dependency_hash] = artifact_id
        tmp = self._file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        import os

        os.replace(tmp, self._file)
