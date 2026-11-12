"""One-time offline super-resolution upgrade for fast-mode templates.

432x480-class templates upscale 2.25x at 1080p export and look soft on large
screens. This tool rebuilds them at 2x (864x960) with Real-ESRGAN plus
flow-guided temporal stabilization (per-frame GAN detail otherwise "boils").
The result is a drop-in template: same frames/order/timing, boxes scaled 2x.

Requirements:
  - realesrgan-ncnn-vulkan (xinntao/Real-ESRGAN release) with realesrgan-x4plus
  - this runtime's venv (cv2/numpy); ffmpeg on PATH

Usage (from the repo root):
  python scripts/fast-mode/upscale_template.py --runtime <engine root> \
      --realesrgan <path to realesrgan-ncnn-vulkan.exe> --work <scratch dir>

Writes new template_A/B.mp4 + face_coordinates.json into <work>/new_assets.
Install by copying them over the runtime assets (BACK UP the originals first),
then re-pin affected projects (template identity changes). Review a sample
before re-pinning: SR is plausible reconstruction, not true detail recovery.
"""
import argparse
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np

ALPHA = 0.5       # fresh SR weight vs flow-propagated detail
FB_THRESH = 1.0   # forward-backward flow consistency threshold (source pixels)
SCALE = 4         # Real-ESRGAN factor; output downscaled to 2x


def rd(p):
    return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)


def wr(p, img):
    ok, buf = cv2.imencode('.png', img)
    if not ok:
        raise RuntimeError(f'encode failed: {p}')
    buf.tofile(str(p))


def extract(runtime, work, speaker):
    frames = work / f'frames_{speaker}'
    frames.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(runtime / f'assets/template_{speaker}.mp4'),
                    str(frames / 'f%04d.png')], check=True)
    return frames


def super_resolve(realesrgan, frames, work, speaker):
    sr = work / f'sr_{speaker}'
    sr.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(realesrgan), '-i', str(frames), '-o', str(sr),
                    '-n', 'realesrgan-x4plus', '-s', str(SCALE)], check=True)
    return sr


def stabilize(frames, sr, work, speaker, out_w, out_h):
    out = work / f'stable_{speaker}'
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(frames.glob('f*.png'))
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    prev_low = prev_stab = grid = None
    for idx, f in enumerate(files):
        low, cur = rd(f), rd(sr / f.name)
        if cur is None:
            raise RuntimeError(f'missing SR frame {f.name}')
        if prev_low is None:
            stab = cur
        else:
            low_g, prev_g = cv2.cvtColor(low, cv2.COLOR_BGR2GRAY), cv2.cvtColor(prev_low, cv2.COLOR_BGR2GRAY)
            fb = dis.calc(low_g, prev_g, None)   # current -> previous
            fw = dis.calc(prev_g, low_g, None)   # previous -> current
            h, w = fb.shape[:2]
            gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
            sx = np.clip(gx + fb[..., 0], 0, w - 1)
            sy = np.clip(gy + fb[..., 1], 0, h - 1)
            fwx = cv2.remap(fw[..., 0], sx, sy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            fwy = cv2.remap(fw[..., 1], sx, sy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            err = np.sqrt((fb[..., 0] + fwx) ** 2 + (fb[..., 1] + fwy) ** 2)
            mask_low = (err < FB_THRESH).astype(np.uint8) * 255
            if grid is None:
                H, W = cur.shape[:2]
                grid = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
            map_x = grid[0] + cv2.resize(fb[..., 0], (cur.shape[1], cur.shape[0]), interpolation=cv2.INTER_LINEAR) * SCALE
            map_y = grid[1] + cv2.resize(fb[..., 1], (cur.shape[1], cur.shape[0]), interpolation=cv2.INTER_LINEAR) * SCALE
            warped = cv2.remap(prev_stab, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            mask = cv2.resize(mask_low, (cur.shape[1], cur.shape[0]), interpolation=cv2.INTER_NEAREST) > 0
            blend = ALPHA * cur.astype(np.float32) + (1 - ALPHA) * warped.astype(np.float32)
            stab = np.where(mask[..., None], blend, cur.astype(np.float32)).astype(np.uint8)
        prev_low, prev_stab = low, stab
        wr(out / f.name, cv2.resize(stab, (out_w, out_h), interpolation=cv2.INTER_AREA))
        if idx % 50 == 0:
            print(f'{speaker} {idx}/{len(files)}', flush=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--runtime', required=True, type=Path)
    ap.add_argument('--realesrgan', required=True, type=Path)
    ap.add_argument('--work', required=True, type=Path)
    args = ap.parse_args()
    runtime, work = args.runtime.resolve(), args.work.resolve()
    coords = json.loads((runtime / 'assets/face_coordinates.json').read_text())
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                            'stream=width,height', '-of', 'json', str(runtime / 'assets/template_A.mp4')],
                           capture_output=True, text=True, check=True)
    size = json.loads(probe.stdout)['streams'][0]
    out_w, out_h = size['width'] * 2, size['height'] * 2
    new_assets = work / 'new_assets'
    new_assets.mkdir(parents=True, exist_ok=True)
    new_coords = {}
    for speaker in ('A', 'B'):
        frames = extract(runtime, work, speaker)
        sr = super_resolve(args.realesrgan, frames, work, speaker)
        stable = stabilize(frames, sr, work, speaker, out_w, out_h)
        target = new_assets / f'template_{speaker}.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', '25', '-i', str(stable / 'f%04d.png'),
                        '-c:v', 'libx264', '-crf', '14', '-pix_fmt', 'yuv420p', str(target)], check=True)
        new_coords[speaker] = [[int(v * 2) for v in box] for box in coords[speaker]]
        print(f'{speaker}: {target} ({out_w}x{out_h})', flush=True)
    (new_assets / 'face_coordinates.json').write_text(json.dumps(new_coords), encoding='utf-8')
    print(f'new assets ready: {new_assets}\nBack up runtime assets, copy these over them, review a sample, then re-pin projects.')


if __name__ == '__main__':
    main()