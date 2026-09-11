"""mock 适配器（关口 A/B 之前无真实提供方时的联调实现）。

行为对齐契约：文本返回结构化话轮草稿、TTS 返回权威 sampleCount、图片/视频返回占位产物。
延迟参数化，模拟长任务并驱动 Job 阶段事件。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any


def _content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
    return h.hexdigest()[:16]


class MockTextProvider:
    name = "mock-text"

    def __init__(self, delay_ms: int = 600) -> None:
        self.delay_ms = delay_ms
        self.capabilities = {
            "simulated": True,
            "streaming": True,
            "maxContextChars": 200_000,
            "structuredOutput": True,
            "paid": False,
        }

    async def generate(self, req: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(self.delay_ms / 1000)
        source = req.get("sourceInput", {})
        content = source.get("content", "")
        brief = req.get("brief", "")
        return self._build_script(content, brief, kind=source.get("kind", "article"))

    async def rewrite(self, req: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(self.delay_ms / 1000)
        # 确定性轻改写（演示候选差异流）：选中话轮应用通用口语化变换
        turn_ids = set(req.get("turnIds", []))
        changed: list[str] = []
        turns = []
        for turn in req.get("turns", []):
            if turn.get("id") not in turn_ids:
                turns.append(turn)
                continue
            new_lines = []
            for line in turn.get("lines", []):
                text = line.get("displayText", "")
                new_text = self._light_edit(text)
                if new_text != text:
                    changed.append(turn["id"])
                new_lines.append({**line, "displayText": new_text, "spokenText": new_text})
            turns.append({**turn, "lines": new_lines})
        return {"turns": turns, "changedTurnIds": sorted(set(changed))}

    @staticmethod
    def _light_edit(text: str) -> str:
        """通用轻改写：但→不过；所以你的意思是→也就是说；删去「那么」。"""
        t = text.replace("但是", "不过").replace("然而", "不过")
        if t.startswith("所以你的意思是"):
            t = "也就是说" + t[len("所以你的意思是"):]
        t = t.replace("那么，", "").replace("那么 ", "")
        return t

    @staticmethod
    def _build_script(content: str, brief: str = "", kind: str = "article") -> dict[str, Any]:
        # 简化拆分：按段落轮流分配给 A / B，产出结构化话轮（真实实现见 02 §5.1 单元划分 + script_svc 两次生成）
        paragraphs = [p.strip() for p in content.split("\n") if p.strip()] or [content or "（空输入）"]
        turns = []
        for i, para in enumerate(paragraphs):
            speaker = "A" if i % 2 == 0 else "B"
            turn_id = f"T{i + 1:02d}"
            line_id = f"L{(i * 2) + 1:03d}"
            turns.append({
                "id": turn_id,
                "speaker": speaker,
                "intent": "explain",
                "tone": "自然",
                "speedRatio": 1.0,
                "lines": [{"id": line_id, "displayText": para, "spokenText": para}],
            })
        return {
            "revisionId": f"R{mock_script_counter()}",
            "sourceInput": {"kind": kind, "content": content},
            "turns": turns,
            "textApiConfigRef": "mock-text",
        }


_counter = [0]


def mock_script_counter() -> int:
    _counter[0] += 1
    return _counter[0]


class MockImageProvider:
    name = "mock-image"

    def __init__(self, delay_ms: int = 400, artifacts_root: Path | None = None) -> None:
        self.delay_ms = delay_ms
        self.artifacts_root = artifacts_root
        self.capabilities = {
            "simulated": True,
            "textToImage": True,
            "singleImageEdit": True,
            "multiRefEdit": False,
            "aspectRatios": ["landscape_16_9", "portrait_16_9"],
            "async_": True,
            "cancel": False,
            "download": True,
            "paid": False,
        }

    async def generate(self, req: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(self.delay_ms / 1000)
        # 占位：写一个文本元数据文件充当产物（真实实现为图片 API 下载与校验）
        return {"placeholder": True, "aspect": req.get("aspect", "landscape"), "url": ""}


class MockTTSProvider:
    name = "mock-tts"

    def __init__(self, delay_ms: int = 800, sample_rate: int = 48000) -> None:
        self.delay_ms = delay_ms
        self.sample_rate = sample_rate
        self.capabilities = {
            "simulated": True,
            "voiceCloning": True,
            "emotion": True,
            "speed": True,
            "pause": True,
            "pronunciation": True,
            "timestamps": True,
            "streaming": True,
            "cancel": False,
            "paid": False,
        }

    async def synthesize(self, req: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(self.delay_ms / 1000)
        text = "".join(req.get("lineTexts", []))
        chars = max(len(text), 4)
        # 仅为演示估算，不能当作真实音频测量值。
        speed = float(req.get("speedRatio", 1.0))
        if speed <= 0:
            raise ValueError("语速必须大于 0")
        sample_count = int(chars * 0.22 * self.sample_rate / speed)
        return {
            "sampleRate": self.sample_rate,
            "sampleCount": sample_count,
            "durationMs": round(sample_count * 1000 / self.sample_rate),
            "providerTaskId": f"mock-{_content_hash(text)[:8]}",
        }

    async def audition(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self.synthesize(req)


class MockVideoProvider:
    name = "mock-video"

    def __init__(self, delay_ms: int = 1200) -> None:
        self.delay_ms = delay_ms
        self.capabilities = {
            "simulated": True,
            "dualTrackInput": True,
            "resolutions": ["1280x720", "1920x1080"],
            "fps": [24, 30],
            "progress": True,
            "cancel": False,
            "paid": False,
        }

    async def submit(self, req: dict[str, Any]) -> str:
        await asyncio.sleep(self.delay_ms / 1000)
        return f"mock-prompt-{_content_hash(json.dumps(req, ensure_ascii=False))[:8]}"
