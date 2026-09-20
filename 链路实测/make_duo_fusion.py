# -*- coding: utf-8 -*-
"""ToAPIs 图生图融合：host_a + host_b + scene → duo_stage.png（两人同框播客图）。
POST /v1/images/edits (multipart)，异步任务轮询。需 DUOCAST_TOAPIS_KEY。"""
import json, mimetypes, os, sys, time, urllib.request, uuid

KEY = os.environ.get("DUOCAST_TOAPIS_KEY") or sys.exit("missing DUOCAST_TOAPIS_KEY")
HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "_duo")
OUT = os.path.join(D, "duo_stage.png")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PROMPT = ("图1是男主播角色，图2是女主播角色，图3是播客录音棚场景。"
          "把图1和图2中的两位角色一字不差地照搬进图3的场景：男主播保持深棕色短发、藏蓝色针织衫内搭白T，坐在长桌左侧，双手交叠放在桌面上；"
          "女主播保持黑色齐肩短发、黄色发卡、暖橙色连帽卫衣，坐在长桌右侧，双手自然平放桌面或轻搭桌沿，不要抬手比划手势。两人各自面前一支桌面麦克风（沿用图3原有的两支麦克风），"
          "两人都面向镜头安静端坐、闭嘴微笑的倾听姿态，嘴巴必须是闭合的，不要张嘴说话的表情。"
          "严格保持图3的场景不变：浅米色墙面、原木长桌、桌上绿植、背景书架、暖色灯串、落地灯、米色扶手椅、整体暖色调，"
          "不要更换家具、不要改变房间风格与光线。3D动画电影风格统一，人物光照与环境一致。"
          "横构图16:9，画面中只有这两个人物，不要文字。")

def multipart(url, fields, files):
    boundary = uuid.uuid4().hex
    body = []
    for k, v in fields.items():
        body += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()]
    for k, fp in files:
        fn = os.path.basename(fp)
        ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
        body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
        body.append(open(fp, "rb").read())
        body.append(b"\r\n")
    body.append(f"--{boundary}--\r\n".encode())
    data = b"".join(body)
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {KEY}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with opener.open(req, timeout=180) as r:
        return json.loads(r.read())

def get(url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}"})
    with opener.open(req, timeout=90) as r:
        return json.loads(r.read())

base = "https://toapis.com"
fields = {"model": "gpt-image-2-vip", "prompt": PROMPT, "size": "16:9", "quality": "medium", "n": "1"}
files = [("image[]", os.path.join(D, "host_a.png")), ("image[]", os.path.join(D, "host_b.png")), ("image[]", os.path.join(D, "scene.png"))]

try:
    task = multipart(base + "/v1/images/edits", fields, files)
except Exception as e:
    detail = ""
    if hasattr(e, "read"):
        try: detail = e.read().decode("utf-8", "replace")[:500]
        except Exception: pass
    print("edits submit failed:", type(e).__name__, e, detail)
    sys.exit(1)
print("submit:", json.dumps(task, ensure_ascii=False)[:300], flush=True)
tid = task.get("id") or task.get("data")
t0 = time.time()
while time.time() - t0 < 600:
    q = None
    for cand in (f"/v1/images/edits/{tid}", f"/v1/images/generations/{tid}", f"/v1/tasks/{tid}"):
        try:
            q = get(base + cand); break
        except urllib.error.HTTPError as e:
            if e.code not in (404, 405):
                print("query", cand, e.code, e.read().decode("utf-8", "replace")[:200])
    if q is None:
        print("no poll endpoint works", json.dumps(task)[:400]); sys.exit(1)
    s = q.get("status")
    print(f"[{time.time()-t0:.0f}s] status={s}", flush=True)
    if s == "completed":
        res = q.get("result") if isinstance(q.get("result"), dict) else {}
        items = res.get("data") or q.get("data") or []
        u = next((i.get("url") for i in items if isinstance(i, dict) and i.get("url")), None)
        if not u:
            print("NO URL:", json.dumps(q, ensure_ascii=False)[:600]); sys.exit(1)
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=120) as r:
            open(OUT, "wb").write(r.read())
        print("saved", OUT, os.path.getsize(OUT), "bytes")
        break
    if s == "failed":
        print("FAILED:", json.dumps(q, ensure_ascii=False)[:800]); sys.exit(1)
    time.sleep(6)
else:
    print("TIMEOUT"); sys.exit(2)
