"""脚本业务编排（06 §3.2 script_svc）：两次生成、选区改写、候选版本登记。mock 阶段为单次生成。"""

from __future__ import annotations

from typing import Any

from ..adapters.base import TextProvider
from ..domain.project import Project, ScriptRevision, Turn
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
    （用户看到 R39A0258285A3 → R1 跳变）；统一为 R1、R2、R3…。"""
    max_num = 0
    for r in project.script_revisions:
        digits = r.id[1:] if r.id.startswith("R") else ""
        if digits.isdigit():
            max_num = max(max_num, int(digits))
    return f"R{max_num + 1}"


def build_script_revision(project: Project, provider: TextProvider, result: dict[str, Any]) -> ScriptRevision:
    source_input = result.get("sourceInput", {})
    rev = ScriptRevision(
        id=next_revision_id(project),
        # 三入口（topic/article/script）的 kind 由提供方结果透传，不再硬编码（11 报告 P2-6）
        source_input={"kind": source_input.get("kind", "article"),
                      "content": source_input.get("content", "")},
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
