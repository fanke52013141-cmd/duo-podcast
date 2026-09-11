"""脚本业务编排（06 §3.2 script_svc）：两次生成、选区改写、候选版本登记。mock 阶段为单次生成。"""

from __future__ import annotations

import uuid
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


def build_script_revision(project: Project, provider: TextProvider, result: dict[str, Any]) -> ScriptRevision:
    source_input = result.get("sourceInput", {})
    rev = ScriptRevision(
        id=f"R{uuid.uuid4().hex[:12].upper()}",
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
