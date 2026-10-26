from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from duocast.api.motion_diagnostics import build_motion_diagnostics, router
from duocast.domain.job import Job, JobStatus


def setup(jobs, entries=None):
    manager = SimpleNamespace(list=lambda: jobs)
    artifacts = SimpleNamespace(by_id=lambda artifact_id: (entries or {}).get(artifact_id))
    return manager, artifacts


def job(identifier="J1", **kwargs):
    return Job(id=identifier, kind="renders", project_id="P1", status=JobStatus.SUCCEEDED,
               input_snapshot={"projectSnapshot": {"motionPack": {"id": "MP", "version": "v1"}, "shotStyle": "mixed"}}, **kwargs)


def test_measured_metrics_are_allowlisted_and_simulated_is_not_real():
    stats = {"motionSource": "pinned-reviewed-motion-pack", "audioFeatureCacheHits": 2, "audioFeatureCacheMisses": 0,
             "audioFeatureCacheInvalid": 1, "audioFeaturePasses": 1, "wallSeconds": 8.5,
             "stageTimings": {"audioFeaturesSec": .5, "cameraRenderSec": 6, "privatePath": "secret"},
             "cameras": {"wide": {"nativeSize": [1280, 720]}}, "apiKey": "SECRET"}
    real = job(result={"artifactIds": ["VID"], "meta": {"generationStats": stats, "fps": 25, "durationSec": 4, "path": "C:/private"}},
               started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:10Z")
    simulated = job("J2", result={"simulated": True, "meta": {"generationStats": stats}})
    manager, artifacts = setup([real, simulated], {"VID": {"fileHash": "a"*64, "path": "C:/secret", "workflowVersion": "worker-v1", "paramsSnapshot": {"apiKey": "secret"}}})
    report = build_motion_diagnostics(manager, artifacts)
    assert report["counts"] == {"real": {"succeeded": 1}, "simulated": {"succeeded": 1}, "unverified": {}}
    cache = report["realMetricsWindow"]["cache"]
    assert cache["audioFeatureCacheHits"] == {"value": 2, "measuredJobs": 1}
    assert cache["audioFeatureCacheMisses"]["value"] == 0
    assert report["jobs"][0]["executionElapsedSeconds"] == 10
    assert report["jobs"][0]["cameraNative"] == [{"cameraId": "wide", "width": 1280, "height": 720}]
    assert report["realMetricsWindow"]["stageTimings"]["cameraRenderSec"]["meanSeconds"] == 6
    serialized = str(report)
    assert "SECRET" not in serialized and "C:/" not in serialized and "privatePath" not in serialized


def test_missing_metrics_stay_unknown_and_do_not_invent_real_completion():
    manager, artifacts = setup([job(result=None), job("J2", result={"meta": {"generationStats": {"engine": "MuseTalk"}}}),
                               Job(id="J3", kind="tts.synthesize")])
    report = build_motion_diagnostics(manager, artifacts)
    assert report["totalRelevantJobs"] == 2
    assert report["counts"]["real"] == {}
    assert report["counts"]["unverified"] == {"succeeded": 2}
    assert report["realMetricsWindow"]["cache"]["audioFeatureCacheHits"] == {"value": None, "measuredJobs": 0}
    row = report["jobs"][0]
    assert row["engineWallSeconds"] is None and row["executionElapsedSeconds"] is None
    assert row["cost"]["amount"] is None
    assert all(value is None for value in row["stageTimingsSeconds"].values())


def test_invalid_counters_times_and_nested_simulated_records_remain_separate():
    stats = {"motionSource": "pinned-reviewed-motion-pack", "audioFeatureCacheHits": True,
             "audioFeatureCacheMisses": -1, "wallSeconds": float("nan"), "stageTimings": {"audioFeaturesSec": float("inf")}}
    manager, artifacts = setup([job(result={"meta": {"generationStats": stats}}),
        Job(id="PREP", kind="motion.prepare", owner_type="motionPack", owner_id="MP", status=JobStatus.RUNNING,
            provider_task_id="external-id", result={"candidate": {"provenance": {"simulated": True}}})])
    report = build_motion_diagnostics(manager, artifacts)
    assert report["jobs"][0]["cache"]["audioFeatureCacheHits"] is None
    assert report["jobs"][0]["cache"]["audioFeatureCacheMisses"] is None
    assert report["jobs"][0]["engineWallSeconds"] is None
    assert report["jobs"][1]["classification"] == "simulated"


def test_prepare_provenance_is_recorded_without_paths_and_window_does_not_change_global_count():
    prep = Job(id="PREP", kind="motion.prepare", owner_type="motionPack", owner_id="MP", provider_task_id="external",
               input_snapshot={"packSnapshot": {"id": "MP", "version": "v1", "clips": [{"role": "A", "media": {"artifactId": "source", "fileHash": "b"*64}, "nativeWidth": 100, "nativeHeight": 80, "frameCount": 25, "fps": 25}]}})
    manager, artifacts = setup([prep, job()])
    report = build_motion_diagnostics(manager, artifacts, limit=1)
    assert report["totalRelevantJobs"] == 2 and report["returnedJobs"] == 1 and report["truncated"] is True
    assert report["jobs"][0]["classification"] == "real"
    assert report["jobs"][0]["inputs"][0]["native"]["width"] == 100
    assert report["counts"]["unverified"]["succeeded"] == 1


def test_http_is_bounded_and_has_no_path_inputs():
    manager, artifacts = setup([])
    app = FastAPI()
    app.state.job_manager, app.state.artifacts = manager, artifacts
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/api/diagnostics/motion-workflow").status_code == 200
        assert client.get("/api/diagnostics/motion-workflow?limit=201").status_code == 422
        assert client.get("/api/diagnostics/motion-workflow?limit=0").status_code == 422
        # Unknown query fields never cause a path to be read.
        assert client.get("/api/diagnostics/motion-workflow?path=C:/private").json()["jobs"] == []
