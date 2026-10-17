"""从任务记录里统计真实引擎的墙钟耗时，用于校准 5 分钟成片的 ETA。

    python scripts/job_timings.py
    python scripts/job_timings.py --storage apps/storage --kind renders

输出每类任务的：墙钟秒数、覆盖的视频秒数、每成片秒需要多少墙钟秒（s/video-s）。
`s/video-s` 就是估算公式里的 R —— 换机器/换分辨率后重跑一次即可重新校准。

说明：
- 只统计 result.simulated is False 的真实引擎任务；mock 任务会被跳过。
- "视频秒数" = 输入快照里 projectSnapshot.audioTimeline 的时长（样片则用 range）。
- 墙钟包含排队、模型加载、重试，因此是偏保守的上界；纯推理时间会更低。
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def load_jobs(storage: Path):
    for path in glob.glob(str(storage / "jobs" / "*" / "job.json")):
        try:
            yield json.load(open(path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue


def video_seconds(job: dict) -> float:
    snap = job.get("inputSnapshot") or {}
    start, end = snap.get("rangeStartSec"), snap.get("rangeEndSec")
    if start is not None and end is not None:
        return max(0.0, float(end) - float(start))
    timeline = (snap.get("projectSnapshot") or {}).get("audioTimeline") or {}
    return float(timeline.get("sampleCount", 0)) / (timeline.get("sampleRate") or 48000)


def main() -> int:
    parser = argparse.ArgumentParser(description="统计真实引擎任务耗时")
    parser.add_argument("--storage", default="apps/storage")
    parser.add_argument("--kind", default=None, help="只看某类任务，如 renders / tts.synthesize")
    args = parser.parse_args()

    rows = []
    for job in load_jobs(Path(args.storage)):
        if job.get("status") != "succeeded" or not job.get("finishedAt") or not job.get("createdAt"):
            continue
        result = job.get("result") or {}
        if result.get("simulated") is not False:
            continue
        if args.kind and job.get("kind") != args.kind:
            continue
        snap = job.get("inputSnapshot") or {}
        meta = result.get("meta") or {}
        stats = meta.get("generationStats") or {}
        wall = (datetime.fromisoformat(job["finishedAt"])
                - datetime.fromisoformat(job["createdAt"])).total_seconds()
        span = video_seconds(job)
        rows.append({
            "kind": job.get("kind"), "wall": wall, "span": span,
            "rate": wall / span if span else None,
            "resolution": snap.get("resolution"), "generated": stats.get("generated"),
            "cached": stats.get("cached"), "id": job.get("id"),
        })

    if not rows:
        print("没有真实引擎任务（全是 mock，或还没有跑过真实任务）。")
        return 0

    rows.sort(key=lambda r: (r["kind"] or "", -r["wall"]))
    print(f"{'kind':16}{'wall(s)':>9}{'video(s)':>9}{'s/video-s':>11}  {'resolution':12}{'gen':>4}{'cache':>6}  job")
    for r in rows:
        rate = f"{r['rate']:.1f}" if r["rate"] else "-"
        print(f"{r['kind'] or '?':16}{r['wall']:>9.1f}{r['span']:>9.2f}{rate:>11}  "
              f"{str(r['resolution']):12}{str(r['generated']):>4}{str(r['cached']):>6}  {r['id']}")

    # 只对「真的生成了片段」的任务求 R 的区间，缓存命中率高的任务不计入
    generating = [r for r in rows if (r["generated"] or 0) > 0 and r["rate"]]
    if generating:
        rates = sorted(r["rate"] for r in generating)
        print(f"\n真实生成任务的 R（s/video-s）：min={rates[0]:.0f}  "
              f"median={rates[len(rates)//2]:.0f}  max={rates[-1]:.0f}  n={len(rates)}")
        print("估算示例：300 秒成片 ≈ 300 × R 秒（再叠加 TTS、图片、后处理）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
