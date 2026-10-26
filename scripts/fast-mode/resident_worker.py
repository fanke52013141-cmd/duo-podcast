"""Opt-in motion-only JSONL worker. Model logs never enter protocol stdout."""
import argparse
from contextlib import redirect_stdout
import hashlib
import json
from pathlib import Path
import sys
import time

PROTOCOL = "duocast-fast-resident-v1"
MAX_LINE = 65536
MAX_REQUEST_BYTES = 32 * 1024 * 1024


def parse_message(line):
    if not isinstance(line, str) or len(line.encode("utf-8")) > MAX_LINE:
        raise ValueError("Protocol line exceeds limit")
    value = json.loads(line)
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        raise ValueError("Unsupported resident protocol")
    return value


def load_request(path, expected_hash):
    source = Path(path)
    if not source.is_absolute() or source.stat().st_size > MAX_REQUEST_BYTES:
        raise ValueError("Request must be a bounded absolute server file")
    with source.open("rb") as stream:
        raw = stream.read(MAX_REQUEST_BYTES+1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("Request exceeds limit")
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError("Frozen request changed")
    request = json.loads(raw)
    if not isinstance(request, dict) or not isinstance(request.get("motionAssets"), dict) or not request["motionAssets"]:
        raise ValueError("Resident worker supports pinned local motion lip sync only")
    return request


def send(**value):
    print(json.dumps({"protocol": PROTOCOL, **value}, ensure_ascii=True), flush=True)


def serve(identity, worker=None):
    if worker is None:
        with redirect_stdout(sys.stderr):
            import worker
    if not callable(getattr(worker, "initialize_runtime", None)) or not callable(getattr(worker, "run_with_runtime", None)):
        send(type="unsupported", runtimeIdentity=identity, message="Worker does not expose resident initialization")
        return 3
    started = time.monotonic()
    with redirect_stdout(sys.stderr):
        runtime = worker.initialize_runtime()
    initialization_seconds = time.monotonic()-started
    send(type="ready", runtimeIdentity=identity, initializationSeconds=initialization_seconds)
    while True:
        line = sys.stdin.readline(MAX_LINE+2)
        if not line:
            break
        message = {}
        try:
            message = parse_message(line)
            if message.get("type") == "shutdown":
                send(type="bye", runtimeIdentity=identity)
                return 0
            request_id = message.get("requestId")
            if (message.get("type") != "render_motion" or message.get("runtimeIdentity") != identity
                    or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
                raise ValueError("Invalid resident command or runtime identity")
            request = load_request(message["requestPath"], message["requestHash"])
            started = time.monotonic()
            with redirect_stdout(sys.stderr):
                worker.run_with_runtime(request, runtime)
            send(type="done", requestId=request_id, runtimeIdentity=identity,
                 initializationSeconds=initialization_seconds, requestWallSeconds=time.monotonic()-started)
        except Exception as exc:
            send(type="error", requestId=message.get("requestId") if isinstance(locals().get("message"), dict) else None,
                 runtimeIdentity=identity, message=str(exc)[:2000])
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", required=True)
    arguments = parser.parse_args()
    raise SystemExit(serve(arguments.identity))
