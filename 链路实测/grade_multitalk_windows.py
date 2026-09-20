# -*- coding: utf-8 -*-
"""以第一生成窗口为色彩基准，固定校正后续窗口，消除窗口边界色彩闪变。"""
import argparse
import os
import subprocess
import tempfile

import numpy as np
from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument("--video", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--boundary", type=float, required=True)
parser.add_argument("--reference-time", type=float, default=1.2)
parser.add_argument("--sample-offset", type=float, default=0.55)
args = parser.parse_args()

with tempfile.TemporaryDirectory() as work:
    reference = os.path.join(work, "reference.png")
    target = os.path.join(work, "target.png")
    for timestamp, path in (
        (args.reference_time, reference),
        (args.boundary + args.sample_offset, target),
    ):
        subprocess.run([
            "ffmpeg", "-y", "-v", "error", "-ss", str(timestamp),
            "-i", args.video, "-frames:v", "1", path,
        ], check=True)

    def stats(path):
        image = np.asarray(Image.open(path).convert("RGB")).astype(np.float32)
        pixels = image.reshape(-1, 3)
        return pixels.mean(axis=0), pixels.std(axis=0)

    reference_mean, reference_std = stats(reference)
    target_mean, target_std = stats(target)
    scale = reference_std / np.maximum(target_std, 1e-6)
    offset = reference_mean - scale * target_mean
    print("reference mean/std:", reference_mean.round(2), reference_std.round(2))
    print("target mean/std:", target_mean.round(2), target_std.round(2))
    print("window correction scale/offset:", scale.round(5), offset.round(3))

    channels = "rgb"
    corrections = ":".join(
        f"{channel}='clip(val*{multiplier:.8f}+{addition:.8f},0,255)'"
        for channel, multiplier, addition in zip(channels, scale, offset)
    )
    filters = (
        f"[0:v]trim=duration={args.boundary},setpts=PTS-STARTPTS[first];"
        f"[0:v]trim=start={args.boundary},setpts=PTS-STARTPTS,lutrgb={corrections}[second];"
        "[first][second]concat=n=2:v=1:a=0[video]"
    )
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", args.video,
        "-filter_complex", filters, "-map", "[video]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "19", "-preset", "medium",
        "-c:a", "aac", "-b:a", "160k", "-shortest", args.output,
    ], check=True)
