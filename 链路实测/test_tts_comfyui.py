# -*- coding: utf-8 -*-
"""实测语音链路：向本地 ComfyUI(8188) 提交 IndexTTS 2.5 四节点工作流，产出 wav。
参考音频用 PPT 项目已有的克隆声线 mp3（只读引用，不改动其任何文件）。"""
import json, os, sys, time, urllib.request, uuid

BASE = "http://127.0.0.1:8188"
WF = json.load(open(r"C:\Users\Administrator\Desktop\双人播客项目\本地引擎工作流\indextts2.5-tts_参考工作流.json", encoding="utf-8"))
REF_MP3 = r"D:\Program Files (x86)\PPT_presentation_video\data\model_voice_references\76abe9e06a9d4670b40b0d966faa652c.mp3"
OUT_DIR = r"C:\Users\Administrator\Desktop\双人播客项目\链路实测\_tts"
os.makedirs(OUT_DIR, exist_ok=True)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def api(path, data=None, files=None, method=None):
    url = BASE + path
    if files:
        boundary = uuid.uuid4().hex
        body = b""
        for k, (fn, content) in files.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
            body += content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    elif data is not None:
        req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method=method or "POST")
    else:
        req = urllib.request.Request(url, method=method or "GET")
    with opener.open(req, timeout=120) as r:
        return json.loads(r.read())

# 1) 放置参考音频（/input/upload 在本版本返回 405，改为本机直拷 input 目录）
import shutil
ref_name = "duocast_ref_A.mp3"
COMFY_INPUT = r"D:\软件\Wan2.2-ReMix-SVI2-V3\Wan2.2-ReMix-SVI2-V3\ComfyUI\input"
shutil.copyfile(REF_MP3, os.path.join(COMFY_INPUT, ref_name))
print("copied reference to ComfyUI input:", ref_name)

# 2) 组装 prompt
import copy
wf = copy.deepcopy(WF)
wf["2"]["inputs"]["text_文本"] = "欢迎来到双声播客。今天我们来聊聊城市里那些正在消失的声音，比如清晨巷口的早餐摊，还有傍晚收摊的吆喝。"
wf["2"]["inputs"]["duration_factor_语速因子"] = 1.1
wf["2"]["inputs"]["reference_audio_参考音频"] = ["4", 0]
wf["4"]["inputs"]["audio_音频"] = ref_name
wf["3"]["inputs"]["filename_prefix_文件名前缀"] = "duocast_test/tts"
prompt_id = api("/prompt", {"prompt": wf, "client_id": "duocast-linktest"})["prompt_id"]
print("prompt_id:", prompt_id)

# 3) 轮询 history
t0 = time.time()
while time.time() - t0 < 900:
    hist = api(f"/history/{prompt_id}")
    if prompt_id in hist:
        h = hist[prompt_id]
        status = h.get("status", {})
        if status.get("status_str") == "error" or status.get("completed") is False and status.get("status_str") == "error":
            print("COMFY ERROR:", json.dumps(h, ensure_ascii=False)[:3000]); sys.exit(1)
        outs = h.get("outputs", {})
        if outs:
            print(f"done in {time.time()-t0:.1f}s")
            for nid, o in outs.items():
                for a in o.get("audio", []) or o.get("images", []) or []:
                    q = f"/view?filename={a['filename']}&type=output" + (f"&subfolder={a['subfolder']}" if a.get("subfolder") else "")
                    with opener.open(BASE + q, timeout=60) as r:
                        fn = os.path.join(OUT_DIR, os.path.basename(a["filename"]))
                        open(fn, "wb").write(r.read())
                    print("saved:", fn, os.path.getsize(fn), "bytes")
            sys.exit(0)
    time.sleep(3)
print("TIMEOUT waiting for job")
sys.exit(2)
