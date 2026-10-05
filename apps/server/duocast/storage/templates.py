"""节目模板库（01 §1.5 / README HF-00）：可复用配置（画幅、A/B 声线绑定）持久化。

模板只带配置与素材引用，不带内容（01 §1.5 模板语义）；工程创建时按 templateId
套用 config。内置两个种子模板，首次启动时落盘；内置模板不可删除。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

# 内置种子（对齐首页原静态模板文案）；仅当 templates.json 不存在时写入一次。
BUILTIN_TEMPLATES: list[dict] = [
    {
        "id": "tpl-tech-weekly",
        "name": "科技周报",
        "desc": "A·李雷（主持）/ B·韩梅梅（嘉宾）· 演播室同框 · 每周更新节奏",
        "tags": ["轻松对谈", "横屏 16:9", "本地引擎", "3 确认点"],
        "builtin": True,
        "config": {
            "aspect": "landscape",
            "voiceBindings": [
                {"characterId": "A", "providerProfileId": "mock-tts", "modelId": "CosyVoice2-0.5B · 参考音"},
                {"characterId": "B", "providerProfileId": "mock-tts", "modelId": "CosyVoice2-0.5B · 参考音"},
            ],
        },
    },
    {
        "id": "tpl-interview",
        "name": "人物访谈",
        "desc": "A·主持（追问）/ B·嘉宾（观点输出）· 深度问答结构 · 可绑定 MiniMax 音色",
        "tags": ["深度问答", "横屏 16:9", "本地 + MiniMax", "3 确认点"],
        "builtin": True,
        "config": {
            "aspect": "landscape",
            "voiceBindings": [
                {"characterId": "A", "providerProfileId": "mock-tts", "modelId": "CosyVoice2-0.5B · 增强"},
                {"characterId": "B", "providerProfileId": "minimax", "modelId": "CosyVoice2-0.5B · 低沉"},
            ],
        },
    },
]


def _sanitize_config(config: dict) -> dict:
    """模板 config 只收白名单字段；voiceBindings 逐条清洗为引用结构。"""
    if not isinstance(config, dict):
        return {}
    out: dict = {}
    if config.get("aspect") in ("landscape", "portrait"):
        out["aspect"] = config["aspect"]
    bindings = config.get("voiceBindings")
    if isinstance(bindings, list):
        clean = []
        for b in bindings:
            if not isinstance(b, dict):
                continue
            entry = {
                "id": f"VB-{str(b.get('characterId', ''))[:1].upper() or 'X'}",
                "characterId": str(b.get("characterId", ""))[:8],
                "providerProfileId": str(b.get("providerProfileId", ""))[:64],
                "modelId": str(b.get("modelId", ""))[:128],
            }
            if entry["characterId"] in ("A", "B"):
                clean.append(entry)
        out["voiceBindings"] = clean
    return out


class TemplateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._file = root / "templates.json"
        if not self._file.exists():
            seeded = [{**t, "config": _sanitize_config(t["config"])} for t in BUILTIN_TEMPLATES]
            self.save({"templates": seeded})

    def _data(self) -> dict:
        return json.loads(self._file.read_text(encoding="utf-8"))

    def save(self, data: dict) -> None:
        tmp = self._file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._file)

    def list(self) -> list[dict]:
        return self._data().get("templates", [])

    def get(self, template_id: str) -> dict | None:
        return next((t for t in self.list() if t.get("id") == template_id), None)

    def create(self, name: str, desc: str, config: dict, tags: list[str] | None = None) -> dict:
        tpl = {
            "id": f"tpl-{uuid.uuid4().hex[:10]}",
            "name": str(name).strip()[:64] or "未命名模板",
            "desc": str(desc).strip()[:200],
            "tags": [str(t)[:24] for t in (tags or [])][:8],
            "builtin": False,
            "config": _sanitize_config(config),
        }
        data = self._data()
        data["templates"].append(tpl)
        self.save(data)
        return tpl

    def delete(self, template_id: str) -> dict | None:
        """删除自定义模板；内置模板拒绝删除（返回 None 由 API 层转 403/422 语义）。"""
        data = self._data()
        target = self.get(template_id)
        if target is None:
            return None
        if target.get("builtin"):
            raise ValueError("BUILTIN_TEMPLATE: 内置模板不可删除")
        data["templates"] = [t for t in data["templates"] if t.get("id") != template_id]
        self.save(data)
        return target
