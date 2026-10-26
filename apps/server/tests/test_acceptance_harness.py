"""Procedural real-media evidence, without private voices, GPU models or network."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "scripts/acceptance"
spec = importlib.util.spec_from_file_location("motion_acceptance_harness", SOURCE / "run_motion_acceptance.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real FFmpeg/ffprobe required for media evidence")
    root = tmp_path_factory.mktemp("acceptance-media")
    result = {}
    for name, color, frequency in [("blue", "blue", 440), ("red", "red", 440), ("different_audio", "blue", 880), ("silent", "blue", None)]:
        path = root / (name + ".mp4")
        command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=160x96:r=25:d=1"]
        if frequency:
            command += ["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration=1"]
        command += ["-t", "1", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        command += ["-c:a", "aac", "-b:a", "96k"] if frequency else ["-an"]
        subprocess.run(command + ["-movflags", "+faststart", str(path)], check=True)
        result[name] = path
    return result


def test_real_media_complete_decode_and_counted_frames(media):
    result = harness.inspect_media(media["blue"], duration=1, fps=25, width=160, height=96)
    assert result["passed"], result
    assert result["video"]["decodedFrames"] == 25
    assert result["video"]["duration"] == 1
    assert result["input"]["sha256"] == harness.sha256_file(media["blue"])
    assert "complete_decode" in result["proves"]
    assert all(e["exitCode"] == 0 and not e["timedOut"] for e in result["executions"])
    assert any("not approval" in limitation for limitation in result["limitations"])


def test_wrong_duration_frame_rate_and_size_are_rejected(media):
    result = harness.inspect_media(media["blue"], duration=2, fps=30, width=320, height=192)
    assert not result["passed"]
    assert {"duration_mismatch", "frame_count_mismatch", "fps_mismatch", "width_mismatch", "height_mismatch"} <= set(result["failures"])


def test_silent_reference_requires_explicit_permission(media):
    assert "missing_audio" in harness.inspect_media(media["silent"])["failures"]
    assert harness.inspect_media(media["silent"], require_audio=False)["passed"]


def test_truncated_real_media_never_passes(media, tmp_path):
    path = tmp_path / "truncated.mp4"
    original = media["blue"].read_bytes()
    path.write_bytes(original[:len(original) // 2])
    result = harness.inspect_media(path, duration=1, fps=25)
    assert not result["passed"]
    assert result["failures"]


def test_same_decoded_audio_is_independent_of_picture(media):
    result = harness.compare_audio(media["blue"], media["red"])
    assert result["passed"], result
    assert result["decodedAudioSha256"][0] == result["decodedAudioSha256"][1]
    assert result["proves"] == ["paired_decoded_audio_equal"]


def test_different_audio_is_rejected(media):
    result = harness.compare_audio(media["blue"], media["different_audio"])
    assert not result["passed"]
    assert result["failure"] == "decoded_audio_mismatch"


def test_audio_comparison_rejects_invalid_input(media):
    result = harness.compare_audio(media["blue"], media["silent"])
    assert not result["passed"]
    assert result["failure"] == "input_media_invalid"


def test_requirements_cover_whole_scope_and_remain_unproven():
    ledger = harness.manifest([])
    requirements = ledger["requirements"]
    assert {r["id"] for r in requirements} == ({f"T{i:02}" for i in range(1, 13)} | {f"V{i:02}" for i in range(1, 19)})
    assert len(requirements) == 30
    assert all(r["status"] in {"pending", "not_run"} and r["evidenceSlots"] and not r["evidence"] for r in requirements)
    assert not ledger["passed"]


def test_attached_green_media_report_does_not_complete_tasks(tmp_path):
    path = tmp_path / "scoped-report.json"
    path.write_text(json.dumps({"schemaVersion": 1, "kind": "media", "result": {"passed": True, "proves": ["complete_decode"]}}))
    ledger = harness.manifest([path])
    assert ledger["observations"][0]["passed"]
    assert ledger["observations"][0]["sha256"] == harness.sha256_file(path)
    assert not ledger["passed"]
    assert all(r["status"] != "passed" for r in ledger["requirements"])
    path.write_text('{"passed": true}')
    with pytest.raises(ValueError, match="harness report"):
        harness.manifest([path])


def test_benchmark_labels_phases_and_fails_on_nonzero_exit(tmp_path):
    data = tmp_path / "input.json"
    data.write_text('{}')
    result = harness.benchmark([sys.executable, "-c", "pass"], cold_runs=1, warm_runs=2,
                               reset_command=[sys.executable, "-c", "pass"], timeout=5, declared_inputs=[data])
    assert result["passed"]
    assert [r["phase"] for r in result["runs"]] == ["cold_reset", "cold", "warm", "warm"]
    assert result["summary"]["warm"]["runs"] == 2
    assert result["declaredInputs"][0]["sha256"] == harness.sha256_file(data)
    failed = harness.benchmark([sys.executable, "-c", "raise SystemExit(7)"], cold_runs=0, warm_runs=1, reset_command=None, timeout=5)
    assert not failed["passed"]
    assert failed["runs"][0]["exitCode"] == 7


def test_cold_reset_is_required_and_its_failure_stops_work():
    with pytest.raises(ValueError, match="reset command"):
        harness.benchmark([sys.executable, "-c", "pass"], cold_runs=1, warm_runs=0, reset_command=None, timeout=5)
    result = harness.benchmark([sys.executable, "-c", "pass"], cold_runs=1, warm_runs=1,
                               reset_command=[sys.executable, "-c", "raise SystemExit(9)"], timeout=5)
    assert not result["passed"]
    assert result["failure"] == "cold_reset_failed"
    assert len(result["runs"]) == 1


def test_timeout_is_recorded_as_failure():
    result = harness.benchmark([sys.executable, "-c", "import time;time.sleep(3)"], cold_runs=0, warm_runs=1, reset_command=None, timeout=.05)
    assert not result["passed"]
    assert result["runs"][0]["timedOut"]


def test_timeout_does_not_leave_a_spawned_child_running(tmp_path):
    marker = tmp_path / "leaked-child.txt"
    child_code = f"import time;from pathlib import Path;time.sleep(1);Path({str(marker)!r}).write_text('leaked')"
    parent_code = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child_code!r}]);time.sleep(3)"
    result = harness.benchmark([sys.executable, "-c", parent_code], cold_runs=0, warm_runs=1,
                               reset_command=None, timeout=.15)
    assert not result["passed"]
    time.sleep(1.1)
    assert not marker.exists()


def test_nonfinite_validation_tolerance_cannot_bypass_expected_spec(media):
    with pytest.raises(ValueError, match="finite positive"):
        harness.inspect_media(media["blue"], duration=100, tolerance=float("nan"))
    with pytest.raises(ValueError, match="finite positive"):
        harness.inspect_media(media["blue"], duration=float("inf"))


def test_known_credentials_and_inline_source_are_not_recorded():
    result = harness.command_record(["python", "-c", "print('secret')", "--api-key", "abc", "--token=xyz", "https://host/?key=secret"])
    assert "secret" not in json.dumps(result)
    assert "abc" not in json.dumps(result)
    assert "xyz" not in json.dumps(result)
    assert "inline-code sha256" in result[2]


def test_cli_writes_traceable_report_without_overwriting_inputs(media, tmp_path):
    output = tmp_path / "report.json"
    assert harness.main(["--output", str(output), "media", str(media["blue"]), "--duration", "1", "--fps", "25"]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["result"]["passed"]
    assert report["environment"]["gitRevision"]
    assert report["environment"]["harnessSha256"]
    assert report["environment"]["tools"]["ffmpeg"]["version"]
    before = output.read_bytes()
    with pytest.raises(SystemExit):
        harness.main(["--output", str(output), "media", str(media["blue"])])
    assert output.read_bytes() == before
    with pytest.raises(SystemExit):
        harness.main(["--output", str(media["blue"]), "media", str(media["blue"])])


def test_cli_failure_has_nonzero_exit_and_evidence(media, tmp_path):
    output = tmp_path / "failed.json"
    assert harness.main(["--output", str(output), "media", str(media["blue"]), "--duration", "9"]) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert "duration_mismatch" in report["result"]["failures"]
