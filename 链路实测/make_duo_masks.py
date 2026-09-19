# -*- coding: utf-8 -*-
"""为 duo_stage.png 生成 A/B 说话人掩码（白=本人区域），并输出叠加预览用于校准。"""
import os
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "_duo")
base = Image.open(os.path.join(D, "duo_stage.png")).convert("RGB")
W, H = base.size
print("base", base.size)

# 校准参数（图像坐标）：每人 = 头部圆 + 上身椭圆
SHAPES = {
    "a": [((430, 340), 210), ((420, 760), (330, 330))],   # ((cx,cy), r) 或 ((cx,cy),(rx,ry))
    "b": [((1080, 390), 210), ((1080, 780), (340, 330))],
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
    m.save(os.path.join(D, f"mask_{spk}.png"))
    # 与渲染输出一致的 864x480（213 节点 fit=crop：等比放大到覆盖后中心裁边）
    s = max(864 / W, 480 / H)
    mw, mh = int(round(W * s)), int(round(H * s))
    small = m.resize((mw, mh), Image.LANCZOS)
    top = (mh - 480) // 2
    canvas = small.crop((0, top, 864, top + 480))
    canvas.save(os.path.join(D, f"mask_{spk}_480.png"))
    tint = Image.new("RGB", (W, H), (255, 80, 80) if spk == "a" else (80, 160, 255))
    preview = Image.composite(tint, preview, m.point(lambda v: int(v * 0.45)))
preview.save(os.path.join(D, "_mask_preview.png"))
print("saved mask_a/b.png + mask_a/b_480.png + _mask_preview.png")
