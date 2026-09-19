# -*- coding: utf-8 -*-
"""MultiTalk 双人同框渲染：duo_stage.png + A/B 掩码 + track_a/track_b 双轨 para 驱动。
用法: python make_video_duo_mt.py [test|full]   test=前5秒（A说B听），full=整段18.78s。"""
import copy, json, os, shutil, struct, subprocess, sys, time, urllib.request, wave

MODE = sys.argv[1] if len(sys.argv) > 1 else "test"
BASE = "http://127.0.0.1:8188"
ROOT = r"C:\Users\Administrator\Desktop\双人播客项目"
COMFY_IN = os.environ.get("COMFY_INPUT_DIR", r"D:\PPT_Studio_Assets\InfiniteTalk_TTS\InfiniteTalk_Runtime\ComfyUI\input")
D = os.path.join(ROOT, "链路实测", "_duo")
OUT_DIR = os.path.join(ROOT, "链路实测", "_video")
WF = json.load(open(os.path.join(ROOT, "本地引擎工作流", "infinitetalk-数字人_api_windows兼容.json"), encoding="utf-8"))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CLIP_S = 5.0 if MODE == "test" else None
tag = f"duo_mt_{MODE}"

def prep_audio(src, dst):
    if CLIP_S is None:
        shutil.copyfile(src, dst)
        return
    w = wave.open(src)
    w.setpos(0)
    frames = w.readframes(int(CLIP_S * 48000))
    w.close()
    o = wave.open(dst, "wb")
    o.setnchannels(1); o.setsampwidth(2); o.setframerate(48000)
    o.writeframes(frames)
    o.close()

shutil.copyfile(os.path.join(D, "duo_stage.png"), os.path.join(COMFY_IN, "duocast_duo_stage.png"))
mdir = os.path.join(COMFY_IN, "duocast_masks")
os.makedirs(mdir, exist_ok=True)
for spk in ("a", "b"):
    shutil.copyfile(os.path.join(D, f"mask_{spk}_480.png"), os.path.join(mdir, f"mask_{spk}.png"))
    prep_audio(os.path.join(D, f"track_{spk}.wav"), os.path.join(COMFY_IN, f"duocast_{tag}_track_{spk}.wav"))
prep_audio(os.path.join(ROOT, "链路实测", "_master_4sent.wav"), os.path.join(COMFY_IN, f"duocast_{tag}_master.wav"))

wf = copy.deepcopy(WF)
wf["120"]["inputs"]["model"] = os.environ.get(
    "MT_MODEL", r"InfiniteTalk\Wan2_1-InfiniteTalk-Multi_fp8_e4m3fn_scaled_KJ.safetensors")  # 多人权重：Single 权重跑 multitalk 模式会原生崩溃
AUTO = os.environ.get("MT_AUTO", "1") == "1"  # infinitetalk 模式 + Multi 权重 + 混合音轨：模型自动识别说话人，绕开 mask 路径
wf["133"]["inputs"]["image"] = "duocast_duo_stage.png"
wf["192"]["inputs"]["mode"] = "infinitetalk" if AUTO else "multitalk"
wf["192"]["inputs"]["colormatch"] = "reinhard"  # 合法枚举：disabled/mkl/hm/reinhard/mvgd/...；mkl 在全黑 letterbox 边上协方差奇异
wf["213"]["inputs"]["fit"] = "crop"             # 裁边不留黑条，避免色匹配退化
wf["218"]["inputs"]["audio"] = f"duocast_{tag}_master.wav"
del wf["197"]  # 弃用声源分离：双轨由 300/301 直接提供
wf["300"] = {"class_type": "VHS_LoadAudioUpload", "inputs": {"audio": f"duocast_{tag}_track_a.wav"}}
wf["301"] = {"class_type": "VHS_LoadAudioUpload", "inputs": {"audio": f"duocast_{tag}_track_b.wav"}}
wf["302"] = {"class_type": "VHS_LoadImagesPath", "inputs": {"directory": mdir, "image_load_cap": 0, "skip_first_images": 0, "select_every_nth": 1}}
wf["303"] = {"class_type": "ImageToMask", "inputs": {"image": ["302", 0], "channel": "red"}}  # VHS 的 MASK 口取 alpha（无alpha→全黑），必须走红通道
wf["198"]["inputs"]["audio_1"] = ["300", 0]
wf["198"]["inputs"]["audio_2"] = ["301", 0]
wf["198"]["inputs"]["ref_target_masks"] = ["303", 0]
if AUTO:  # 单轨混合音频、无掩码：走 InfiniteTalk 自动多人路径
    wf["198"]["inputs"]["audio_1"] = ["218", 0]
    del wf["198"]["inputs"]["audio_2"]
    del wf["198"]["inputs"]["ref_target_masks"]
    for nid in ("300", "301", "302", "303"):
        del wf[nid]
wf["198"]["inputs"]["multi_audio_type"] = "para"
wf["134"]["inputs"]["blocks_to_swap"] = int(os.environ.get("SWAP_BLOCKS", "38"))  # multitalk 掩码路径显存压力更高，swap 需大于单人版的 32
wf["229"]["inputs"]["filename_prefix"] = f"duocast_linktest/{tag}"
if os.environ.get("NO_TEACACHE", "1") == "1":  # multitalk+teacache 疑似原生崩溃源，默认摘除
    del wf["240"]
    for k, v in list(wf["199"]["inputs"].items()):
        if isinstance(v, list) and v[0] == "240":
            del wf["199"]["inputs"][k]
wf["192"]["inputs"]["force_offload"] = os.environ.get("MT_FORCE_OFFLOAD", "0") == "1"

req = urllib.request.Request(BASE + "/prompt",
                             data=json.dumps({"prompt": wf, "client_id": "duocast-duo-mt"}).encode(),
                             headers={"Content-Type": "application/json"})
try:
    prompt_id = json.loads(opener.open(req, timeout=60).read())["prompt_id"]
except urllib.error.HTTPError as e:
    print("PROMPT REJECTED:", e.code)
    print(e.read().decode("utf-8", "replace")[:4000])
    sys.exit(1)
print("mode", MODE, "prompt_id:", prompt_id, flush=True)

t0 = time.time()
out_fn = os.path.join(OUT_DIR, f"{tag}.mp4")
while time.time() - t0 < 5400:
    hist = json.loads(opener.open(BASE + f"/history/{prompt_id}", timeout=30).read())
    if prompt_id in hist:
        h = hist[prompt_id]
        if h.get("status", {}).get("status_str") == "error":
            print(f"COMFY ERROR after {time.time()-t0:.0f}s")
            print(json.dumps(h, ensure_ascii=False)[:6000])
            sys.exit(1)
        outs = h.get("outputs", {})
        if outs:
            for nid, o in outs.items():
                for v in o.get("gifs", []) or o.get("videos", []) or []:
                    if not v["filename"].endswith("-audio.mp4"):
                        continue
                    q = f"/view?filename={v['filename']}&type=output&subfolder={v.get('subfolder','')}"
                    with opener.open(BASE + q, timeout=120) as r:
                        open(out_fn, "wb").write(r.read())
                    print(f"done in {time.time()-t0:.0f}s saved", out_fn, os.path.getsize(out_fn))
                    subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,duration", "-of", "csv", out_fn])
                    sys.exit(0)
            print("finished without audio mp4:", json.dumps(outs)[:800])
            sys.exit(1)
    time.sleep(10)
print("TIMEOUT")
sys.exit(2)
