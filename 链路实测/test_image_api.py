# -*- coding: utf-8 -*-
"""实测图片链路：gpt-image-2-vip @ codex2api.com，参数原样使用用户提供的 JSON。"""
import base64, json, os, sys, time, urllib.request

CRED_FILE = r"D:\Program Files (x86)\PPT_presentation_video\data\credentials.json"
CRED_REF = "credential://2ec155c738664a65a3556a0dd96585a5"
ENDPOINT = "https://www.codex2api.com/v1/images/generations"
OUT_DIR = r"C:\Users\Administrator\Desktop\双人播客项目\链路实测\_image"
os.makedirs(OUT_DIR, exist_ok=True)

with open(CRED_FILE, encoding="utf-8") as f:
    api_key = json.load(f)["credentials"][CRED_REF]["secret_values"]["api_key"]

payload = {
    "model": "gpt-image-2-vip",
    "prompt": "生成一张高级时装广告图：模特穿着结构感象牙白外套，柔和棚拍光线，面料细节清晰，编辑部大片构图。",
    "size": "4:5",
    "n": 4,
    "response_format": "b64_json",
    "background": "auto",
    "metadata": {"resolution": "1K", "orientation": "portrait"},
    "quality": "low",
}

req = urllib.request.Request(
    ENDPOINT,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
)
t0 = time.time()
handler = urllib.request.ProxyHandler({})  # 直连，绕开本地代理干扰
opener = urllib.request.build_opener(handler)
try:
    with opener.open(req, timeout=600) as resp:
        body = resp.read()
        status = resp.status
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code} after {time.time()-t0:.1f}s")
    print(e.read().decode("utf-8", "replace")[:2000])
    sys.exit(1)
except Exception as e:
    print(f"FAILED after {time.time()-t0:.1f}s: {type(e).__name__}: {e}")
    sys.exit(1)

dt = time.time() - t0
data = json.loads(body)
items = data.get("data") or []
print(f"OK HTTP {status} in {dt:.1f}s, images={len(items)}")
for i, it in enumerate(items):
    b64 = it.get("b64_json")
    if b64:
        p = os.path.join(OUT_DIR, f"img_{i}.png")
        with open(p, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"  saved {p} ({os.path.getsize(p)/1024:.0f} KB)")
    elif it.get("url"):
        print(f"  url: {it['url'][:120]}")
if data.get("error"):
    print("error:", json.dumps(data["error"], ensure_ascii=False)[:500])
