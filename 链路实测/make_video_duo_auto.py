# -*- coding: utf-8 -*-
"""双人同框·裁窗回贴方案：从 duo_stage 裁 A/B 说话窗 → InfiniteTalk(单人稳定路径)驱动 → 羽化贴回原坐标。
A 说话时贴 A 窗（B 静止=倾听），B 说话时贴 B 窗。产出 duocast_duo_sameframe.mp4。"""
import copy, json, os, shutil, subprocess, sys, time, urllib.request
from PIL import Image, ImageDraw, ImageFilter

ROOT = r"C:\Users\Administrator\Desktop\双人播客项目"
COMFY_IN = os.environ.get("COMFY_INPUT_DIR", r"D:\PPT_Studio_Assets\InfiniteTalk_TTS\InfiniteTalk_Runtime\ComfyUI\input")
D = os.path.join(ROOT, "链路实测", "_duo")
OUT_DIR = os.path.join(ROOT, "链路实测", "_video")
ART = os.path.join(ROOT, "apps", "storage", "artifacts", "audio")
WF = json.load(open(os.path.join(ROOT, "本地引擎工作流", "infinitetalk-数字人_api_windows兼容.json"), encoding="utf-8"))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = "http://127.0.0.1:8188"

# 1536x864 duo_stage 上的说话窗（正方形，含头部+上身）
BOXES = {"a": (120, 30, 740, 650), "b": (770, 80, 1390, 700)}
TURNS = [("T01", "a", "AUD-30f0e05a3a97.wav"), ("T02", "b", "AUD-829071118d1d.wav"),
         ("T03", "a", "AUD-55dc98f0d426.wav"), ("T04", "b", "AUD-438521f6a3ae.wav")]

stage = Image.open(os.path.join(D, "duo_stage.png")).convert("RGB")
assert stage.size == (1536, 864), stage.size
for spk, (x0, y0, x1, y1) in BOXES.items():
    stage.crop((x0, y0, x1, y1)).save(os.path.join(D, f"win_{spk}.png"))
    shutil.copyfile(os.path.join(D, f"win_{spk}.png"), os.path.join(COMFY_IN, f"duocast_win_{spk}.png"))
    # 羽化 alpha 蒙版（边缘 28px 渐变）
    m = Image.new("L", (x1 - x0, y1 - y0), 255)
    dr = ImageDraw.Draw(m)
    for i in range(28):
        v = int(255 * i / 27)
        dr.rectangle([i, i, x1 - x0 - 1 - i, y1 - y0 - 1 - i], outline=v)
    m = m.filter(ImageFilter.GaussianBlur(10))
    m.save(os.path.join(D, f"win_{spk}_mask.png"))
for tid, spk, wav in TURNS:
    shutil.copyfile(os.path.join(ART, wav), os.path.join(COMFY_IN, f"duocast_{tid}.wav"))
print("windows + masks + audio staged", flush=True)

def render(tid, spk):
    fn = os.path.join(OUT_DIR, f"win_{tid}.mp4")
    if os.path.exists(fn):
        print(f"[{tid}] reuse", flush=True)
        return fn
    wf = copy.deepcopy(WF)
    wf["133"]["inputs"]["image"] = f"duocast_win_{spk}.png"
    wf["218"]["inputs"]["audio"] = f"duocast_{tid}.wav"
    wf["134"]["inputs"]["blocks_to_swap"] = int(os.environ.get("SWAP_BLOCKS", "32"))
    wf["229"]["inputs"]["filename_prefix"] = f"duocast_linktest/win_{tid}"
    req = urllib.request.Request(BASE + "/prompt", data=json.dumps({"prompt": wf, "client_id": "duocast-win"}).encode(),
                                 headers={"Content-Type": "application/json"})
    prompt_id = json.loads(opener.open(req, timeout=60).read())["prompt_id"]
    print(f"[{tid}] submitted {prompt_id}", flush=True)
    t0 = time.time()
    while time.time() - t0 < 14400:
        try:
            hist = json.loads(opener.open(BASE + f"/history/{prompt_id}", timeout=30).read())
        except Exception as e:
            print(f"[{tid}] poll err {type(e).__name__}; waiting for server", flush=True)
            time.sleep(60)
            continue
        if prompt_id in hist:
            h = hist[prompt_id]
            if h.get("status", {}).get("status_str") == "error":
                print(f"[{tid}] ERROR", json.dumps(h, ensure_ascii=False)[:2000]); sys.exit(1)
            outs = h.get("outputs", {})
            if outs:
                got = []
                for nid, o in outs.items():
                    for v in o.get("gifs", []) or o.get("videos", []) or []:
                        got.append(v["filename"])
                        if "-audio" in v["filename"]:
                            q = f"/view?filename={v['filename']}&type=output&subfolder={v.get('subfolder','')}"
                            with opener.open(BASE + q, timeout=120) as r:
                                open(fn, "wb").write(r.read())
                if os.path.exists(fn):
                    print(f"[{tid}] done {time.time()-t0:.0f}s", flush=True)
                    return fn
                print(f"[{tid}] no audio mp4 in {got}"); sys.exit(1)
        time.sleep(10)
    print(f"[{tid}] TIMEOUT"); sys.exit(2)

segs = [render(t, s) for t, s, _ in TURNS]

# 合成：每段 = 底图(循环) + 动画窗回贴(羽化) → 1536x864；拼接后统一缩到 864x480 并配母轨音轨
built = []
for idx, (tid, spk, _) in enumerate(TURNS):
    seg = segs[idx]
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", seg],
                         capture_output=True, text=True).stdout.strip()
    x0, y0, x1, y1 = BOXES[spk]
    gap = 0.0 if idx == len(TURNS) - 1 else 0.32  # 话轮间保留母轨 0.32s 停顿，末帧克隆补齐防口型漂移
    outp = os.path.join(OUT_DIR, f"winb_{tid}.mp4")
    if not os.path.exists(outp):
        tpad = f",tpad=stop_mode=clone:stop_duration={gap}" if gap else ""
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-loop", "1", "-t", str(float(dur) + gap + 0.08), "-i", os.path.join(D, "duo_stage.png"),
               "-i", seg,
               "-i", os.path.join(D, f"win_{spk}_mask.png"),
               "-filter_complex",
               f"[1:v]scale={x1-x0}:{y1-y0}:flags=lanczos{tpad}[ov];[2:v]format=gray[mk];"
               f"[0:v]format=rgba[base];[ov][mk]alphamerge[ova];"
               f"[base][ova]overlay={x0}:{y0}:shortest=1:format=auto,format=yuv420p[v]",
               "-map", "[v]", "-an", "-r", "25", "-c:v", "libx264", "-preset", "medium", "-crf", "19", outp]
        subprocess.run(cmd, check=True)
    built.append(outp)
    print(f"[{tid}] composited {outp}", flush=True)

lst = os.path.join(OUT_DIR, "_win_list.txt")
open(lst, "w").write("\n".join(f"file '{os.path.basename(p)}'" for p in built))
final = os.path.join(OUT_DIR, "duocast_duo_sameframe.mp4")
master = os.path.join(ROOT, "链路实测", "_master_4sent.wav")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
                "-i", master, "-map", "0:v", "-map", "1:a",
                "-vf", "scale=864:480:flags=lanczos", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-shortest", final], check=True)
print("FINAL:", final, os.path.getsize(final), "bytes")
subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,duration", "-of", "csv", final])
