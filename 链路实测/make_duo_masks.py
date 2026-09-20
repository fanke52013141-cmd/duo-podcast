# -*- coding: utf-8 -*-
"""为 duo_stage.png 生成 A/B 说话人掩码（白=本人区域），并输出叠加预览用于校准。"""
import os
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "_duo")
SOURCE = os.environ.get("DUO_SOURCE", "duo_stage.png")
PREFIX = os.environ.get("MASK_PREFIX", "mask")
base = Image.open(os.path.join(D, SOURCE)).convert("RGB")
W, H = base.size
print("base", base.size)

# 校准参数（图像坐标）：每人 = 头部圆 + 上身椭圆
sx, sy = W / 1536, H / 864
SHAPES = {
    "a": [((430 * sx, 340 * sy), 210 * sx), ((420 * sx, 760 * sy), (330 * sx, 330 * sy))],   # ((cx,cy), r) 或 ((cx,cy),(rx,ry))
    "b": [((1080 * sx, 390 * sy), 210 * sx), ((1080 * sx, 780 * sy), (340 * sx, 330 * sy))],
}

preview = base.copy()
for spk, shapes in SHAPES.items():
    m = Image.new("L", (W, H), 0)
    dr = ImageDraw.Draw(m)
    for center, rad in shapes:
        rx, ry = rad if isinstance(rad, tuple) else (rad, rad)
        cx, cy = center
        dr.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(12))
    m.save(os.path.join(D, f"{PREFIX}_{spk}.png"))
    # 与渲染输出一致的 864x480（213 节点 fit=crop：等比放大到覆盖后中心裁边）
    s = max(864 / W, 480 / H)
    mw, mh = int(round(W * s)), int(round(H * s))
    small = m.resize((mw, mh), Image.LANCZOS)
    top = (mh - 480) // 2
    canvas = small.crop((0, top, 864, top + 480))
    canvas.save(os.path.join(D, f"{PREFIX}_{spk}_480.png"))
    tint = Image.new("RGB", (W, H), (255, 80, 80) if spk == "a" else (80, 160, 255))
    preview = Image.composite(tint, preview, m.point(lambda v: int(v * 0.45)))
preview.save(os.path.join(D, f"_{PREFIX}_preview.png"))
print(f"saved {PREFIX}_a/b.png + {PREFIX}_a/b_480.png + _{PREFIX}_preview.png")
