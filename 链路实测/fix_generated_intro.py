# -*- coding: utf-8 -*-
"""以原始底图覆盖扩散残留首帧，并在无声前导内淡出到生成画面。"""
import argparse
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--video", required=True)
parser.add_argument("--source", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--hold", type=float, default=0.55)
parser.add_argument("--fade-end", type=float, default=0.9)
args = parser.parse_args()

if args.fade_end <= args.hold:
    raise SystemExit("fade-end must be greater than hold")
alpha = f"if(lt(T,{args.hold}),1,if(lt(T,{args.fade_end}),1-(T-{args.hold})/({args.fade_end}-{args.hold}),0))"
filters = (
    "[0:v]scale=864:480:force_original_aspect_ratio=increase,crop=864:480,"
    "format=rgba[plate];[1:v]format=rgba[generated];"
    f"[plate][generated]blend=all_expr='A*({alpha})+B*(1-({alpha}))',format=yuv420p[outv]"
)
subprocess.run([
    "ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", args.source, "-i", args.video,
    "-filter_complex", filters, "-map", "[outv]", "-map", "1:a?", "-shortest",
    "-c:v", "libx264", "-crf", "19", "-preset", "medium", "-c:a", "aac", "-b:a", "160k",
    args.output,
], check=True)
