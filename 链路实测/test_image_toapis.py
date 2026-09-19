# -*- coding: utf-8 -*-
"""实测图片链路（ToAPIs 通道，文档：异步任务式 /v1/images/generations）。"""
import json, os, sys, time, urllib.request

KEY = sys.argv[1]
OUT = r"C:\Users\Administrator\Desktop\双人播客项目\链路实测\_image"
os.makedirs(OUT, exist_ok=True)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HDRS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
payload = {
    "model": "gpt-image-2-vip",
    "prompt": "生成一张高级时装广告图：模特穿着结构感象牙白外套，柔和棚拍光线，面料细节清晰，编辑部大片构图。",
    "size": "4:5", "n": 4, "quality": "low", "background": "auto",
    "resolution": "1k", "response_format": "url",
}

def call(url, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None, headers=HDRS)
    with opener.open(req, timeout=90) as r:
        return json.loads(r.read())

for base in ("https://toapis.com", "https://toapis.cn"):
    try:
        task = call(base + "/v1/images/generations", payload)
        print("SUBMIT OK via", base, "->", json.dumps(task, ensure_ascii=False)[:300])
        break
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try: detail = e.read().decode("utf-8", "replace")[:300]
            except Exception: pass
        print(f"submit FAILED via {base}: {type(e).__name__} {e} {detail}")
        task = None
else:
    sys.exit(1)

tid = task.get("id") or task.get("data")
t0 = time.time()
url = None
q = None
while time.time() - t0 < 600:
    for cand in (f"/v1/images/generations/{tid}", f"/v1/images/tasks/{tid}", f"/v1/tasks/{tid}"):
        try:
            q = call(base + cand)
            url = cand
            break
        except urllib.error.HTTPError as e:
            if e.code not in (404, 405):
                print("query", cand, "->", e.code, e.read().decode("utf-8","replace")[:200])
        except Exception:
            pass
    if not url:
        print("no task query endpoint worked; last payload:", json.dumps(task, ensure_ascii=False)[:400]); sys.exit(1)
    s = q.get("status")
    print(f"[{time.time()-t0:.0f}s] status={s} progress={q.get('progress')}", flush=True)
    if s == "completed":
        import base64
        res = q.get("result") if isinstance(q.get("result"), dict) else {}
        items = res.get("data") or q.get("data") or []
        urls = [i.get("url") for i in items if isinstance(i, dict) and i.get("url")]
        b64s = [i.get("b64_json") for i in items if isinstance(i, dict) and i.get("b64_json")]
        for i, u in enumerate(urls):
            fn = os.path.join(OUT, f"toapis_{i}.png")
            with opener.open(urllib.request.Request(u), timeout=120) as r:
                open(fn, "wb").write(r.read())
            print("saved", fn, os.path.getsize(fn))
        for i, b in enumerate(b64s):
            fn = os.path.join(OUT, f"toapis_{i}.png")
            open(fn, "wb").write(base64.b64decode(b))
            print("saved(b64)", fn, os.path.getsize(fn))
        sys.exit(0)
    if s == "failed":
        print("TASK FAILED:", json.dumps(q, ensure_ascii=False)[:800]); sys.exit(1)
    time.sleep(8)
print("TIMEOUT")
sys.exit(2)
