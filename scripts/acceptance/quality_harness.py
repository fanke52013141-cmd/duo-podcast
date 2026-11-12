# -*- coding: utf-8 -*-
"""47号背景退化客观标尺。

背景静止（锁定机位），干净帧的背景应始终贴近干净底图；退化帧会偏离。
对每秒帧的背景区域（上部35%，避开人物主体）计算：
  - bg_div：与底图背景的平均绝对差（0-255，越小越干净）——主判据
  - sharp：背景 Laplacian 方差（高频细节；碎点也会抬高，须结合 bg_div 看）
  - confetti：背景色度局部高频能量（彩色碎点）
判据：bg_div 全程低且平稳（末段 ≤ 首段×1.3 且绝对值小）= 背景未退化。

用法（需 numpy/PIL，用快速引擎 venv）：
  快速模式引擎\\venv\\Scripts\\python.exe scripts/acceptance/quality_harness.py <video> <base_image> [outdir]
"""
import subprocess, sys, os
import numpy as np
from PIL import Image

FF = "ffmpeg"

def lap_var(g):
    g = g.astype(np.float32)
    lap = g[:-2,1:-1]+g[2:,1:-1]+g[1:-1,:-2]+g[1:-1,2:]-4*g[1:-1,1:-1]
    return float(lap.var())

def main():
    video, base = sys.argv[1], sys.argv[2]
    outdir = sys.argv[3] if len(sys.argv) > 3 else os.path.dirname(video)
    os.makedirs(outdir, exist_ok=True)
    # probe dims
    import json
    pr = json.loads(subprocess.run([ "ffprobe","-v","error","-select_streams","v:0",
        "-show_entries","stream=width,height","-of","json",video],capture_output=True,check=True).stdout)
    W,H = pr["streams"][0]["width"], pr["streams"][0]["height"]
    bimg = np.asarray(Image.open(base).convert("RGB").resize((W,H)), dtype=np.float32)
    bh = int(H*0.35)
    bbg = bimg[:bh]                                   # base background (top 35%)
    bbg_gray = bbg.mean(axis=2)
    # decode 1fps frames
    raw = subprocess.run([FF,"-v","error","-i",video,"-vf","fps=1","-f","rawvideo","-pix_fmt","rgb24","-"],
                         capture_output=True,check=True).stdout
    n = len(raw)//(W*H*3)
    frames = np.frombuffer(raw[:n*W*H*3],dtype=np.uint8).reshape(n,H,W,3).astype(np.float32)
    rows=[]
    for i in range(n):
        bg = frames[i,:bh]
        div = float(np.abs(bg-bbg).mean())
        sharp = lap_var(bg.mean(axis=2))
        # chroma confetti: U/V local variation
        r,g,b = bg[:,:,0],bg[:,:,1],bg[:,:,2]
        u = -0.169*r-0.331*g+0.5*b; v = 0.5*r-0.419*g-0.081*b
        conf = float(np.abs(np.diff(u,axis=1)).mean()+np.abs(np.diff(v,axis=1)).mean())
        rows.append((i,div,sharp,conf))
    print(f"video={os.path.basename(video)}  {W}x{H}  frames@1fps={n}")
    print("sec  bg_div  sharp  confetti")
    for i,div,sharp,conf in rows:
        print(f"{i:>3}  {div:6.2f}  {sharp:7.1f}  {conf:6.2f}")
    first_div = rows[0][1]; last_div = rows[-1][1]
    max_div = max(r[1] for r in rows)
    k = max(1, n//3)
    first_third = sum(r[1] for r in rows[:k])/k
    last_third = sum(r[1] for r in rows[-k:])/k
    trend = last_third/first_third if first_third>0 else 0
    print(f"\n首段bg_div={first_div:.2f} 末段bg_div={last_div:.2f} 峰值={max_div:.2f}")
    print(f"前1/3均值={first_third:.2f} 后1/3均值={last_third:.2f} 趋势比(后/前)={trend:.2f}")
    # 主判据=趋势：干净视频背景天然波动但无上升趋势；退化片后段显著高于前段。
    verdict = "PASS 背景未退化" if (trend < 1.3 and last_third < 18) else "FAIL 背景退化"
    print("判定:", verdict, "（阈值: 后/前趋势比<1.3 且 后1/3均值<18）")
    # first vs last comparison
    Image.fromarray(frames[0].astype(np.uint8)).save(os.path.join(outdir,"harness_first.png"))
    Image.fromarray(frames[-1].astype(np.uint8)).save(os.path.join(outdir,"harness_last.png"))
    combo = np.concatenate([frames[0],frames[-1]],axis=1).astype(np.uint8)
    Image.fromarray(combo).save(os.path.join(outdir,"harness_first_vs_last.png"))
    print("对比图:", os.path.join(outdir,"harness_first_vs_last.png"))

if __name__=="__main__":
    main()