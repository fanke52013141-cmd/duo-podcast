# -*- coding: utf-8 -*-
"""使用指定参考音频合成一条 WAV 语音。"""
import argparse
import copy
import json
import os
import shutil
import sys
import time
import urllib.request

ROOT = r"C:\Users\Administrator\Desktop\双人播客项目"
BASE = "http://127.0.0.1:8188"
COMFY_INPUT = r"D:\PPT_Studio_Assets\InfiniteTalk_TTS\InfiniteTalk_Runtime\ComfyUI\input"
WF = os.path.join(ROOT, "本地引擎工作流", "indextts2.5-tts_参考工作流.json")
OUT_DIR = os.path.join(ROOT, "链路实测", "_tts")

parser = argparse.ArgumentParser()
parser.add_argument("--reference", required=True)
parser.add_argument("--text", required=True)
parser.add_argument("--tag", default="tts_reference")
args = parser.parse_args()

if not os.path.isfile(args.reference):
    sys.exit(f"reference file not found: {args.reference}")
os.makedirs(OUT_DIR, exist_ok=True)
reference_name = f"duocast_ref_{args.tag}{os.path.splitext(args.reference)[1]}"
shutil.copyfile(args.reference, os.path.join(COMFY_INPUT, reference_name))

workflow = copy.deepcopy(json.load(open(WF, encoding="utf-8")))
workflow["2"]["inputs"]["text_文本"] = args.text
workflow["2"]["inputs"]["duration_factor_语速因子"] = 1.0
workflow["2"]["inputs"]["reference_audio_参考音频"] = ["4", 0]
workflow["4"]["inputs"]["audio_音频"] = reference_name
workflow["3"]["inputs"]["filename_prefix_文件名前缀"] = f"duocast_listener/{args.tag}"

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
request = urllib.request.Request(
    BASE + "/prompt",
    data=json.dumps({"prompt": workflow, "client_id": "duocast-reference-tts"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
prompt_id = json.loads(opener.open(request, timeout=60).read())["prompt_id"]
print("prompt_id:", prompt_id, flush=True)

started = time.time()
while time.time() - started < 900:
    history = json.loads(opener.open(BASE + f"/history/{prompt_id}", timeout=30).read())
    if prompt_id in history:
        result = history[prompt_id]
        if result.get("status", {}).get("status_str") == "error":
            sys.exit(json.dumps(result, ensure_ascii=True)[:4000])
        for output in result.get("outputs", {}).values():
            for audio in output.get("audio", []):
                query = f"/view?filename={audio['filename']}&type=output"
                if audio.get("subfolder"):
                    query += f"&subfolder={audio['subfolder']}"
                path = os.path.join(OUT_DIR, f"{args.tag}.wav")
                with opener.open(BASE + query, timeout=120) as response:
                    open(path, "wb").write(response.read())
                print("saved:", path, flush=True)
                sys.exit(0)
    time.sleep(3)
sys.exit("TTS timeout")
