# -*- coding: utf-8 -*-
"""把白底主播图抠像合成进播客场景：A 在左麦位、B 在右麦位，产出 duo_a.png / duo_b.png。"""
import os
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "_duo")

def unwhite(img, threshold=246, feather=18):
    """白底转透明：接近纯白的像素降 alpha，边缘羽化。"""
    img = img.convert("RGB")
    px = img.load()
    w, h = img.size
    out = Image.new("RGBA", (w, h))
    op = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            m = min(r, g, b)
            if m >= threshold:
                a = 0
            elif m <= threshold - feather:
                a = 255
            else:
                a = int(255 * (threshold - m) / feather)
            op[x, y] = (r, g, b, a)
    return out

scene = Image.open(os.path.join(D, "scene.png")).convert("RGB")
SW, SH = scene.size
print("scene", scene.size)

# 麦位横向中心（场景实测：左麦约 x=33%，右麦约 x=66%）
SPOTS = {"a": 0.285, "b": 0.715}

for host, mic in (("a", "host_a.png"), ("b", "host_b.png")):
    im = Image.open(os.path.join(D, mic))
    cut = unwhite(im)
    # 裁掉透明边后按目标高度缩放：人物占场景高 ~78%
    bbox = cut.getbbox()
    cut = cut.crop(bbox)
    target_h = int(SH * 0.78)
    scale = target_h / cut.size[1]
    cut = cut.resize((int(cut.size[0] * scale), target_h), Image.LANCZOS)
    canvas = scene.copy().convert("RGBA")
    cx = int(SW * SPOTS[host])
    paste_x = cx - cut.size[0] // 2
    paste_y = int(SH * 0.97) - cut.size[1]  # 底部落在桌沿附近
    canvas.alpha_composite(cut, (paste_x, max(0, paste_y)))
    out = canvas.convert("RGB")
    fn = os.path.join(D, f"duo_{host}.png")
    out.save(fn)
    print("saved", fn, out.size)
