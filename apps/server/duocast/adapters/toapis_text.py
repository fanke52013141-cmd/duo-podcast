"""Structured dialogue generation and selected-turn rewriting through ToAPIs."""
from __future__ import annotations

import json
import httpx

from .mock import MockTextProvider


class ToAPIsTextProvider:
    name = "toapis-text"

    def __init__(self, api_key: str, model: str = "deepseek-flash"):
        self.api_key = api_key
        self.model = model
        self.capabilities = {"simulated": False, "streaming": False, "maxContextChars": 80000,
                             "structuredOutput": True, "paid": True, "model": model}

    async def _chat(self, system, user):
        response = None
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            for base in ("https://toapis.com", "https://toapis.cn"):
                try:
                    response = await client.post(base + "/v1/chat/completions",
                        headers={"Authorization": "Bearer " + self.api_key}, json={"model": self.model,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                            "response_format": {"type": "json_object"}, "max_tokens": 5000})
                    if response.status_code == 200:
                        break
                    if response.status_code < 500:
                        raise RuntimeError(f"TEXT_API_HTTP_{response.status_code}: 文本生成请求失败")
                except httpx.TransportError:
                    continue
        if response is None or response.status_code != 200:
            raise RuntimeError("TEXT_API_UNAVAILABLE: 文本服务暂时不可用，请重试")
        raw = response.json()["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("TEXT_BAD_OUTPUT: 模型未返回有效结构化对话") from exc

    @staticmethod
    def _normalize(turns):
        if not isinstance(turns, list) or not turns:
            raise RuntimeError("TEXT_BAD_OUTPUT: 对话为空")
        output = []
        for i, turn in enumerate(turns):
            speaker = turn.get("speaker")
            if speaker not in ("A", "B"):
                raise RuntimeError("TEXT_BAD_OUTPUT: 说话人必须为 A 或 B")
            lines = turn.get("lines", [])
            if not lines and turn.get("text"):
                lines = [{"displayText": turn["text"]}]
            normalized = [{"id": line.get("id") or f"L{i+1:03d}-{j+1}",
                "displayText": str(line.get("displayText", "")).strip(),
                "spokenText": str(line.get("spokenText") or line.get("displayText", "")).strip()}
                for j, line in enumerate(lines)]
            if not normalized or any(not line["displayText"] for line in normalized):
                raise RuntimeError("TEXT_BAD_OUTPUT: 话轮包含空台词")
            intent = turn.get("intent", "other")
            output.append({"id": turn.get("id") or f"T{i+1:02d}", "speaker": speaker,
                "intent": intent if intent in {"explain", "probe", "example", "challenge", "summarize", "other"} else "other",
                "lines": normalized, "tone": str(turn.get("tone") or "自然"), "speedRatio": 1.0})
        return output

    async def generate(self, req):
        source = req.get("sourceInput", {})
        content = str(source.get("content", ""))
        if source.get("kind") == "script":
            # Importing authored dialogue is a real parser operation, without rewriting it.
            result = MockTextProvider._build_script(content, kind="script")
            result["textApiConfigRef"] = "script-import"
            return result
        if not content.strip() or len(content) > 80000:
            raise ValueError("请输入 1–80000 字的主题或文章")
        result = await self._chat(
            '你为双人中文播客生成自然对话，只输出 JSON。结构为 {"contentBrief":{"coreQuestion":"",'
            '"keyPoints":[],"necessaryFacts":[],"paragraphGoals":[],"speakerDuties":""},"turns":['
            '{"speaker":"A或B","intent":"explain或probe或example或challenge或summarize或other",'
            '"tone":"自然","lines":[{"displayText":"台词","spokenText":"朗读台词"}]}]}。'
            'A负责提问推进，B负责观点与例子。至少4个话轮，A/B交替，有开场和收尾。'
            '依据用户输入，不编造数据或引用。优先遵守用户的长度要求；未要求时总字数150–250字。',
            json.dumps({"source": source, "brief": req.get("brief", {})}, ensure_ascii=False))
        result["turns"] = self._normalize(result.get("turns"))
        if len(result["turns"]) < 3 or {t["speaker"] for t in result["turns"]} != {"A", "B"}:
            raise RuntimeError("TEXT_BAD_OUTPUT: 对谈必须包含双方发言")
        result.update(sourceInput=source, textApiConfigRef=f"{self.name}:{self.model}")
        return result

    async def rewrite(self, req):
        selected = req.get("turns", [])
        result = await self._chat(
            '只改写用户选中的话轮，输出 JSON {"turns":[...]}。保持原话轮 id、speaker、每条 line 的 id。'
            '保留事实，按 mode 执行：trim精简、dedupe去重、casual口语化、probe增加具体追问、rewrite自然改写。'
            'lines 必须有 displayText 和 spokenText。不要添加或删除话轮。',
            json.dumps({"mode": req.get("mode", "rewrite"), "turns": selected}, ensure_ascii=False))
        result["turns"] = self._normalize(result.get("turns"))
        originals = {turn["id"]: turn for turn in selected}
        if set(originals) != {turn["id"] for turn in result["turns"]}:
            raise RuntimeError("TEXT_BAD_OUTPUT: 改写改变了话轮范围")
        for turn in result["turns"]:
            before = originals[turn["id"]]
            if turn["speaker"] != before["speaker"] or [l["id"] for l in turn["lines"]] != [l["id"] for l in before["lines"]]:
                raise RuntimeError("TEXT_BAD_OUTPUT: 改写改变了角色或台词范围")
        result["changedTurnIds"] = [turn["id"] for turn in result["turns"]
            if [l["displayText"] for l in turn["lines"]] != [l["displayText"] for l in originals[turn["id"]]["lines"]]]
        return result
