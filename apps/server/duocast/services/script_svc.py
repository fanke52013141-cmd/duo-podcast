"""脚本业务编排（06 §3.2 script_svc）：两次生成、选区改写、候选版本登记。mock 阶段为单次生成。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..adapters.base import TextProvider
from ..domain.project import ContentBrief, Project, ScriptRevision, Turn
from ..storage.project_store import ProjectStore


def _turn_from_dict(data: dict[str, Any]) -> Turn:
    lines = [{"id": ln["id"], "display_text": ln["displayText"], "spoken_text": ln["spokenText"]}
             for ln in data.get("lines", [])]
    return Turn(
        id=data["id"],
        speaker=data["speaker"],
        intent=data.get("intent", "other"),
        lines=lines,
        tone=data.get("tone", "自然"),
        speed_ratio=data.get("speedRatio", 1.0),
    )


def next_revision_id(project: Project) -> str:
    """顺序版本号（与前端 nextRevisionId 同一口径）：R 后缀数值 + 1。

    此前服务端用十六进制随机 id、前端编辑用 R1/R2，同一工程出现两套版本号口径
    （用户看到 R39A0258285A3 → R1 跳变）；统一为 R1、R2、R3…。
    """
    max_num = 0
    for r in project.script_revisions:
        digits = r.id[1:] if r.id.startswith("R") else ""
        if digits.isdigit():
            max_num = max(max_num, int(digits))
    return f"R{max_num + 1}"


def build_script_revision(project: Project, provider: TextProvider, result: dict[str, Any]) -> ScriptRevision:
    source_input = result.get("sourceInput", {})
    brief_data = result.get("contentBrief") or {}
    rev = ScriptRevision(
        id=next_revision_id(project),
        # 三入口（topic/article/script）的 kind 由提供方结果透传，不再硬编码（11 报告 P2-6）
        source_input={"kind": source_input.get("kind", "article"),
                      "content": source_input.get("content", "")},
        # 内容结构由提供方结果透传（mock 也填充演示值）；旧行为是永远空 ContentBrief，检查器全是「—」
        content_brief=ContentBrief.model_validate(brief_data) if brief_data else ContentBrief(),
        turns=[_turn_from_dict(t) for t in result.get("turns", [])],
        text_api_config_ref=result.get("textApiConfigRef", provider.name),
    )
    return rev


def apply_script_revision(store: ProjectStore, project_id: str, revision: ScriptRevision,
                          expected_project_revision: int | None = None) -> Project:
    project = store.load(project_id)
    if project is None:
        raise KeyError(f"project not found: {project_id}")
    # 候选版本登记：追加到 revisions，currentDraftRevision 指向最新
    patch = {
        "script_revisions": [r.model_dump(by_alias=True) for r in project.script_revisions] + [revision.model_dump(by_alias=True)],
    }
    if expected_project_revision is not None and project.revision == expected_project_revision:
        patch["current_draft_revision"] = revision.id
    return store.apply(project_id, patch, expected_revision=project.revision)


# ---- F5 分支：版本对比与合并（支持行级） ----

def turn_plain_text(turn: Turn) -> str:
    return "\n".join(line.display_text for line in turn.lines)


def diff_revisions(base: ScriptRevision, source: ScriptRevision) -> dict[str, Any]:
    """对比两个版本：话轮级（增删/说话人变化）+ 行级（新增/删除/改写）。"""
    base_turns = {turn.id: turn for turn in base.turns}
    source_turns = {turn.id: turn for turn in source.turns}
    turns: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []

    for bt in base.turns:
        st = source_turns.get(bt.id)
        if st is None:
            turns.append({"turnId": bt.id, "speaker": bt.speaker, "status": "removed",
                          "text": turn_plain_text(bt)})
            continue
        if bt.speaker != st.speaker:
            turns.append({"turnId": bt.id, "speaker": st.speaker, "status": "changed", "text": ""})
        source_lines = {line.id: line for line in st.lines}
        base_line_ids = {line.id for line in bt.lines}
        for bl in bt.lines:
            sl = source_lines.get(bl.id)
            if sl is None:
                lines.append({"turnId": bt.id, "lineId": bl.id, "status": "removed",
                              "speaker": bt.speaker, "before": bl.display_text, "after": ""})
            elif (sl.display_text, sl.spoken_text) != (bl.display_text, bl.spoken_text):
                lines.append({"turnId": bt.id, "lineId": bl.id, "status": "changed",
                              "speaker": bt.speaker, "before": bl.display_text, "after": sl.display_text})
        for sl in st.lines:
            if sl.id not in base_line_ids:
                lines.append({"turnId": bt.id, "lineId": sl.id, "status": "added",
                              "speaker": bt.speaker, "before": "", "after": sl.display_text})

    for st in source.turns:
        if st.id not in base_turns:
            turns.append({"turnId": st.id, "speaker": st.speaker, "status": "added",
                          "text": turn_plain_text(st)})

    counts = {status: sum(1 for item in lines if item["status"] == status)
              for status in ("added", "removed", "changed")}
    turn_counts = {status: sum(1 for item in turns if item["status"] == status)
                   for status in ("added", "removed", "changed")}
    return {"turns": turns, "lines": lines, "counts": counts, "turnCounts": turn_counts}


def merge_revision(project: Project, base: ScriptRevision, source: ScriptRevision,
                   turn_ids: list[str] | None, label: str = "",
                   line_ids: list[str] | None = None) -> ScriptRevision:
    """以 base 为底，把 source 中选中的话轮/行合并成新草稿版本。

    turn_ids 管「整话轮的新增/删除」；line_ids 管「同一话轮内逐行改写/新增/删除」，
    格式 `T05:L010`。两者都为 None 时，默认合并全部改写/新增（删除需显式选）。
    """
    diff = diff_revisions(base, source)
    # 显式给了任一选择就按选择执行；两者都不给才用「全部改写/新增」的默认策略。
    if turn_ids is None and line_ids is None:
        chosen_turns = {t["turnId"] for t in diff["turns"] if t["status"] == "added"}
        chosen_lines = {f'{l["turnId"]}:{l["lineId"]}' for l in diff["lines"]
                        if l["status"] in ("changed", "added")}
    else:
        chosen_turns = set(turn_ids or [])
        chosen_lines = set(line_ids or [])

    source_turns = {turn.id: turn for turn in source.turns}
    result: list[Turn] = [deepcopy(turn) for turn in base.turns]
    by_id = {turn.id: turn for turn in result}

    # 1) 整话轮的新增 / 删除
    for turn_id in chosen_turns:
        if turn_id in by_id and turn_id not in source_turns:
            result.remove(by_id.pop(turn_id))
    for st in source.turns:
        if st.id in chosen_turns and st.id not in by_id:
            added = deepcopy(st)
            result.append(added)
            by_id[st.id] = added

    # 2) 行级改动：按 source 中的顺序处理，保证新增行的插入位置稳定
    def line_order(token: str) -> tuple[int, int]:
        turn_id, _, line_id = token.partition(":")
        st = source_turns.get(turn_id)
        if st is None:
            return (1, 0)
        return (0, next((i for i, line in enumerate(st.lines) if line.id == line_id), 0))

    for token in sorted(chosen_lines, key=line_order):
        turn_id, _, line_id = token.partition(":")
        bt, st = by_id.get(turn_id), source_turns.get(turn_id)
        if bt is None or st is None:
            continue
        source_line = next((line for line in st.lines if line.id == line_id), None)
        existing = next((line for line in bt.lines if line.id == line_id), None)
        if source_line is not None:
            if existing is not None:
                existing.display_text = source_line.display_text
                existing.spoken_text = source_line.spoken_text
            else:
                new_line = deepcopy(source_line)
                insert_at = 0
                for previous in reversed(st.lines[:st.lines.index(source_line)]):
                    pos = next((i for i, line in enumerate(bt.lines) if line.id == previous.id), None)
                    if pos is not None:
                        insert_at = pos + 1
                        break
                bt.lines.insert(insert_at, new_line)
        else:
            bt.lines = [line for line in bt.lines if line.id != line_id]
            if not bt.lines:
                result.remove(bt)
                by_id.pop(turn_id, None)

    return ScriptRevision(
        id=next_revision_id(project),
        source_input=deepcopy(base.source_input),
        content_brief=deepcopy(base.content_brief),
        turns=result,
        text_api_config_ref=base.text_api_config_ref,
        parent_revision_id=base.id,
        label=(label or f"合并自 {source.id}")[:40],
    )
