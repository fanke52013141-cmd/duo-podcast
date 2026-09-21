"""生成 InfiniteTalk 速度实验工作流，不提交 GPU 任务。

用法：
  py render_speed_plan.py --preset baseline --output _plans/baseline.json
  py render_speed_plan.py --preset fast --attention sageattn --compile --output _plans/fast.json

产物可直接作为 ComfyUI /prompt 的 prompt 字段。此脚本刻意不自动开启 TeaCache。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


PRESETS = {
    "baseline": {"height": 480, "fps": 25, "swap": 32},
    # 1.8:1，与现有 864×480 底图构图一致，且均为 16 的倍数。
    "fast": {"height": 400, "fps": 25, "swap": 32},
    # 只用于验证低帧率是否保持口型；后处理应插帧回交付帧率。
    "fast20": {"height": 400, "fps": 20, "swap": 32},
}


def build_plan(workflow: dict, *, preset: str, attention: str, compile_model: bool,
               tea_cache: bool, force_offload: bool | None) -> tuple[dict, dict]:
    settings = dict(PRESETS[preset])
    height, fps = settings["height"], settings["fps"]
    wf = json.loads(json.dumps(workflow))
    # 下列四项共同决定「每秒实际采样帧数」，不能只调整 VideoCombine 的输出 fps。
    wf["213"]["inputs"]["scale_to_length"] = height
    wf["198"]["inputs"]["fps"] = fps
    wf["223"]["inputs"]["expression"] = f"a*{fps}+1"
    wf["229"]["inputs"]["frame_rate"] = fps
    wf["134"]["inputs"]["blocks_to_swap"] = settings["swap"]
    wf["122"]["inputs"]["attention_mode"] = attention
    if compile_model:
        wf["122"]["inputs"]["compile_args"] = ["177", 0]
    else:
        wf["122"]["inputs"].pop("compile_args", None)
    if force_offload is not None:
        for node_id in ("135", "192", "199"):
            wf[node_id]["inputs"]["force_offload"] = force_offload
    if not tea_cache:
        wf.pop("240", None)
        wf["199"]["inputs"].pop("cache_args", None)
        for value in wf["199"]["inputs"].values():
            if isinstance(value, list) and value and value[0] == "240":
                raise ValueError("TeaCache link was not removed")
    meta = {
        "preset": preset, "height": height, "fps": fps, "blocksToSwap": settings["swap"],
        "attention": attention, "torchCompile": compile_model, "teaCache": tea_cache,
        "forceOffload": force_offload,
        "acceptance": "同一底图、音频、seed、4 steps；记录耗时、显存、口型和色闪。",
    }
    return wf, meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", choices=PRESETS, default="baseline")
    parser.add_argument("--attention", choices=("sdpa", "sageattn", "sageattn_compiled"), default="sdpa")
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--tea-cache", action="store_true", help="仅限隔离实验；默认关闭")
    parser.add_argument("--force-offload", choices=("keep", "on", "off"), default="keep")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "本地引擎工作流" / "infinitetalk-数字人_api_windows兼容.json"
    workflow = json.loads(source.read_text(encoding="utf-8"))
    offload = {"keep": None, "on": True, "off": False}[args.force_offload]
    planned, meta = build_plan(workflow, preset=args.preset, attention=args.attention,
                               compile_model=args.compile, tea_cache=args.tea_cache,
                               force_offload=offload)
    # 渲染器将此键与底图/掩码/音轨哈希组合，作为可安全复用成片的内容寻址缓存键。
    meta["workflowSha256"] = hashlib.sha256(
        json.dumps(planned, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"meta": meta, "prompt": planned}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
