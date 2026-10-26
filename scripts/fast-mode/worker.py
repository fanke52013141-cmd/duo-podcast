"""Offline MuseTalk worker. Receives a server-owned JSON request; no network downloads."""
import os
import sys
import json
import math
import time
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for key in ('HF_HOME', 'TORCH_HOME', 'NUMBA_CACHE_DIR', 'MPLCONFIGDIR', 'XDG_CACHE_HOME'):
    folder = ROOT / 'cache' / key.lower()
    folder.mkdir(parents=True, exist_ok=True)
    os.environ[key] = str(folder)
os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONDONTWRITEBYTECODE='1', TOKENIZERS_PARALLELISM='false')
sys.path.insert(0, str(ROOT / 'vendor/MuseTalk'))
from quality import quality_options
from render_geometry import composite_size, project_box, preparation_key
import cv2
import numpy as np
import torch
from transformers import WhisperModel
from musetalk.models.vae import VAE
from musetalk.models.unet import UNet, PositionalEncoding
from musetalk.utils.audio_processor import AudioProcessor
from musetalk.utils.face_parsing import FaceParsing
from musetalk.utils.face_parsing.model import BiSeNet
from musetalk.utils.blending import get_image

class Parser(FaceParsing):
    def model_init(self, *args, **kwargs):
        weights = torch.load(ROOT / 'assets/parsing_bisenet.pth', map_location='cpu', weights_only=True)
        backbone = ROOT / 'cache/parser_resnet18.pth'
        if not backbone.exists():
            torch.save({k.removeprefix('cp.resnet.'): v for k, v in weights.items() if k.startswith('cp.resnet.')}, backbone)
        net = BiSeNet(str(backbone))
        net.load_state_dict(weights, strict=True)
        return net.eval().cuda()

def read_frames(speaker):
    cap = cv2.VideoCapture(f'assets/template_{speaker}.mp4')
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok: break
        frames.append(frame)
    cap.release()
    return frames

def schedule(frames, original, turns, speaker, count):
    """Choose separate speaking/listening banks, with nearest-pose joins and ping-pong loops."""
    banks = {}
    for speaking in (False, True):
        indices = [i for i in range(len(frames)) if (
            any(t['speaker'] == speaker and t['start'] <= i / 25 < t['end'] for t in original)
            if speaking else not any(t['speaker'] == speaker and t['start'] - .12 <= i / 25 < t['end'] + .12 for t in original))]
        groups = []
        for i in indices:
            if not groups or i != groups[-1][-1] + 1: groups.append([])
            groups[-1].append(i)
        if not groups: raise ValueError('Template lacks a required state bank')
        banks[speaking] = max(groups, key=len)
    poses = [cv2.resize(f[:300], (36, 25)).astype(np.int16) for f in frames]
    chosen, active = [], []
    state = None
    cursor = 0
    direction = 1
    for i in range(count):
        talking = any(t['speaker'] == speaker and t['start'] <= i / 25 < t['end'] for t in turns)
        bank = banks[talking]
        if talking != state:
            prev = poses[chosen[-1]] if chosen else poses[bank[0]]
            cursor = min(range(len(bank)), key=lambda k: np.abs(poses[bank[k]] - prev).mean())
            direction = 1 if cursor < len(bank) - 1 else -1
            state = talking
        chosen.append(bank[cursor])
        active.append(talking)
        if len(bank) > 1:
            if not 0 <= cursor + direction < len(bank): direction *= -1
            cursor += direction
    return chosen, active

def encoder(path, width, height, crf):
    return subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
        '-s', f'{width}x{height}', '-r', '25', '-i', 'pipe:0', '-an', '-c:v', 'libx264',
        '-preset', 'fast', '-crf', str(crf), '-pix_fmt', 'yuv420p', str(path)], stdin=subprocess.PIPE)

