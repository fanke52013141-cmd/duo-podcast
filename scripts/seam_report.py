"""量化成片镜头切换处的接缝：逐帧亮度/色度统计，找出相邻帧突变点。

用来回答「正反打/过肩的衔接到底有多明显」，不需要重新渲染：

    python scripts/seam_report.py apps/storage/artifacts/video/<...>/final.mp4
    python scripts/seam_report.py xxx.mp4 --luma 6 --chroma 5

判定经验（0–255 量程）：
- 亮度差 < 5：基本无感；
- 亮度差 6–15 / 色度差 6–15：看得出来的跳变，但不算严重；
- 亮度差 > 15 或色度差 > 20：明显的闪/偏色，需要做切点色彩匹配。

依赖：ffmpeg / ffprobe（PATH 里，或用 --ffmpeg 指定目录）。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

METADATA_RE = re.compile(r"lavfi\.signalstats\.(YAVG|UAVG|VAVG)=([\d.]+)")
FRAME_RE = re.compile(r"frame:\s*(\d+)\s+pts:\s*\d+\s+pts_time:\s*([\d.]+)")


def find_ffmpeg(explicit: str | None) -> tuple[str, str]:
    if explicit:
        base = Path(explicit)
        return str(base / "ffmpeg.exe"), str(base / "ffprobe.exe")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("找不到 ffmpeg/ffprobe，请装到 PATH 或用 --ffmpeg 指定 bin 目录")
    return ffmpeg, ffprobe


def probe(ffprobe: str, video: Path) -> str:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-show_entries", "format=duration", "-of", "default=nw=1", str(video)],
        capture_output=True)
    return (out.stdout or b"").decode("utf-8", "replace").replace("\n", " ").strip()


def frame_stats(ffmpeg: str, video: Path) -> list[dict]:
    proc = subprocess.run(
        [ffmpeg, "-v", "info", "-i", str(video), "-an",
         "-vf", "signalstats,metadata=print:file=-", "-f", "null", "-"],
        capture_output=True)
    text = (proc.stderr or b"").decode("utf-8", "replace") + (proc.stdout or b"").decode("utf-8", "replace")
    frames: list[dict] = []
    for line in text.splitlines():
        m = FRAME_RE.match(line.strip())
        if m:
            frames.append({"frame": int(m.group(1)), "t": float(m.group(2))})
            continue
        m = re.search(r"lavfi\.signalstats\.(YAVG|UAVG|VAVG)=([\d.]+)", line)
        if m and frames:
            frames[-1][m.group(1)] = float(m.group(2))
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description="测量成片切点接缝")
    parser.add_argument("video")
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg 所在目录")
    parser.add_argument("--luma", type=float, default=6.0, help="亮度差阈值（默认 6）")
    parser.add_argument("--chroma", type=float, default=5.0, help="色度差阈值（默认 5）")
    args = parser.parse_args()

    video = Path(args.video)
    if not video.is_file():
        raise SystemExit(f"文件不存在: {video}")
    ffmpeg, ffprobe = find_ffmpeg(args.ffmpeg)

    print(f"{video.name}  {probe(ffprobe, video)}")
    frames = frame_stats(ffmpeg, video)
    print(f"已统计 {len(frames)} 帧")
    if not frames:
        raise SystemExit("没有解析到帧统计（确认 ffmpeg 带 signalstats 滤镜）")

    jumps = []
    for prev, cur in zip(frames, frames[1:]):
        if not all(k in prev and k in cur for k in ("YAVG", "UAVG", "VAVG")):
            continue
        d_luma = abs(cur["YAVG"] - prev["YAVG"])
        d_chroma = ((cur["UAVG"] - prev["UAVG"]) ** 2 + (cur["VAVG"] - prev["VAVG"]) ** 2) ** 0.5
        if d_luma >= args.luma or d_chroma >= args.chroma:
            jumps.append((cur["t"], d_luma, d_chroma))

    print(f"\n发现 {len(jumps)} 处跳变（亮度差 ≥{args.luma} 或色度差 ≥{args.chroma}）：")
    for t, d_luma, d_chroma in jumps:
        print(f"  t={t:6.2f}s  亮度差={d_luma:5.1f}  色度差={d_chroma:5.1f}")
    if jumps:
        lumas = sorted(j[1] for j in jumps)
        chromas = sorted(j[2] for j in jumps)
        print(f"\n亮度差：中位 {lumas[len(lumas)//2]:.1f}  最大 {lumas[-1]:.1f}")
        print(f"色度差：中位 {chromas[len(chromas)//2]:.1f}  最大 {chromas[-1]:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
