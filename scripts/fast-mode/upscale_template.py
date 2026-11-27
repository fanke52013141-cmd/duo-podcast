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
import os
import shutil
import subprocess
from pathlib import Path

ALPHA = 0.5       # fresh SR weight vs flow-propagated detail
FB_THRESH = 1.0   # forward-backward flow consistency threshold (source pixels)
SCALE = 4         # Real-ESRGAN factor; output downscaled to 2x
GENERATED_DIRS = ('frames_A', 'frames_B', 'sr_A', 'sr_B', 'stable_A', 'stable_B', 'new_assets', 'validation')


def _linked(path):
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


def _remove_generated(path):
    if _linked(path):
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def reset_work(work):
    """Allow reruns without letting frames from a longer old template survive."""
    work = Path(work)
    if work.exists():
        if not work.is_dir():
            raise ValueError('work must be a directory')
        unknown = [path.name for path in work.iterdir() if path.name not in GENERATED_DIRS]
        if unknown:
            raise ValueError('work 必须是全新目录或仅包含本工具生成的子目录；请换一个空 work 目录')
    for name in GENERATED_DIRS:
        _remove_generated(work / name)
    work.mkdir(parents=True, exist_ok=True)
    return work


def probe_video(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                             'stream=width,height,nb_frames,avg_frame_rate', '-of', 'json', str(path)],
                            capture_output=True, text=True, check=True, timeout=30)
    stream = json.loads(result.stdout)['streams'][0]
    return {'width': int(stream['width']), 'height': int(stream['height']),
            'frames': int(stream['nb_frames']), 'fps': stream['avg_frame_rate']}


def rd(p):
    import cv2
    import numpy as np
    return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)


def wr(p, img):
    import cv2
    ok, buf = cv2.imencode('.png', img)
    if not ok:
        raise RuntimeError(f'encode failed: {p}')
    buf.tofile(str(p))


def extract(runtime, work, speaker, expected_frames):
    frames = work / f'frames_{speaker}'
    frames.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(runtime / f'assets/template_{speaker}.mp4'),
                    str(frames / 'f%04d.png')], check=True)
    expected = {f'f{index:04d}.png' for index in range(1, expected_frames + 1)}
    actual = {path.name for path in frames.glob('f*.png')}
    if actual != expected:
        raise ValueError(f'{speaker} 抽帧数量与源模板不一致，疑似旧帧混入或源视频损坏')
    return frames


def super_resolve(realesrgan, frames, work, speaker):
    sr = work / f'sr_{speaker}'
    sr.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(realesrgan), '-i', str(frames), '-o', str(sr),
                    '-n', 'realesrgan-x4plus', '-s', str(SCALE)], check=True)
    expected = {path.name for path in frames.glob('f*.png')}
    actual = {path.name for path in sr.glob('f*.png')}
    if actual != expected:
        raise ValueError(f'{speaker} 超分帧数量与抽帧不一致，不能继续稳定化')
    return sr


def stabilize(frames, sr, work, speaker, out_w, out_h):
    import cv2
    import numpy as np
    out = work / f'stable_{speaker}'
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(frames.glob('f*.png'))
    if {path.name for path in sr.glob('f*.png')} != {path.name for path in files}:
        raise ValueError('超分目录与抽帧目录不一致')
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    prev_low = prev_stab = grid = None
    for idx, f in enumerate(files):
        low, cur = rd(f), rd(sr / f.name)
        if low is None or cur is None:
            raise RuntimeError(f'missing source or SR frame {f.name}')
        if cur.shape[:2] != (low.shape[0] * SCALE, low.shape[1] * SCALE):
            raise RuntimeError(f'unexpected SR dimensions for {f.name}')
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


def validate_outputs(work, new_assets, source_runtime, expected_size, expected_frames):
    """Run the canonical template validator on the exact files to be installed."""
    from validate_template import validate
    root = work / 'validation'
    _remove_generated(root)
    assets = root / 'assets'
    assets.mkdir(parents=True)
    shutil.copyfile(source_runtime / 'assets/manifest.json', assets / 'manifest.json')
    for name in ('template_A.mp4', 'template_B.mp4', 'face_coordinates.json'):
        source, target = new_assets / name, assets / name
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)
    report = validate(root)
    for role in report['roles']:
        if ((role['width'], role['height'], role['frames']) !=
                (expected_size[0], expected_size[1], expected_frames)):
            raise ValueError('超分模板尺寸或帧数与源模板不一致')
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--runtime', required=True, type=Path)
    ap.add_argument('--realesrgan', required=True, type=Path)
    ap.add_argument('--work', required=True, type=Path)
    args = ap.parse_args()
    runtime, work = args.runtime.resolve(), reset_work(args.work.resolve())
    realesrgan = args.realesrgan.resolve()
    if not realesrgan.is_file():
        raise ValueError('Real-ESRGAN executable not found')
    from validate_template import validate as validate_source
    validate_source(runtime)
    specs = {speaker: probe_video(runtime / f'assets/template_{speaker}.mp4') for speaker in ('A', 'B')}
    identities = {(spec['width'], spec['height'], spec['frames'], spec['fps']) for spec in specs.values()}
    if len(identities) != 1 or next(iter(identities))[3] != '25/1':
        raise ValueError('A/B source templates must share dimensions, frame count and 25 fps')
    size, frames = specs['A'], specs['A']['frames']
    out_w, out_h = size['width'] * 2, size['height'] * 2
    coords = json.loads((runtime / 'assets/face_coordinates.json').read_text(encoding='utf-8'))
    new_assets = work / 'new_assets'
    new_assets.mkdir(parents=True, exist_ok=True)
    new_coords = {}
    for speaker in ('A', 'B'):
        extracted = extract(runtime, work, speaker, size['frames'])
        sr = super_resolve(realesrgan, extracted, work, speaker)
        stable = stabilize(extracted, sr, work, speaker, out_w, out_h)
        target = new_assets / f'template_{speaker}.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', '25', '-i', str(stable / 'f%04d.png'),
                        '-c:v', 'libx264', '-crf', '14', '-pix_fmt', 'yuv420p', str(target)], check=True)
        output_spec = probe_video(target)
        if (output_spec['width'], output_spec['height'], output_spec['frames'], output_spec['fps']) != (out_w, out_h, frames, '25/1'):
            raise ValueError(f'{speaker} 超分视频帧数、尺寸或帧率校验失败')
        new_coords[speaker] = [[int(value * 2) for value in box] for box in coords[speaker]]
        print(f'{speaker}: {target} ({out_w}x{out_h})', flush=True)
    (new_assets / 'face_coordinates.json').write_text(json.dumps(new_coords), encoding='utf-8')
    report = validate_outputs(work, new_assets, runtime, (out_w, out_h), frames)
    print(f'new assets ready: {new_assets}\nBack up runtime assets, copy these over them, review a sample, then re-pin projects.')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()