"""为已生成视频创建保守的超分/插帧后处理计划。

默认只检查输入并写计划；--execute 使用 ffmpeg Lanczos 缩放，不会改动输入文件。
ComfyUI 方案仅输出节点接线说明，避免自动安装或假设 RTX/RIFE 节点已存在。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def probe(path: Path) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--width", type=int, default=0)
    parser.add_argument("--height", type=int, default=0)
    parser.add_argument("--target-fps", type=float, default=0)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--plan", type=Path, default=Path("postprocess-plan.json"))
    args = parser.parse_args()
    if not args.input.is_file():
        raise SystemExit(f"输入视频不存在: {args.input}")
    if args.execute and (args.output is None or not args.width or not args.height):
        raise SystemExit("--execute 需要 --output、--width 和 --height")
    info = probe(args.input)
    plan = {
        "input": str(args.input.resolve()), "probe": info,
        "ffmpegFallback": {
            "operation": "Lanczos scale; preserve source audio and timeline",
            "targetWidth": args.width or None, "targetHeight": args.height or None,
            "targetFps": args.target_fps or None,
        },
        "comfyUi": {
            "upscale": "LoadVideo → GetVideoComponents → RTXVideoSuperResolution(scale=2, quality=ULTRA) → CreateVideo → SaveVideo",
            "interpolation": "FrameInterpolationModelLoader(RIFE/FILM) → FrameInterpolate(multiplier=2) → CreateVideo",
            "rule": "先只验证超分；插帧仅在低 fps 生成链路中启用，并以原音频时间轴复封装。",
        },
    }
    args.plan.parent.mkdir(parents=True, exist_ok=True)
    args.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.execute:
        print(f"已写入后处理计划: {args.plan}")
        return
    vf = f"scale={args.width}:{args.height}:flags=lanczos"
    if args.target_fps:
        # fps 滤镜改变帧采样但保持 PTS/音轨时长；它不是 RIFE，RIFE 应走上方 ComfyUI 计划。
        vf += f",fps={args.target_fps}"
    t0 = time.monotonic()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(args.input), "-vf", vf,
                    "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-crf", "18",
                    "-c:a", "copy", str(args.output)], check=True)
    print(json.dumps({"output": str(args.output), "elapsedSeconds": round(time.monotonic() - t0, 3)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
