"""F5 对比/合并，以及阶段③ 单元级进度回调。"""

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from duocast.api.script import diff_script, merge_script
from duocast.domain.project import Line, ScriptRevision, Turn
from duocast.services.script_svc import diff_revisions, merge_revision
from duocast.services.voice_svc import synthesize_timeline
from duocast.storage.project_store import ProjectStore


def _turn(tid: str, speaker: str, text: str) -> Turn:
    return Turn(id=tid, speaker=speaker, lines=[Line(id=f"L-{tid}", spoken_text=text, display_text=text)])


def _setup(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    project = store.create("diff-case")
    r1 = ScriptRevision(id="R1", turns=[_turn("T1", "A", "第一句"), _turn("T2", "B", "第二句")])
    r2 = ScriptRevision(id="R2", turns=[
        _turn("T1", "A", "第一句"), _turn("T2", "B", "第二句改过"), _turn("T3", "A", "新增一句"),
    ])
    saved = store.apply(project.id, {
        "script_revisions": [r1, r2], "current_draft_revision": "R2",
    }, 0)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_store=store)))
    return store, saved, request, r1, r2


def test_diff_reports_changed_and_added(tmp_path):
    _, _, _, r1, r2 = _setup(tmp_path)
    changes = diff_revisions(r1, r2)
    by_id = {c["turnId"]: c for c in changes}
    assert by_id["T2"]["status"] == "changed"
    assert by_id["T2"]["before"] == "第二句" and by_id["T2"]["after"] == "第二句改过"
    assert by_id["T3"]["status"] == "added"
    assert "T1" not in by_id


def test_merge_applies_selected_turns_only(tmp_path):
    store, project, request, _, _ = _setup(tmp_path)
    result = asyncio.run(merge_script("diff-case", {
        "baseRevisionId": "R1", "sourceRevisionId": "R2", "turnIds": ["T2"], "label": "只并第二句",
    }, request))

    saved = store.load(project.id)
    merged = next(r for r in saved.script_revisions if r.id == result["revisionId"])
    assert merged.parent_revision_id == "R1"
    assert merged.label == "只并第二句"
    assert [t.id for t in merged.turns] == ["T1", "T2"]
    assert merged.turns[1].lines[0].display_text == "第二句改过"
    # 新增的 T3 没被选中，不应合并
    assert "T3" not in [t.id for t in merged.turns]
    assert saved.current_draft_revision == merged.id


def test_merge_can_apply_a_deletion(tmp_path):
    store, project, request, _, _ = _setup(tmp_path)
    # base=R2 有 T3，source=R1 没有；选中 T3 表示「采用 source 的删除」
    result = asyncio.run(merge_script("diff-case", {
        "baseRevisionId": "R2", "sourceRevisionId": "R1", "turnIds": ["T3"], "label": "删掉新增句",
    }, request))
    saved = store.load(project.id)
    merged = next(r for r in saved.script_revisions if r.id == result["revisionId"])
    assert [t.id for t in merged.turns] == ["T1", "T2"]


def test_diff_endpoint_counts(tmp_path):
    _, _, request, _, _ = _setup(tmp_path)
    payload = asyncio.run(diff_script("diff-case", "R1", "R2", request))
    assert payload["counts"] == {"added": 1, "removed": 0, "changed": 1}

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
    revision = ScriptRevision(id="R1", turns=[
        _turn("T1", "A", "一"), _turn("T2", "B", "二"), _turn("T3", "A", "三"),
    ])
    project = store.apply(project.id, {"script_revisions": [revision], "current_draft_revision": "R1"}, 0)

    events: list[tuple[int, int]] = []
    asyncio.run(synthesize_timeline(
        store, _TTS(), project, revision, {}, progress=lambda done, total: events.append((done, total))))

    assert events == [(1, 3), (2, 3), (3, 3)]
