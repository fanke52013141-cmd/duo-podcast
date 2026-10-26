"""Inspect motion evidence without loading GPU models; visual review stays required."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/server"))
from duocast.services.motion_quality import inspect_motion_clip


def region(value):
    try:
        x, y, width, height = map(float, value.split(","))
        return {"x": x, "y": y, "width": width, "height": height}
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("Expected x,y,width,height in normalized coordinates")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--body-region", type=region, action="append", default=[])
    parser.add_argument("--exclude-region", type=region, action="append", default=[])
    parser.add_argument("--background-region", type=region)
    parser.add_argument("--expected-file-hash")
    parser.add_argument("--expected-frames", type=int)
    parser.add_argument("--expected-fps", type=float)
    parser.add_argument("--usable-start-frame", type=int, default=0)
    parser.add_argument("--usable-end-frame", type=int)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.out and args.out.resolve() == args.video.resolve():
        parser.error("Report output must not overwrite the source video")
    report = inspect_motion_clip(args.video, body_regions=args.body_region,
        exclusion_regions=args.exclude_region, background_region=args.background_region,
        expected_file_hash=args.expected_file_hash, expected_frames=args.expected_frames,
        expected_fps=args.expected_fps, usable_start_frame=args.usable_start_frame,
        usable_end_frame=args.usable_end_frame)
    content = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".motion-check-", suffix=".tmp", dir=args.out.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
            os.replace(name, args.out)
        finally:
            Path(name).unlink(missing_ok=True)
    print(content)
    return 2 if report["hardFailures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
