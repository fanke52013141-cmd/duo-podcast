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
        # 确定性轻改写（演示候选差异流）：选中话轮按 mode 应用变换，兜底保证候选非空——
        # 文本没有触发词时旧实现返回「共改 0 处」，用户只能接受一个空差异（第四轮实测发现）。
        turn_ids = set(req.get("turnIds", []))
        mode = req.get("mode", "rewrite")
        changed: list[str] = []
        turns = []
        for turn in req.get("turns", []):
            if turn.get("id") not in turn_ids:
                turns.append(turn)
                continue
            new_lines = []
            for line in turn.get("lines", []):
                text = line.get("displayText", "")
                new_text = self._light_edit(text, mode)
                if new_text != text:
                    changed.append(turn["id"])
                new_lines.append({**line, "displayText": new_text, "spokenText": new_text})
            turns.append({**turn, "lines": new_lines})
        return {"turns": turns, "changedTurnIds": sorted(set(changed))}

    @staticmethod
    def _light_edit(text: str, mode: str = "rewrite") -> str:
        """通用轻改写：按改写意图做确定性替换；无触发词时兜底一处可见变化。"""
        if not text.strip():
            return text
        t = text
        if mode == "casual":  # 更口语化
            t = t.replace("但是", "不过").replace("然而", "不过").replace("因此", "所以")
        elif mode == "dedupe":  # 减少重复：去掉口头禅与重复句
            t = t.replace("然后，", "").replace("然后", "接着", 1)
            sentences = [s for s in t.split("。") if s]
            deduped: list[str] = []
            for s in sentences:
                if s not in deduped:
                    deduped.append(s)
            if len(deduped) != len(sentences):
                t = "。".join(deduped) + ("。" if t.rstrip().endswith("。") else "")
        elif mode == "trim":  # 精简：删语气词与程度副词
            t = t.replace("其实", "").replace("真的", "").replace("非常", "很").replace("  ", " ")
        elif mode == "probe":  # 增加追问：句尾补一个具体追问
            core = t.rstrip("。！？!?. ")
            t = f"{core}，为什么这么说呢？" if core else t
        else:  # rewrite：通用
            t = t.replace("但是", "不过").replace("然而", "不过")
            if t.startswith("所以你的意思是"):
                t = "也就是说" + t[len("所以你的意思是"):]
        t = t.replace("那么，", "").replace("那么 ", "")
        if t == text:  # 兜底：保证候选非空（演示差异流可用性）
            t = ("说实话，" + text) if mode in ("rewrite", "casual") else (text + "，可以这样理解。")
        return t

    @staticmethod
    def _build_script(content: str, brief: str = "", kind: str = "article") -> dict[str, Any]:
        """演示脚本生成：三种入口都产出 A/B 交替、≥3 轮、含收尾的双人对话。

        此前按段落机械轮流：单段话题输入只得到 1 条 A 话轮（B 全程无台词），
        样片要求的 A→B→A 区间必然缺失（第四轮全流程实测发现）。
        """
        text = (content or "").strip()
        if kind == "script":
            # 已有脚本：每行一条发言，`A:` / `B:` 标记说话人（01 §3 三入口契约），无标记时轮流分配
            parsed: list[tuple[str, str]] = []
            for raw in text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                marker = line[:2]
                if marker in ("A:", "A：", "B:", "B："):
                    parsed.append((marker[0], line[2:].strip()))
                else:
                    parsed.append(("", line))
            entries: list[tuple[str, str]] = []
            last = "B"
            for spk, line in parsed:
                entries.append((spk or ("A" if last == "B" else "B"), line))
                last = entries[-1][0]
        else:
            entries = MockTextProvider._entries_from_prose(text, kind)

        # 相邻同说话人合并成一条话轮的多行（双人对话语义：A/B 交替）
        merged: list[tuple[str, list[str]]] = []
        for spk, line in entries:
            if merged and merged[-1][0] == spk and len(merged[-1][1]) < 2:
                merged[-1][1].append(line)
            else:
                merged.append((spk, [line]))

        turns = []
        for i, (speaker, lines) in enumerate(merged):
            first = lines[0]
            intent = "summarize" if i == len(merged) - 1 else ("probe" if speaker == "A" and i > 0 else "explain")
            turns.append({
                "id": f"T{i + 1:02d}",
                "speaker": speaker,
                "intent": intent,
                "tone": "自然",
                "speedRatio": 1.0,
                "lines": [{"id": f"L{i * 10 + j + 1:03d}", "displayText": ln, "spokenText": ln}
                          for j, ln in enumerate(lines)],
            })
        return {
            "revisionId": f"R{mock_script_counter()}",
            "sourceInput": {"kind": kind, "content": content},
            "contentBrief": MockTextProvider._build_brief(text, kind),
            "turns": turns,
            "textApiConfigRef": "mock-text",
        }

    @staticmethod
    def _entries_from_prose(text: str, kind: str) -> list[tuple[str, str]]:
        """话题 / 文章 → A/B 交替发言序列（保证以 A 开场、A→B→A、以 A 收尾）。"""
        if not text:
            text = "（空输入）"
        sentences = [s.strip() for s in text.replace("！", "。").replace("？", "。").split("。") if s.strip()]
        head = sentences[0] if sentences else text[:40]
        if kind == "topic":
            topic = head if len(head) <= 60 else head[:57] + "…"
            rest = [s for s in sentences[1:] if s != head]
            point_a = rest[0] if rest else "技术和听众信任其实是两条线，不能混在一起谈。"
            point_b = rest[1] if len(rest) > 1 else "商业上数字人确实能降本，但观众认的是人，不是皮。"
            entries = [
                ("A", f"欢迎回到本期节目。今天我们聊一个听众问得很多的话题：{topic}。"),
                ("B", f"这个话题我很有感触。我的基本判断是：{point_b}"),
                ("A", f"我先补一个背景：{point_a}那听众为什么还是更信任真人？"),
                ("B", "因为播客的本质是人对人的表达——观点、犹豫、临场的反应都是信任的来源，念稿机器给不了。"),
                ("A", "所以工具会变，内容的核心不变。今天我们先聊到这里。"),
                ("A", "感谢大家收听，我们下期再见。"),
            ]
            return entries
        # article：句子两两分组交替分配；不足 3 轮时补开场/收尾模板
        groups = ["。".join(sentences[i:i + 2]) + "。" for i in range(0, len(sentences), 2) if sentences[i]]
        groups = [g for g in groups if g.strip("。")]
        if len(groups) < 3:
            filler = [
                ("A", f"今天我们围绕这期内容展开聊：{head[:40]}。"),
                ("B", groups[0] if groups else "我先说说我的整体看法。"),
                ("A", groups[1] if len(groups) > 1 else "我补充一个角度。"),
                ("B", groups[2] if len(groups) > 2 else "对，这两点放在一起看会更清楚。"),
                ("A", "好，本期先聊到这里，感谢收听，我们下期再见。"),
            ]
            return filler
        entries = []
        for i, g in enumerate(groups[:14]):  # 演示上限：超长文章截到 14 轮
            spk = "A" if i % 2 == 0 else "B"
            if i == 0:
                g = f"欢迎回到本期节目。{g}"
            if i == min(13, len(groups) - 1):
                g = f"{g}今天就先聊到这里，感谢收听。"
            entries.append((spk, g))
        return entries

    @staticmethod
    def _build_brief(text: str, kind: str) -> dict[str, Any]:
        """内容结构（ContentBrief）：从输入中提取演示值，检查器不再全是「—」。"""
        if kind == "script":
            text = "\n".join(ln[2:].strip() if ln[:2] in ("A:", "A：", "B:", "B：") else ln
                             for ln in text.splitlines())
        sentences = [s.strip() for s in text.replace("！", "。").replace("？", "。").split("。") if s.strip()]
        core = sentences[0] if sentences else ""
        if len(core) > 50:
            core = core[:47] + "…"
        facts = [s for s in sentences if any(ch.isdigit() for ch in s)][:2]
        return {
            "coreQuestion": core or "本期核心议题待补充",
            "keyPoints": [s[:40] for s in sentences[1:4]],
            "necessaryFacts": [s[:40] for s in facts],
            "speakerDuties": "A 负责开场、提问与推进节奏；B 负责输出观点与举例。",
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
