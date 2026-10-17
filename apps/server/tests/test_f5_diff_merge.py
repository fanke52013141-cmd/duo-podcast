"""F5 对比/合并（行级）与阶段③ 单元级进度。"""

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from duocast.api.script import diff_script, merge_script
from duocast.domain.project import Line, ScriptRevision, Turn
from duocast.services.script_svc import diff_revisions
from duocast.services.voice_svc import synthesize_timeline
from duocast.storage.project_store import ProjectStore


def _turn(tid: str, speaker: str, *texts: str) -> Turn:
    return Turn(id=tid, speaker=speaker, lines=[
        Line(id=f"L-{tid}-{i}", spoken_text=text, display_text=text) for i, text in enumerate(texts)
    ])


def _setup(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    project = store.create("diff-case")
    r1 = ScriptRevision(id="R1", turns=[_turn("T1", "A", "开头"), _turn("T2", "B", "第二句")])
    r2 = ScriptRevision(id="R2", turns=[
        _turn("T1", "A", "开头"), _turn("T2", "B", "第二句改过"), _turn("T3", "A", "新增一句"),
    ])
    saved = store.apply(project.id, {
        "script_revisions": [r1, r2], "current_draft_revision": "R2",
    }, 0)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_store=store)))
    return store, saved, request, r1, r2


def test_diff_reports_line_and_turn_changes(tmp_path):
    _, _, _, r1, r2 = _setup(tmp_path)
    diff = diff_revisions(r1, r2)

    line_changes = {(c["turnId"], c["lineId"]): c for c in diff["lines"]}
    assert line_changes[("T2", "L-T2-0")]["status"] == "changed"
    assert line_changes[("T2", "L-T2-0")]["after"] == "第二句改过"
    # T3 整条新增属于话轮级
    assert [t["turnId"] for t in diff["turns"] if t["status"] == "added"] == ["T3"]
    assert diff["counts"]["changed"] == 1
    assert diff["turnCounts"]["added"] == 1


def test_merge_line_level_inside_same_turn(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    project = store.create("line-merge")
    base = ScriptRevision(id="R1", turns=[_turn("T1", "A", "前半句", "后半句")])
    left = ScriptRevision(id="R2", turns=[_turn("T1", "A", "前半句改", "后半句")])
    right = ScriptRevision(id="R3", turns=[_turn("T1", "A", "前半句", "后半句改")])
    store.apply(project.id, {"script_revisions": [base, left, right], "current_draft_revision": "R3"}, 0)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_store=store)))

    first = asyncio.run(merge_script("line-merge", {
        "baseRevisionId": "R1", "sourceRevisionId": "R2", "lineIds": ["T1:L-T1-0"],
        "label": "并入前半句",
    }, request))
    first_saved = store.load("line-merge")
    merged1 = next(r for r in first_saved.script_revisions if r.id == first["revisionId"])
    assert [line.display_text for line in merged1.turns[0].lines] == ["前半句改", "后半句"]

    second = asyncio.run(merge_script("line-merge", {
        "baseRevisionId": merged1.id, "sourceRevisionId": "R3", "lineIds": ["T1:L-T1-1"],
        "label": "再并后半句",
    }, request))
    second_saved = store.load("line-merge")
    merged2 = next(r for r in second_saved.script_revisions if r.id == second["revisionId"])
    assert [line.display_text for line in merged2.turns[0].lines] == ["前半句改", "后半句改"]


def test_merge_turn_level_add_and_delete(tmp_path):
    store, project, request, _, _ = _setup(tmp_path)
    added = asyncio.run(merge_script("diff-case", {
        "baseRevisionId": "R1", "sourceRevisionId": "R2", "turnIds": ["T3"], "label": "只要新增句",
    }, request))
    saved = store.load("diff-case")
    merged = next(r for r in saved.script_revisions if r.id == added["revisionId"])
    assert [t.id for t in merged.turns] == ["T1", "T2", "T3"]
    # 未选中的改写不进来
    assert merged.turns[1].lines[0].display_text == "第二句"

    removed = asyncio.run(merge_script("diff-case", {
        "baseRevisionId": "R2", "sourceRevisionId": "R1", "turnIds": ["T3"], "label": "删掉新增句",
    }, request))
    saved = store.load("diff-case")
    merged2 = next(r for r in saved.script_revisions if r.id == removed["revisionId"])
    assert [t.id for t in merged2.turns] == ["T1", "T2"]


def test_diff_endpoint_counts(tmp_path):
    _, _, request, _, _ = _setup(tmp_path)
    payload = asyncio.run(diff_script("diff-case", "R1", "R2", request))
    assert payload["counts"]["changed"] == 1
    assert payload["turnCounts"]["added"] == 1

    with pytest.raises(HTTPException) as exc:
        asyncio.run(diff_script("diff-case", "R1", "R99", request))
    assert exc.value.status_code == 422


class _TTS:
    capabilities = {"simulated": True}

    async def synthesize(self, request):
        return {"sampleRate": 48000, "sampleCount": 48000, "audioAssetId": "AUD-1"}


def test_synthesis_reports_unit_progress(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    project = store.create("progress-case")
    revision = ScriptRevision(id="R1", turns=[_turn("T1", "A", "一"), _turn("T2", "B", "二"), _turn("T3", "A", "三")])
    project = store.apply(project.id, {"script_revisions": [revision], "current_draft_revision": "R1"}, 0)

    events: list[tuple[int, int]] = []
    asyncio.run(synthesize_timeline(
        store, _TTS(), project, revision, {}, progress=lambda done, total: events.append((done, total))))

    assert events == [(1, 3), (2, 3), (3, 3)]
