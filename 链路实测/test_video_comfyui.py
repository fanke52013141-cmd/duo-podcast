# -*- coding: utf-8 -*-
"""实测视频链路：InfiniteTalk 数字人工作流 @ 主包 ComfyUI(8188)。
输入 = 我方 IndexTTS 产出的截断音频(转 PCM16) + 主包 input 现成人像。"""
import copy, json, os, struct, sys, time, urllib.request

BASE = "http://127.0.0.1:8188"
COMFY_IN = os.environ.get("COMFY_INPUT_DIR", r"D:\软件\Wan2.2-ReMix-SVI2-V3\Wan2.2-ReMix-SVI2-V3\ComfyUI\input")
PORTRAIT = os.environ.get("PORTRAIT_NAME", "av_ea295f205c.png")
import shutil
if not os.path.exists(os.path.join(COMFY_IN, PORTRAIT)):
    shutil.copyfile(os.path.join(r"D:\软件\Wan2.2-ReMix-SVI2-V3\Wan2.2-ReMix-SVI2-V3\ComfyUI\input", PORTRAIT),
                    os.path.join(COMFY_IN, PORTRAIT))
OUT_DIR = r"C:\Users\Administrator\Desktop\双人播客项目\链路实测\_video"
os.makedirs(OUT_DIR, exist_ok=True)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
WF = json.load(open(r"C:\Users\Administrator\Desktop\双人播客项目\本地引擎工作流\infinitetalk-数字人_api_windows兼容.json", encoding="utf-8"))

# 1) TTS wav(float32,22050) 截前 5 秒转 PCM16，拷入 input
src = open(r"C:\Users\Administrator\Desktop\双人播客项目\链路实测\_tts\tts_00001.wav", "rb").read()
i = src.find(b"data"); n = min(struct.unpack("<I", src[i+4:i+8])[0], 22050 * 5 * 4)
samples = struct.unpack(f"<{n//4}f", src[i+8:i+8+n])
pcm = struct.pack(f"<{len(samples)}h", *[int(max(-1.0, min(1.0, s)) * 32767) for s in samples])
hdr_fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 22050, 22050 * 2, 2, 16)
wav16 = b"RIFF" + struct.pack("<I", 36 + len(hdr_fmt) + 8 + len(pcm) - 8) + b"WAVE" + hdr_fmt + b"data" + struct.pack("<I", len(pcm)) + pcm
open(os.path.join(COMFY_IN, "duocast_clip5.wav"), "wb").write(wav16)
print("input audio: duocast_clip5.wav", len(wav16), "bytes")

# 2) 提交工作流
wf = copy.deepcopy(WF)
swap = os.environ.get("SWAP_BLOCKS")
if swap:
    wf["134"]["inputs"]["blocks_to_swap"] = int(swap)
    wf["134"]["inputs"]["use_non_blocking"] = os.environ.get("SWAP_NONBLOCK", "0") == "1"
tag = os.environ.get("RUN_TAG", "video")
wf["133"]["inputs"]["image"] = PORTRAIT
wf["218"]["inputs"]["audio"] = "duocast_clip5.wav"
wf["229"]["inputs"]["filename_prefix"] = f"duocast_linktest/{tag}"
prompt_id = None
req = urllib.request.Request(BASE + "/prompt", data=json.dumps({"prompt": wf, "client_id": "duocast-linktest-video"}).encode(),
                             headers={"Content-Type": "application/json"})
try:
    prompt_id = json.loads(opener.open(req, timeout=60).read())["prompt_id"]
except urllib.error.HTTPError as e:
    print("PROMPT REJECTED:", e.code); print(e.read().decode("utf-8", "replace")[:4000]); sys.exit(1)
print("prompt_id:", prompt_id, flush=True)

t0 = time.time()
err_ks = set()
while time.time() - t0 < 5400:
    hist = json.loads(opener.open(BASE + f"/history/{prompt_id}", timeout=30).read())
    if prompt_id in hist:
        h = hist[prompt_id]
        st = h.get("status", {})
        if st.get("status_str") == "error":
            print(f"COMFY ERROR after {time.time()-t0:.0f}s")
            print(json.dumps(h, ensure_ascii=False)[:6000]); sys.exit(1)
        outs = h.get("outputs", {})
        if outs:
            print(f"done in {time.time()-t0:.0f}s")
            for nid, o in outs.items():
                for v in o.get("gifs", []) or o.get("videos", []) or []:
                    q = f"/view?filename={v['filename']}&type=output&subfolder={v.get('subfolder','')}"
                    fn = os.path.join(OUT_DIR, os.path.basename(v["filename"]))
                    with opener.open(BASE + q, timeout=120) as r:
                        open(fn, "wb").write(r.read())
                    print("saved:", fn, os.path.getsize(fn), "bytes")
            sys.exit(0)
    time.sleep(10)
print("TIMEOUT after 90min")
sys.exit(2)
