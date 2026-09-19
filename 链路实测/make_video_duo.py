# -*- coding: utf-8 -*-
"""双人播客端到端渲染：4 个话轮单元 wav × duo_a/duo_b 场景头像 → InfiniteTalk 分段 → ffmpeg 拼接。
话轮顺序 A,B,A,B；段间补 0.32s 静音对齐母轨 18.78s 节奏。需 Runtime ComfyUI@8188。"""
import copy, json, os, shutil, struct, subprocess, sys, time, urllib.request

BASE = "http://127.0.0.1:8188"
ROOT = r"C:\Users\Administrator\Desktop\双人播客项目"
COMFY_IN = os.environ.get("COMFY_INPUT_DIR", r"D:\PPT_Studio_Assets\InfiniteTalk_TTS\InfiniteTalk_Runtime\ComfyUI\input")
ART = os.path.join(ROOT, "apps", "storage", "artifacts", "audio")
OUT_DIR = os.path.join(ROOT, "链路实测", "_video")
os.makedirs(OUT_DIR, exist_ok=True)
WF = json.load(open(os.path.join(ROOT, "本地引擎工作流", "infinitetalk-数字人_api_windows兼容.json"), encoding="utf-8"))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 话轮 -> (说话人, 单元wav)。EP3763 R66F6C2FC0AB4，映射来自项目文件 adoptedAudioAssetId
TURNS = [
    ("T01", "a", "AUD-30f0e05a3a97.wav"),
    ("T02", "b", "AUD-829071118d1d.wav"),
    ("T03", "a", "AUD-55dc98f0d426.wav"),
    ("T04", "b", "AUD-438521f6a3ae.wav"),
]

for host in ("a", "b"):
    shutil.copyfile(os.path.join(ROOT, "链路实测", "_duo", f"duo_{host}.png"),
                    os.path.join(COMFY_IN, f"duocast_duo_{host}.png"))
for tid, spk, wav in TURNS:
    dst = os.path.join(COMFY_IN, f"duocast_{tid}.wav")
    shutil.copyfile(os.path.join(ART, wav), dst)
    data = open(dst, "rb").read()
    i = data.find(b"data")
    nbytes = struct.unpack("<I", data[i+4:i+8])[0]
    print(f"{tid} spk={spk.upper()} wav={nbytes/2/48000:.2f}s", flush=True)

def render(tid, spk):
    fn = os.path.join(OUT_DIR, f"duo_{tid}.mp4")
    if os.path.exists(fn):
        print(f"[{tid}] reuse existing {fn}", flush=True)
        return fn
    wf = copy.deepcopy(WF)
    wf["133"]["inputs"]["image"] = f"duocast_duo_{spk}.png"
    wf["218"]["inputs"]["audio"] = f"duocast_{tid}.wav"
    wf["229"]["inputs"]["filename_prefix"] = f"duocast_linktest/duo_{tid}"
    req = urllib.request.Request(BASE + "/prompt",
                                 data=json.dumps({"prompt": wf, "client_id": "duocast-duo"}).encode(),
                                 headers={"Content-Type": "application/json"})
    prompt_id = json.loads(opener.open(req, timeout=60).read())["prompt_id"]
    print(f"[{tid}] submitted prompt_id={prompt_id}", flush=True)
    t0 = time.time()
    while time.time() - t0 < 3600:
        hist = json.loads(opener.open(BASE + f"/history/{prompt_id}", timeout=30).read())
        if prompt_id in hist:
            h = hist[prompt_id]
            if h.get("status", {}).get("status_str") == "error":
                print(f"[{tid}] COMFY ERROR after {time.time()-t0:.0f}s")
                print(json.dumps(h, ensure_ascii=False)[:4000]); sys.exit(1)
            outs = h.get("outputs", {})
            if outs:
                for nid, o in outs.items():
                    for v in o.get("gifs", []) or o.get("videos", []) or []:
                        if not v["filename"].endswith("-audio.mp4"):
                            continue
                        q = f"/view?filename={v['filename']}&type=output&subfolder={v.get('subfolder','')}"
                        with opener.open(BASE + q, timeout=120) as r:
                            open(fn, "wb").write(r.read())
                        print(f"[{tid}] done in {time.time()-t0:.0f}s saved {fn} {os.path.getsize(fn)}B", flush=True)
                        return fn
                print(f"[{tid}] finished but no audio mp4 in outputs", json.dumps(outs)[:800]); sys.exit(1)
        time.sleep(10)
    print(f"[{tid}] TIMEOUT"); sys.exit(2)

segments = [render(tid, spk) for tid, spk, _ in TURNS]

# 拼接：前 3 段音频尾补 0.32s 静音，concat demuxer 统一重编码
padded = []
for i, s in enumerate(segments):
    p = os.path.join(OUT_DIR, f"_duo_pad{i}.mp4")
    if i < len(segments) - 1:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", s, "-c:v", "copy",
                        "-af", "apad=pad_dur=0.32", "-c:a", "aac", "-b:a", "128k", p], check=True)
    else:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", s, "-c", "copy", p], check=True)
    padded.append(p)
lst = os.path.join(OUT_DIR, "_duo_list.txt")
open(lst, "w").write("\n".join(f"file '{os.path.basename(p)}'" for p in padded))
final = os.path.join(OUT_DIR, "duocast_duo_4sent.mp4")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", final], check=True)
print("FINAL:", final, os.path.getsize(final), "bytes")
subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                "stream=codec_name,width,height,duration:format=duration",
                "-of", "default=nw=1", final])
