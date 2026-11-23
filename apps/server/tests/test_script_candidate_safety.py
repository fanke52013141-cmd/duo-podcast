"""Rewrite candidates are accepted only against their unchanged source script."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from duocast.api.script import accept_rewrite, rewrite_script
from duocast.core.eventbus import EventBus
from duocast.domain.job import JobStatus
from duocast.domain.project import Approval, Line, ScriptRevision, Turn
from duocast.jobs.manager import JobManager
from duocast.storage.project_store import ProjectStore


def _setup(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    store.create("P")
    revision = ScriptRevision(id="R1", turns=[
        Turn(id="T1", speaker="A", lines=[Line(id="L1", display_text="原句", spoken_text="原句")]),
        Turn(id="T2", speaker="B", lines=[Line(id="L2", display_text="另一句", spoken_text="另一句")]),
    ])
    project = store.apply("P", {"script_revisions": [revision], "current_draft_revision": "R1"}, 0)
    manager = JobManager(tmp_path / "jobs", EventBus(tmp_path / "events.jsonl"), {"api": 1})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_store=store, job_manager=manager)))
    return store, manager, request, project


def _candidate(manager, request):
    accepted = asyncio.run(rewrite_script("P", {
        "revisionId": "R1", "turnIds": ["T1"], "clientToken": "rewrite-1", "mode": "rewrite",
    }, request))
    job = manager.get(accepted["jobId"])
    manager.transition(job, JobStatus.RUNNING)
    candidate_turn = {
        "id": "T1", "speaker": "A", "intent": "other", "tone": "自然", "speedRatio": 1.0,
        "lines": [{"id": "L1", "displayText": "改写句", "spokenText": "改写句"}],
    }
    manager.transition(job, JobStatus.SUCCEEDED, result={
        "candidate": {"turns": [candidate_turn], "changedTurnIds": ["T1"]},
    })
    return job


def test_accept_rewrite_uses_frozen_snapshot_and_preserves_other_turn(tmp_path):
    store, manager, request, project = _setup(tmp_path)
    job = _candidate(manager, request)
    response = asyncio.run(accept_rewrite("P", job.id, {"expectedRevision": project.revision}, request))
    assert response["currentDraftRevision"] == "R1"
    turns = response["scriptRevisions"][0]["turns"]
    assert turns[0]["lines"][0]["displayText"] == "改写句"
    assert turns[1]["lines"][0]["displayText"] == "另一句"


def test_accept_rewrite_rejects_turn_edited_after_request(tmp_path):
    store, manager, request, project = _setup(tmp_path)
    job = _candidate(manager, request)
    changed = store.load("P").script_revisions[0].model_copy(deep=True)
    changed.turns[0].lines[0].display_text = "用户后续编辑"
    current = store.apply("P", {"script_revisions": [changed]}, project.revision)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(accept_rewrite("P", job.id, {"expectedRevision": current.revision}, request))
    assert caught.value.status_code == 409
    assert store.load("P").script_revisions[0].turns[0].lines[0].display_text == "用户后续编辑"


def test_accept_rewrite_rejects_after_version_switch(tmp_path):
    store, manager, request, project = _setup(tmp_path)
    job = _candidate(manager, request)
    other = ScriptRevision(id="R2", turns=store.load("P").script_revisions[0].turns)
    current = store.apply("P", {"script_revisions": [*store.load("P").script_revisions, other],
                                "current_draft_revision": "R2"}, project.revision)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(accept_rewrite("P", job.id, {"expectedRevision": current.revision}, request))
    assert caught.value.status_code == 409


def test_accept_rewrite_creates_new_draft_when_source_was_approved(tmp_path):
    store, manager, request, project = _setup(tmp_path)
    approved = store.apply("P", {"approvals": [Approval(kind="script", input_revision_id="R1")]}, project.revision)
    job = _candidate(manager, request)
    response = asyncio.run(accept_rewrite("P", job.id, {"expectedRevision": approved.revision}, request))
    assert response["currentDraftRevision"] == "R2"
    assert [revision["id"] for revision in response["scriptRevisions"]] == ["R1", "R2"]