@torch.inference_mode()
def run(request):
    os.chdir(ROOT)
    started = time.monotonic()
    out = Path(request['outputDir'])
    out.mkdir(parents=True, exist_ok=True)
    quality_name = request.get('fastQuality', 'original')
    quality = quality_options(quality_name)
    duration = request['sampleCount'] / request['sampleRate']
    if not 0 < duration <= 300: raise ValueError('Duration outside 0–300 seconds')
    count = math.ceil(duration * 25)
    models = ROOT / 'models'
    torch.manual_seed(request.get('seed', 42))
    torch.cuda.reset_peak_memory_stats()
    vae = VAE(str(models / 'sd-vae'), use_float16=True)
    unet = UNet(str(models / 'musetalkV15/musetalk.json'), str(models / 'musetalkV15/unet.pth'), use_float16=True, device='cuda')
    unet.model.eval()
    pe = PositionalEncoding(d_model=384).half().cuda()
    whisper = WhisperModel.from_pretrained(str(models / 'whisper'), local_files_only=True).half().cuda().eval()
    processor = AudioProcessor(str(models / 'whisper'))
    parser = Parser()
    if request.get('motionAssets'):
        from motion_worker import run_motion
        return run_motion(request, {'torch': torch, 'cv2': cv2, 'np': np, 'ROOT': ROOT,
            'quality': quality, 'vae': vae, 'unet': unet, 'pe': pe, 'whisper': whisper,
            'processor': processor, 'parser': parser, 'get_image': get_image})
    source = json.loads((ROOT / 'assets/manifest.json').read_text(encoding='utf-8'))
    coords = json.loads((ROOT / 'assets/face_coordinates.json').read_text(encoding='utf-8'))
    stats = {}
    export_width, export_height = map(int, request.get('resolution', '1920x1080').split('x'))
    source_sizes = {}
    for speaker in ('A', 'B'):
        frames = read_frames(speaker)
        if not frames or len(frames) != len(coords[speaker]): raise ValueError('Incomplete motion template')
        indices, active = schedule(frames, source['turns'], request['turns'], speaker, count)
        key = preparation_key(ROOT/f'assets/template_{speaker}.mp4', coords[speaker], models/'sd-vae/config.json', models/'sd-vae/diffusion_pytorch_model.bin')
        cache = ROOT / 'cache' / f'latents_v2_{speaker}_{key}.pt'
        if cache.exists():
            latents = torch.load(cache, map_location='cpu', weights_only=True)
        else:
            latents = []
            for frame, (x1, y1, x2, y2) in zip(frames, coords[speaker]):
                crop = cv2.resize(frame[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4)
                latents.append(vae.get_latents_for_unet(crop).cpu())
            latents = torch.cat(latents)
            torch.save(latents, cache)
        features, n = processor.get_audio_feature(request['tracks'][speaker])
        chunks = processor.get_whisper_chunk(features, 'cuda', torch.float16, whisper, n, fps=25)
        if len(chunks) < count - 1: raise ValueError('Audio feature track too short')
        if len(chunks) < count: chunks = torch.cat([chunks, chunks[-1:]])
        source_height, source_width = frames[0].shape[:2]
        source_sizes[speaker] = [source_width, source_height]
        width, height = composite_size(source_width, source_height, export_width, export_height)
        process = encoder(out / f'role_{speaker}.mp4', width, height, quality['roleCrf'])
        try:
            for first in range(0, count, 8):
                target = list(range(first, min(first + 8, count)))
                batch = [i for i in target if active[i]]
                rendered = {}
                if batch:
                    cond = pe(chunks[batch].cuda().half())
                    predicted = unet.model(latents[[indices[i] for i in batch]].cuda().half(), torch.tensor([0], device='cuda'), encoder_hidden_states=cond).sample
                    for i, face in zip(batch, vae.decode_latents(predicted)):
                        box = project_box(coords[speaker][indices[i]], (source_width, source_height), (width, height))
                        x1, y1, x2, y2 = box
                        face = cv2.resize(face, (x2-x1, y2-y1), interpolation=cv2.INTER_LANCZOS4)
                        canvas = cv2.resize(frames[indices[i]], (width, height), interpolation=cv2.INTER_LANCZOS4)
                        rendered[i] = get_image(canvas, face, box, fp=parser, mode='jaw')
                for i in target:
                    frame = rendered[i] if i in rendered else cv2.resize(frames[indices[i]], (width, height), interpolation=cv2.INTER_LANCZOS4)
                    process.stdin.write(frame.tobytes())
                if first % 200 == 0: print(f'{speaker} {first}/{count}', flush=True)
        finally:
            process.stdin.close()
            code = process.wait()
        if code: raise RuntimeError('Role encoding failed')
        stats[speaker] = {'mouthFrames': sum(active), 'listeningFrames': count-sum(active)}
    width, height = map(int, request.get('resolution', '1920x1080').split('x'))
    fps = request.get('fps', 25)
    cameras = request.get('cameraPlan') or [{'start':0,'end':duration,'crop':None}]
    filters = ['[0:v][1:v]hstack=inputs=2[base]']
    filters.append('[base]split='+str(len(cameras))+''.join(f'[in{i}]' for i in range(len(cameras)))) if len(cameras)>1 else None
    for i, shot in enumerate(cameras):
        box=shot.get('crop')
        crop=f"crop=iw*{box['width']}:ih*{box['height']}:iw*{box['x']}:ih*{box['y']}," if box else ''
        source=f'in{i}' if len(cameras)>1 else 'base'
        filters.append(f'[{source}]trim=start_frame={round(shot["start"]*25)}:end_frame={round(shot["end"]*25)},setpts=PTS-STARTPTS,'+crop+
            f'scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},'+
            (quality['filter']+',' if quality['filter'] else '')+f'setsar=1[shot{i}]')
    filters.append(''.join(f'[shot{i}]' for i in range(len(cameras)))+f'concat=n={len(cameras)}:v=1:a=0,fps={fps}[v]')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(out/'role_A.mp4'), '-i', str(out/'role_B.mp4'), '-i', request['master'],
        '-filter_complex', ';'.join(filters),
        '-map', '[v]', '-map', '2:a:0', '-t', str(duration), '-c:v', 'libx264', '-preset', 'fast', '-crf', str(quality['crf']),
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(out/'final.mp4')], check=True)
    metrics = {'wallSeconds': time.monotonic()-started, 'durationSec': duration, 'frames': count, 'roles': stats,
        'fastQuality': quality_name, 'sourceSizes': source_sizes, 'compositing': 'deferred-scale-v2',
        'peakCudaGB': torch.cuda.max_memory_allocated()/1e9, 'motionSource': 'approved template v1', 'engine': 'MuseTalk 1.5'}
    (out/'metrics.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')
    for speaker in ('A','B'): (out/f'role_{speaker}.mp4').unlink()
    print(json.dumps(metrics), flush=True)

if __name__ == '__main__':
    run(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
