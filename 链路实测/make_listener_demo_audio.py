# -*- coding: utf-8 -*-
"""生成含静音前导的双轨测试音频；B 说话，A 为等长数字静音。"""
import argparse
import os
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--speech", required=True)
parser.add_argument("--output-dir", required=True)
parser.add_argument("--lead", type=float, default=0.9)
parser.add_argument("--tail", type=float, default=0.4)
args = parser.parse_args()
os.makedirs(args.output_dir, exist_ok=True)

b = os.path.join(args.output_dir, "listener_demo_track_b.wav")
a = os.path.join(args.output_dir, "listener_demo_track_a.wav")
master = os.path.join(args.output_dir, "listener_demo_master.wav")
subprocess.run([
    "ffmpeg", "-y", "-v", "error",
    "-f", "lavfi", "-t", str(args.lead), "-i", "anullsrc=r=48000:cl=mono",
    "-i", args.speech,
    "-f", "lavfi", "-t", str(args.tail), "-i", "anullsrc=r=48000:cl=mono",
    "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[a]",
    "-map", "[a]", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", b,
], check=True)
duration = float(subprocess.check_output([
    "ffprobe", "-v", "error", "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1", b
], text=True).strip())
subprocess.run([
    "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", str(duration),
    "-i", "anullsrc=r=48000:cl=mono", "-c:a", "pcm_s16le", a
], check=True)
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", b, "-c", "copy", master], check=True)
print(f"duration={duration:.3f}s")
for path in (a, b, master):
    print(path)
