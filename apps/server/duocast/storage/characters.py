"""全局角色库（01 §1.5 资产库语义）：跨工程复用的角色（形象 / 性格 / 声线引用占位）。

存储 storage/characters/characters.json；首启动播种李雷 / 韩梅梅两条默认角色，
与页面此前硬编码的默认一致，行为兼容。图片只存相对文件名，由 assets API 提供读取。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

DEFAULT_CHARACTERS: list[dict] = [
    {
        "id": "char-lilei",
        "name": "李雷",
        "speaker": "A",
        "role": "主持",
        "desc": "主持 · 男 · 沉稳清晰",
        "traits": "沉稳 · 主持型 · 语速 1.0",
        "imagePath": "",
    },
    {
        "id": "char-hanmeimei",
        "name": "韩梅梅",
        "speaker": "B",
        "role": "嘉宾",
        "desc": "嘉宾 · 女 · 温和有说服力",
        "traits": "温和 · 嘉宾型 · 语速 1.0",
        "imagePath": "",
    },
]


class CharacterStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._file = root / "characters.json"
        if not self._file.exists():
            self._write(DEFAULT_CHARACTERS)

    def _read(self) -> list[dict]:
        return json.loads(self._file.read_text(encoding="utf-8"))

    def _write(self, items: list[dict]) -> None:
        tmp = self._file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        import os

        os.replace(tmp, self._file)

    def list(self) -> list[dict]:
        return self._read()

    def get(self, character_id: str) -> dict | None:
        return next((c for c in self._read() if c.get("id") == character_id), None)

    def create(self, name: str, speaker: str = "A", role: str = "主持",
               desc: str = "", traits: str = "") -> dict:
        items = self._read()
        character = {
            "id": f"char-{uuid.uuid4().hex[:8]}",
            "name": name.strip(),
            "speaker": speaker if speaker in ("A", "B") else "A",
            "role": role.strip() or "主持",
            "desc": desc.strip(),
            "traits": traits.strip(),
            "imagePath": "",
        }
        items.append(character)
        self._write(items)
        return character

    def update(self, character_id: str, patch: dict) -> dict | None:
        items = self._read()
        for i, c in enumerate(items):
            if c.get("id") == character_id:
                allowed = {k: patch[k] for k in ("name", "role", "desc", "traits", "imagePath", "speaker")
                           if k in patch}
                items[i] = {**c, **allowed}
                self._write(items)
                return items[i]
        return None
