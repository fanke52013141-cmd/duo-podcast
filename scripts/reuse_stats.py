"""整期配音的单元复用率统计（F1 观测）。

真实引擎下跑一段时间后，用它看「改一句到底重算了多少」：

    python scripts/reuse_stats.py
    python scripts/reuse_stats.py --storage apps/storage --project EP9926

数据来源：jobs/<id>/job.json 里 tts.synthesize 的 result.meta（units / reused）。
mock 模式不产音频资产，reused 恒为 0，属预期。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def iter_jobs(storage: Path):
    jobs_root = storage / "jobs"
    if not jobs_root.is_dir():
        return
    for job_dir in jobs_root.iterdir():
        manifest = job_dir / "job.json"
        if not manifest.is_file():
            continue
        try:
            yield json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue


def main() -> int:
    parser = argparse.ArgumentParser(description="统计整期配音的单元复用率")
    parser.add_argument("--storage", default="apps/storage", help="运行时存储根目录")
    parser.add_argument("--project", default=None, help="只看某个工程")
    args = parser.parse_args()

    rows: list[tuple[str, str, int, int]] = []
    for job in iter_jobs(Path(args.storage)):
        if job.get("kind") != "tts.synthesize" or job.get("status") != "succeeded":
            continue
        if args.project and job.get("projectId") != args.project:
            continue
        meta = (job.get("result") or {}).get("meta") or {}
        units = meta.get("units")
        reused = meta.get("reused")
        if not isinstance(units, int) or not isinstance(reused, int):
            continue
        rows.append((job.get("projectId", "?"), job.get("id", "?"), units, reused))

    if not rows:
        print("没有找到带 units/reused 的整期配音任务（mock 模式或旧版本任务）。")
        return 0

    print(f"{'project':14} {'job':14} {'units':>6} {'reused':>7} {'reuse%':>7}")
    total_units = total_reused = 0
    for project_id, job_id, units, reused in rows:
        total_units += units
        total_reused += reused
        pct = (reused / units * 100) if units else 0
        print(f"{project_id:14} {job_id:14} {units:>6} {reused:>7} {pct:>6.0f}%")
    pct = (total_reused / total_units * 100) if total_units else 0
    print(f"\n合计：{total_reused}/{total_units} 单元复用（{pct:.0f}%），共 {len(rows)} 次整期合成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
