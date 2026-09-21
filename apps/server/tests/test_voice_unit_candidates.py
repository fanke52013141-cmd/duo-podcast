"""单话轮配音候选：局部生成不覆盖采用版本，采用后才重建母轨时间线。"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from duocast.api.voice import synthesize
from duocast.domain.project import Approval, Line, ScriptRevision, Turn
from duocast.jobs.manager import JobManager
from duocast.core.eventbus import EventBus
from duocast.services.voice_svc import adopt_unit_candidate, synthesize_timeline
from duocast.storage.project_store import ProjectStore


class AssetTTS:
    def __init__(self):
        self.calls = []
        self.index = 0

    async def synthesize(self, request):
        self.calls.append(request)
        self.index += 1
        return {"sampleRate": 48000, "sampleCount": 48000 + self.index,
                "audioAssetId": f"AUD-{self.index}"}


def fixture(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    project = store.create("voice-candidates")
    revision = ScriptRevision(id="R1", turns=[
        Turn(id="T-A", speaker="A", lines=[Line(id="L-A", spoken_text="第一句")]),
        Turn(id="T-B", speaker="B", lines=[Line(id="L-B", spoken_text="第二句")]),
    ])
    return store, store.apply(project.id, {"script_revisions": [revision], "current_draft_revision": "R1"}, 0), revision


def test_partial_synthesis_only_calls_target_and_preserves_adopted_audio(tmp_path):
    store, project, revision = fixture(tmp_path)
    tts = AssetTTS()
    initial = asyncio.run(synthesize_timeline(store, tts, project, revision, {}))
    before = {unit.id: unit.adopted_audio_asset_id for unit in initial.units}
    current = store.load(project.id)

    candidate = asyncio.run(synthesize_timeline(
        store, tts, current, revision, {}, target_turn_ids=["T-A"]))

    assert [call["speaker"] for call in tts.calls] == ["A", "B", "A"]
    assert store.load(project.id).audio_timeline.units[0].adopted_audio_asset_id == before["U-T-A"]
    assert store.load(project.id).audio_timeline.units[1].adopted_audio_asset_id == before["U-T-B"]
    generated = candidate.units[0]
    assert generated.candidate_audio_asset_ids[-1] == "AUD-3"
    assert generated.adopted_audio_asset_id == before["U-T-A"]


def test_adopting_candidate_rebuilds_timeline_and_clears_voice_confirmations(tmp_path):
    store, project, revision = fixture(tmp_path)
    tts = AssetTTS()
    asyncio.run(synthesize_timeline(store, tts, project, revision, {}))
    current = store.load(project.id)
    store.apply(current.id, {"approvals": [
        Approval(kind="script", input_revision_id="R1"),
        Approval(kind="voice", input_revision_id="R1"),
        Approval(kind="sample", input_revision_id="R1"),
    ]}, current.revision)
    asyncio.run(synthesize_timeline(store, tts, store.load(project.id), revision, {}, target_turn_ids=["T-A"]))

    adopted = adopt_unit_candidate(store, project.id, "U-T-A", "AUD-3")

    assert adopted.units[0].adopted_audio_asset_id == "AUD-3"
    assert adopted.units[1].adopted_audio_asset_id == "AUD-2"
    assert adopted.sample_count == 48003 + 320 * 48 + 48002
    assert [approval.kind for approval in store.load(project.id).approvals] == ["script"]


def test_api_rejects_empty_duplicate_or_unknown_partial_turn_ids(tmp_path):
    store, project, _ = fixture(tmp_path)
    manager = JobManager(tmp_path / "jobs", EventBus(tmp_path / "events.jsonl"), {"gpu": 1})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_store=store, job_manager=manager)))
    for turn_ids in ([], ["T-A", "T-A"], ["unknown"], "T-A"):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(synthesize(project.id, {"turnIds": turn_ids}, request))
        assert caught.value.status_code == 422
