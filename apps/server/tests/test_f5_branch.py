"""F5 分支：从任一版本派生新草稿，原版本与确认记录保持不变。"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from duocast.api.script import branch_script
from duocast.domain.project import Approval, Line, ScriptRevision, Turn
from duocast.storage.project_store import ProjectStore


def _setup(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    project = store.create("branch-case")
    revision = ScriptRevision(id="R1", label="原稿", turns=[
        Turn(id="T1", speaker="A", lines=[Line(id="L1", spoken_text="第一句")]),
        Turn(id="T2", speaker="B", lines=[Line(id="L2", spoken_text="第二句")]),
    ])
    saved = store.apply(project.id, {
        "script_revisions": [revision],
        "current_draft_revision": "R1",
        "approvals": [Approval(kind="script", input_revision_id="R1")],
    }, 0)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_store=store)))
    return store, saved, request


def test_branch_copies_turns_and_becomes_current_draft(tmp_path):
    store, project, request = _setup(tmp_path)
    result = asyncio.run(branch_script("branch-case", {"label": "开场更活泼版"}, request))

    assert result["parentRevisionId"] == "R1"
    assert result["currentDraftRevision"] == result["revisionId"] != "R1"

    saved = store.load("branch-case")
    assert len(saved.script_revisions) == 2
    branch = saved.script_revisions[-1]
    assert branch.parent_revision_id == "R1"
    assert branch.label == "开场更活泼版"
    assert [t.id for t in branch.turns] == ["T1", "T2"]
    # 原版本与确认记录保持不变
    assert saved.script_revisions[0].turns[0].lines[0].spoken_text == "第一句"
    assert [a.kind for a in saved.approvals] == ["script"]


def test_branch_from_historical_revision(tmp_path):
    store, project, request = _setup(tmp_path)
    first = asyncio.run(branch_script("branch-case", {"fromRevisionId": "R1", "label": "B 方案"}, request))
    second = asyncio.run(branch_script(
        "branch-case", {"fromRevisionId": first["revisionId"], "label": "从 B 再分"}, request))

    saved = store.load("branch-case")
    assert len(saved.script_revisions) == 3
    assert saved.script_revisions[-1].parent_revision_id == first["revisionId"]
    assert saved.current_draft_revision == second["revisionId"]


def test_branch_rejects_unknown_revision(tmp_path):
    store, project, request = _setup(tmp_path)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(branch_script("branch-case", {"fromRevisionId": "R99"}, request))
    assert exc.value.status_code == 422
