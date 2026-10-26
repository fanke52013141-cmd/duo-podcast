"""Read-only media evidence and explicit-command benchmarks; never a visual approval."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 1


def sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return sha256_stream(source)


def sha256_stream(source) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def input_record(path: Path) -> dict:
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Not a media file: {path}")
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def command_record(argv: list[str]) -> list[str]:
    """Do not persist credential values or arbitrary inline program source."""
    result = []
    redact_next = False
    for index, argument in enumerate(argv):
        lowered = argument.lower()
        if redact_next:
            result.append("<redacted>")
            redact_next = False
        elif index and argv[index - 1] in {"-c", "-Command", "--eval", "-e"}:
            result.append("<inline-code sha256:" + hashlib.sha256(argument.encode()).hexdigest() + ">")
        elif any(word in lowered for word in ("api-key=", "api_key=", "token=", "password=", "secret=")):
            result.append(argument.split("=", 1)[0] + "=<redacted>")
        elif lowered in {"--api-key", "--api_key", "--token", "--password", "--secret", "-h", "--header"}:
            result.append(argument)
            redact_next = True
        elif "://" in argument:
            result.append("<url sha256:" + hashlib.sha256(argument.encode()).hexdigest() + ">")
        else:
            result.append(argument)
    return result


def execute(argv: list[str], timeout: float, capture_json: bool = False) -> tuple[dict, bytes]:
    started = time.monotonic()
    timed_out = False
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        grouping = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False, **grouping)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # A venv launcher can own a second Python process; terminate only this command's tree.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            code = process.wait()
        stdout.seek(0)
        out_hash = sha256_stream(stdout)
        stderr.seek(0)
        err_hash = sha256_stream(stderr)
        stdout.seek(0)
        payload = stdout.read(4 * 1024 * 1024 + 1) if capture_json else b""
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("Probe JSON exceeded 4 MiB evidence limit")
    return {"command": command_record(argv), "exitCode": code, "timedOut": timed_out,
            "wallSeconds": time.monotonic() - started, "stdoutSha256": out_hash,
            "stderrSha256": err_hash}, payload


def environment() -> dict:
    git = shutil.which("git")
    revision = subprocess.run([git, "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True) if git else None
    dirty = subprocess.run([git, "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True) if git else None
    tools = {}
    for name in ("ffmpeg", "ffprobe"):
        executable = shutil.which(name)
        if executable:
            version = subprocess.run([executable, "-version"], capture_output=True, text=True, timeout=10)
            tools[name] = {"path": executable, "version": version.stdout.splitlines()[0] if version.stdout else "unknown"}
        else:
            tools[name] = {"available": False}
    return {"gitRevision": revision.stdout.strip() if revision and revision.returncode == 0 else None,
            "gitDirty": bool(dirty.stdout.strip()) if dirty and dirty.returncode == 0 else None,
            "harnessSha256": sha256_file(Path(__file__)),
            "pythonVersion": sys.version.split()[0], "tools": tools}


def inspect_media(path: Path, *, duration: float | None = None, fps: float | None = None,
                  width: int | None = None, height: int | None = None,
                  require_audio: bool = True, tolerance: float = .05, timeout: float = 120) -> dict:
    for value in (duration, fps, width, height, tolerance, timeout):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("Expected media specifications and time limits must be finite positive numbers")
    record = input_record(path)
    expected = {"duration": duration, "fps": fps, "width": width, "height": height,
                "requireAudio": require_audio, "toleranceSeconds": tolerance}
    result = {"input": record, "expected": expected, "passed": False, "failures": [],
              "executions": [], "proves": [], "limitations": [
                  "Does not establish motion, visible eyes, identity, lip synchronization or naturalness.",
                  "A successful decode/spec check is not approval of a motion template."]}
    probe, raw = execute(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
                          "-of", "json", record["path"]], timeout, capture_json=True)
    result["executions"].append(probe)
    if probe["exitCode"] or probe["timedOut"]:
        result["failures"].append("probe_failed")
        return result
    try:
        info = json.loads(raw)
    except (ValueError, TypeError):
        result["failures"].append("probe_invalid_json")
        return result
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not video:
        result["failures"].append("missing_video")
        return result
    try:
        actual_fps = float(Fraction(video["avg_frame_rate"]))
        count = int(video["nb_read_frames"])
        actual_duration = float(video.get("duration", info.get("format", {}).get("duration", 0)))
        if not math.isfinite(actual_fps) or not math.isfinite(actual_duration) or actual_fps <= 0 or count <= 0 or actual_duration <= 0:
            raise ValueError("Empty or invalid video")
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        result["failures"].append("invalid_video_timing")
        return result
    result["video"] = {"width": video["width"], "height": video["height"], "fps": actual_fps,
                       "decodedFrames": count, "duration": actual_duration,
                       "durationSource": "stream" if "duration" in video else "container"}
    if duration is not None and abs(actual_duration - duration) > tolerance:
        result["failures"].append("duration_mismatch")
    if fps is not None and abs(actual_fps - fps) > .001:
        result["failures"].append("fps_mismatch")
    if duration is not None and fps is not None and count != round(duration * fps):
        result["failures"].append("frame_count_mismatch")
    if width is not None and video["width"] != width:
        result["failures"].append("width_mismatch")
    if height is not None and video["height"] != height:
        result["failures"].append("height_mismatch")
    if require_audio and not audio:
        result["failures"].append("missing_audio")
    if audio:
        audio_duration = audio.get("duration")
        result["audio"] = {"codec": audio.get("codec_name"), "duration": float(audio_duration) if audio_duration else None}
        if audio_duration and (not math.isfinite(float(audio_duration)) or float(audio_duration) <= 0):
            result["failures"].append("invalid_audio_timing")
        elif audio_duration and abs(float(audio_duration) - actual_duration) > tolerance:
            result["failures"].append("audio_video_duration_mismatch")
    decode, _ = execute(["ffmpeg", "-v", "error", "-xerror", "-err_detect", "explode", "-nostdin",
                         "-i", record["path"], "-f", "null", "-"], timeout)
    result["executions"].append(decode)
    if decode["exitCode"] or decode["timedOut"]:
        result["failures"].append("complete_decode_failed")
    else:
        result["proves"].append("complete_decode")
    if sha256_file(Path(record["path"])) != record["sha256"]:
        result["failures"].append("input_changed_during_validation")
    result["passed"] = not result["failures"]
    if result["passed"]:
        result["proves"].append("requested_media_spec")
    return result


def compare_audio(first: Path, second: Path, *, timeout: float = 120) -> dict:
    media = [inspect_media(path, require_audio=True, timeout=timeout) for path in (first, second)]
    result = {"passed": False, "media": media, "executions": [], "pcmFormat": "s16le/48000Hz/mono",
              "proves": [], "limitations": ["Equal decoded audio does not establish equal picture quality or valid role binding."]}
    hashes = []
    if not all(item["passed"] for item in media):
        result["failure"] = "input_media_invalid"
        return result
    for item in media:
        execution, _ = execute(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", item["input"]["path"],
                                "-map", "0:a:0", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"], timeout)
        result["executions"].append(execution)
        if execution["exitCode"] or execution["timedOut"]:
            result["failure"] = "audio_decode_failed"
            return result
        hashes.append(execution["stdoutSha256"])
        if sha256_file(Path(item["input"]["path"])) != item["input"]["sha256"]:
            result["failure"] = "input_changed_during_validation"
            return result
    result["decodedAudioSha256"] = hashes
    result["passed"] = hashes[0] == hashes[1]
    if result["passed"]:
        result["proves"].append("paired_decoded_audio_equal")
    else:
        result["failure"] = "decoded_audio_mismatch"
    return result


def benchmark(command: list[str], *, cold_runs: int, warm_runs: int, reset_command: list[str] | None,
              timeout: float, declared_inputs: list[Path] | None = None) -> dict:
    if not command or cold_runs < 0 or warm_runs < 0 or not 1 <= cold_runs + warm_runs <= 100:
        raise ValueError("Provide a command and 1–100 total runs")
    if cold_runs and not reset_command:
        raise ValueError("Cold runs require an explicit reset command; a label alone does not establish cold state")
    inputs = [input_record(path) for path in (declared_inputs or [])]
    script_inputs = []
    for argument in command[1:]:
        try:
            path = Path(argument)
            if path.is_file():
                script_inputs.append(input_record(path))
        except OSError:
            continue
    runs = []
    for phase, count in (("cold", cold_runs), ("warm", warm_runs)):
        for index in range(count):
            if phase == "cold":
                reset, _ = execute(reset_command, timeout)
                runs.append({"phase": "cold_reset", "index": index + 1, **reset})
                if reset["exitCode"] or reset["timedOut"]:
                    return {"passed": False, "failure": "cold_reset_failed", "runs": runs}
            execution, _ = execute(command, timeout)
            runs.append({"phase": phase, "index": index + 1, **execution})
    summaries = {}
    for phase in ("cold", "warm"):
        values = [r["wallSeconds"] for r in runs if r["phase"] == phase]
        if values:
            summaries[phase] = {"runs": len(values), "medianSeconds": statistics.median(values), "maxSeconds": max(values)}
    return {"passed": all(r["exitCode"] == 0 and not r["timedOut"] for r in runs), "runs": runs,
            "declaredInputs": inputs, "commandFiles": script_inputs,
            "summary": summaries, "proves": ["command_execution_timings"], "limitations": [
                "Operator-provided reset must actually clear relevant caches/models; this harness cannot establish that automatically.",
                "Repeated process launch timings do not establish persistent-model warm inference or visual quality."]}


def manifest(evidence_reports: list[Path]) -> dict:
    requirements = json.loads((Path(__file__).with_name("requirements.json")).read_text(encoding="utf-8"))
    observations = []
    for path in evidence_reports:
        record = input_record(path)
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("schemaVersion") != SCHEMA_VERSION or "kind" not in report or "result" not in report:
            raise ValueError("Evidence must be a harness report, not an arbitrary green file")
        observations.append({**record, "kind": report["kind"], "passed": report["result"].get("passed"),
                             "proves": report["result"].get("proves", []),
                             "limitations": report["result"].get("limitations", [])})
    return {"requirements": requirements["requirements"], "observations": observations,
            "passed": False, "notice": "Reports attach scoped observations only. Task and visual acceptance remain pending until requirement-by-requirement review; file existence never completes a requirement."}


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--output", type=Path, help="New JSON report path; defaults to ignored .review/motion-acceptance")
    cli.add_argument("--timeout", type=float, default=120)
    sub = cli.add_subparsers(dest="kind", required=True)
    media = sub.add_parser("media")
    media.add_argument("input", type=Path)
    media.add_argument("--duration", type=float)
    media.add_argument("--fps", type=float)
    media.add_argument("--width", type=int)
    media.add_argument("--height", type=int)
    media.add_argument("--allow-silent", action="store_true")
    media.add_argument("--tolerance", type=float, default=.05)
    audio = sub.add_parser("compare-audio")
    audio.add_argument("first", type=Path)
    audio.add_argument("second", type=Path)
    timing = sub.add_parser("benchmark", help="Executes only the explicit command after --; no default model/network command")
    timing.add_argument("--cold-runs", type=int, default=0)
    timing.add_argument("--warm-runs", type=int, default=5)
    timing.add_argument("--reset-command-json", help="JSON argv array, executed before each cold run")
    timing.add_argument("--input", action="append", type=Path, default=[], dest="declared_inputs",
                        help="Hash a media/request input consumed indirectly by the benchmark")
    timing.add_argument("command", nargs=argparse.REMAINDER)
    ledger = sub.add_parser("manifest")
    ledger.add_argument("--evidence-report", action="append", type=Path, default=[])
    return cli


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if not 0 < args.timeout <= 10800:
        cli.error("timeout must be between 0 and 10800 seconds")
    if args.kind == "media":
        for name in ("duration", "fps", "width", "height", "tolerance"):
            value = getattr(args, name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                cli.error(name + " must be finite and positive")
    output = args.output or ROOT / ".review/motion-acceptance" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".json")
    inputs = [getattr(args, name) for name in ("input", "first", "second") if hasattr(args, name)]
    inputs += getattr(args, "evidence_report", [])
    inputs += getattr(args, "declared_inputs", [])
    if output.exists() or output.resolve() in {p.resolve() for p in inputs}:
        cli.error("Output must be a new file and cannot replace an input")
    try:
        if args.kind == "media":
            result = inspect_media(args.input, duration=args.duration, fps=args.fps, width=args.width,
                                   height=args.height, require_audio=not args.allow_silent,
                                   tolerance=args.tolerance, timeout=args.timeout)
        elif args.kind == "compare-audio":
            result = compare_audio(args.first, args.second, timeout=args.timeout)
        elif args.kind == "benchmark":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            reset = json.loads(args.reset_command_json) if args.reset_command_json else None
            if reset is not None and (not isinstance(reset, list) or not reset or any(not isinstance(x, str) for x in reset)):
                raise ValueError("reset command must be a nonempty JSON argv array")
            result = benchmark(command, cold_runs=args.cold_runs, warm_runs=args.warm_runs, reset_command=reset,
                               timeout=args.timeout, declared_inputs=args.declared_inputs)
        else:
            result = manifest(args.evidence_report)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result = {"passed": False, "errorType": type(exc).__name__, "message": str(exc)}
    report = {"schemaVersion": SCHEMA_VERSION, "kind": args.kind,
              "createdAt": datetime.now(timezone.utc).isoformat(), "environment": environment(), "result": result}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as destination:
        json.dump(report, destination, ensure_ascii=False, indent=2)
    print(json.dumps({"report": str(output.resolve()), "passed": result.get("passed"),
                      "proves": result.get("proves", []), "failures": result.get("failures", [])}, ensure_ascii=False))
    return 0 if result.get("passed") or args.kind == "manifest" else 1


if __name__ == "__main__":
    raise SystemExit(main())
