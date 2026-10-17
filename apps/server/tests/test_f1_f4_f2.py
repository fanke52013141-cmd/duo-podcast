"""F1（按单元复用合成）、F4（视觉/音频依赖拆分）、F2（确认可撤销）回归。"""

import asyncio

from duocast.domain.project import Approval, Line, ScriptRevision, StageState, Turn
from duocast.domain.visual import VisualVariant
from duocast.services.approvals import active, revoke
from duocast.services.render_inputs import visual_dependency_hash
from duocast.services.stage_svc import derive_stage_states
from duocast.services.voice_svc import synthesize_timeline
from duocast.storage.project_store import ProjectStore


class CountingTTS:
    capabilities = {"simulated": True}

    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, request):
        self.calls += 1
        return {"sampleRate": 48000, "sampleCount": 48000, "audioAssetId": f"AUD-{self.calls}"}


def _fixture(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    project = store.create("f1-cases")
    revision = ScriptRevision(id="R1", turns=[
        Turn(id="T1", speaker="A", lines=[Line(id="L1", spoken_text="第一句")]),
        Turn(id="T2", speaker="B", lines=[Line(id="L2", spoken_text="第二句")]),
    ])
    saved = store.apply(project.id, {"script_revisions": [revision], "current_draft_revision": "R1"}, 0)
    return store, saved, revision


def test_full_synthesis_reuses_unchanged_units_only(tmp_path):
    store, project, revision = _fixture(tmp_path)
    first_tts = CountingTTS()
    first = asyncio.run(synthesize_timeline(store, first_tts, project, revision, {}))
    assert first_tts.calls == 2
    assert first.reused_unit_ids == []

    # 只改第二句，新建草稿版本 R2；第一句的指纹未变
    saved = store.load(project.id)
    changed = ScriptRevision(id="R2", turns=[
        Turn(id="T1", speaker="A", lines=[Line(id="L1", spoken_text="第一句")]),
        Turn(id="T2", speaker="B", lines=[Line(id="L2", spoken_text="第二句改过了")]),
    ])
    project = store.apply(project.id, {
        "script_revisions": [*saved.script_revisions, changed],
        "current_draft_revision": "R2",
    }, saved.revision)

    second_tts = CountingTTS()
    second = asyncio.run(synthesize_timeline(store, second_tts, project, changed, {}))
    assert second_tts.calls == 1, "只应重新合成改动过的句子"
    assert second.reused_unit_ids == ["U-T1"]
    assert second.applied is True


def test_audio_change_keeps_sample_confirmation_valid(tmp_path):
    store, project, revision = _fixture(tmp_path)
    asyncio.run(synthesize_timeline(store, CountingTTS(), project, revision, {}))
    project = store.load(project.id)
    project.visual_variants = [VisualVariant(id="V1")]
    project.approvals = [Approval(kind="sample", input_revision_id="R1",
                                  spec={"dependencyHash": visual_dependency_hash(project)})]
    assert derive_stage_states(project, [])["visual"] == StageState.DONE

    # 只改音频（间隔/总长），画面依赖不变：样片确认必须继续有效（F4）
    project.audio_timeline.transition_gap_ms = [100]
    project.audio_timeline.sample_count += 4800
    assert derive_stage_states(project, [])["visual"] == StageState.DONE

    # 画面本身变了才失效
    project.visual_variants = [*project.visual_variants, VisualVariant(id="V2")]
    assert derive_stage_states(project, [])["visual"] == StageState.STALE


def test_revoke_marks_approvals_instead_of_deleting():
    approvals = [Approval(kind="voice", input_revision_id="R1"), Approval(kind="sample", input_revision_id="R1")]
    marked = revoke(approvals, ("voice", "sample"), "重新生成整期配音")
    assert len(marked) == 2
    assert all(a.revoked_at for a in marked)
    assert marked[0].revoked_reason == "重新生成整期配音"
    assert active(marked) == []
    # 再次撤销不会覆盖首次撤销时间
    again = revoke(marked, ("voice",), "第二次")
    assert again[0].revoked_at == marked[0].revoked_at
