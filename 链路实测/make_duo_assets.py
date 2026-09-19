# -*- coding: utf-8 -*-
"""用 ToAPIs 生成双人播客素材：主播A头像、主播B头像、播客场景图。
用法: python make_duo_assets.py   (需环境变量 DUOCAST_TOAPIS_KEY)"""
import json, os, sys, time, urllib.request

KEY = os.environ.get("DUOCAST_TOAPIS_KEY") or sys.exit("missing DUOCAST_TOAPIS_KEY")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_duo")
os.makedirs(OUT, exist_ok=True)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HDRS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

JOBS = [
    ("host_a.png", {
        "model": "gpt-image-2-vip",
        "prompt": "3D动画电影风格角色立绘：一位年轻男主播，圆脸大眼，温和笑容，深棕色短发，穿藏蓝色针织衫白T领口，正面胸像特写，面部居于画面正中且占画面高度约60%，纯白色无缝背景，柔和影棚光，皮克斯质感，高清，画面中只有一个人物，不要文字",
        "size": "1:1", "n": 1, "quality": "medium", "background": "auto",
        "resolution": "1k", "response_format": "url"}),
    ("host_b.png", {
        "model": "gpt-image-2-vip",
        "prompt": "3D动画电影风格角色立绘：一位年轻女主播，圆脸大眼，开朗笑容，黑色齐肩短发别一枚黄色发卡，穿暖橙色卫衣，正面胸像特写，面部居于画面正中且占画面高度约60%，纯白色无缝背景，柔和影棚光，皮克斯质感，高清，画面中只有一个人物，不要文字。与同系列男主播角色同一美术风格",
        "size": "1:1", "n": 1, "quality": "medium", "background": "auto",
        "resolution": "1k", "response_format": "url"}),
    ("scene.png", {
        "model": "gpt-image-2-vip",
        "prompt": "温馨播客录音棚插画背景：浅米色墙面，中央一张原木桌，桌上两支麦克风和一盆小绿植，背景书架与暖色灯串，柔和光线，扁平3D动画电影美术风格，横构图16:9，无人物，不要文字",
        "size": "16:9", "n": 1, "quality": "medium", "background": "auto",
        "resolution": "1k", "response_format": "url"}),
]

def call(url, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None, headers=HDRS)
    with opener.open(req, timeout=90) as r:
        return json.loads(r.read())

base = "https://toapis.com"
for name, payload in JOBS:
    fn = os.path.join(OUT, name)
    if os.path.exists(fn):
        print("skip existing", name); continue
    task = call(base + "/v1/images/generations", payload)
    tid = task.get("id") or task.get("data")
    print(name, "submitted", tid, flush=True)
    t0 = time.time()
    while time.time() - t0 < 600:
        q = call(f"{base}/v1/images/generations/{tid}")
        s = q.get("status")
        if s == "completed":
            res = q.get("result") if isinstance(q.get("result"), dict) else {}
            items = res.get("data") or q.get("data") or []
            u = next((i.get("url") for i in items if isinstance(i, dict) and i.get("url")), None)
            if not u:
                print("NO URL:", json.dumps(q, ensure_ascii=False)[:400]); sys.exit(1)
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=120) as r:
                open(fn, "wb").write(r.read())
            print("saved", fn, os.path.getsize(fn), "bytes", f"[{time.time()-t0:.0f}s]", flush=True)
            break
        if s == "failed":
            print("TASK FAILED:", json.dumps(q, ensure_ascii=False)[:600]); sys.exit(1)
        time.sleep(6)
    else:
        print("TIMEOUT", name); sys.exit(2)
print("ALL DONE")
