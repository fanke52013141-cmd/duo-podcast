# -*- coding: utf-8 -*-
"""四句话真 TTS 母轨 → InfiniteTalk 数字人视频（Runtime 实例@8188，swap32）。

与 test_video_comfyui.py 的区别：直接投喂完整的 PCM16/48k 母轨（不截断、不转码），
用于 goal「看视频效果」的真实成片演示。渲染时长由音频时长决定（MathExpression a*25+1 帧）。
"""
import copy, json, os, shutil, sys, time, urllib.request, wave

BASE = "http://127.0.0.1:8188"
COMFY_IN = os.environ.get("COMFY_INPUT_DIR",
                          r"D:\PPT_Studio_Assets\InfiniteTalk_TTS\InfiniteTalk_Runtime\ComfyUI\input")
PORTRAIT = os.environ.get("PORTRAIT_NAME", "av_ea295f205c.png")
MASTER = os.environ.get("MASTER_WAV",
                        r"C:\Users\Administrator\Desktop\双人播客项目\链路实测\_master_4sent.wav")
OUT_DIR = r"C:\Users\Administrator\Desktop\双人播客项目\链路实测\_video"
WF_PATH = r"C:\Users\Administrator\Desktop\双人播客项目\本地引擎工作流\infinitetalk-数字人_api_windows兼容.json"
os.makedirs(OUT_DIR, exist_ok=True)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 1) 母轨校验 + 拷贝进 Runtime input（ComfyUI /input/upload 返回 405，只能文件拷贝）
with wave.open(str(MASTER)) as w:
    dur = w.getnframes() / w.getframerate()
    print(f"master: {w.getframerate()}Hz {w.getnchannels()}ch {w.getsampwidth()*8}bit dur={dur:.2f}s")
dst_audio = os.path.join(COMFY_IN, "duocast_4sent.wav")
shutil.copyfile(MASTER, dst_audio)
if not os.path.exists(os.path.join(COMFY_IN, PORTRAIT)):
    shutil.copyfile(os.path.join(
        r"D:\软件\Wan2.2-ReMix-SVI2-V3\Wan2.2-ReMix-SVI2-V3\ComfyUI\input", PORTRAIT),
        os.path.join(COMFY_IN, PORTRAIT))

# 2) 组装工作流
wf = copy.deepcopy(json.load(open(WF_PATH, encoding="utf-8")))
swap = os.environ.get("SWAP_BLOCKS", "32")
wf["134"]["inputs"]["blocks_to_swap"] = int(swap)
wf["134"]["inputs"]["use_non_blocking"] = os.environ.get("SWAP_NONBLOCK", "0") == "1"
tag = os.environ.get("RUN_TAG", "duocast_4sent")
wf["133"]["inputs"]["image"] = PORTRAIT
wf["218"]["inputs"]["audio"] = "duocast_4sent.wav"
wf["229"]["inputs"]["filename_prefix"] = f"duocast_linktest/{tag}"
print(f"submit: swap={swap} portrait={PORTRAIT} audio=duocast_4sent.wav tag={tag}", flush=True)

req = urllib.request.Request(BASE + "/prompt",
                             data=json.dumps({"prompt": wf, "client_id": "duocast-video-4sent"}).encode(),
                             headers={"Content-Type": "application/json"})
try:
    prompt_id = json.loads(opener.open(req, timeout=60).read())["prompt_id"]
except urllib.error.HTTPError as e:
    print("PROMPT REJECTED:", e.code); print(e.read().decode("utf-8", "replace")[:4000]); sys.exit(1)
print("prompt_id:", prompt_id, flush=True)

# 3) 轮询下载
t0 = time.time()
while time.time() - t0 < 5400:
    hist = json.loads(opener.open(BASE + f"/history/{prompt_id}", timeout=30).read())
    if prompt_id in hist:
        h = hist[prompt_id]
        if h.get("status", {}).get("status_str") == "error":
            print(f"COMFY ERROR after {time.time()-t0:.0f}s")
            print(json.dumps(h, ensure_ascii=False)[:6000]); sys.exit(1)
        outs = h.get("outputs", {})
        if outs:
            print(f"done in {time.time()-t0:.0f}s")
            for o in outs.values():
                for v in o.get("gifs", []) or o.get("videos", []) or []:
                    q = f"/view?filename={v['filename']}&type=output&subfolder={v.get('subfolder','')}"
                    fn = os.path.join(OUT_DIR, os.path.basename(v["filename"]))
                    with opener.open(BASE + q, timeout=180) as r:
                        open(fn, "wb").write(r.read())
                    print("saved:", fn, os.path.getsize(fn), "bytes", flush=True)
            sys.exit(0)
    time.sleep(15)
print("TIMEOUT after 90min"); sys.exit(2)
