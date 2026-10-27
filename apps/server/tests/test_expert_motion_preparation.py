"""Actual CPU helper smoke, opt-in to installed cv2/numpy runtime; no model use."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import wave

import pytest

from duocast.services.expert_motion import build_expert_motion_plan
from test_expert_motion import assets_and_edits


def test_png_write_uses_unicode_aware_filesystem(tmp_path):
    """Python writes the encoded buffer instead of passing a Windows path to cv2.imwrite."""
    import importlib.util
    import sys
    from types import SimpleNamespace

    scripts = Path(__file__).resolve().parents[3] / 'scripts/fast-mode'
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location('expert_unicode_png', scripts/'prepare_expert_motion.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path/'中文目录'/'参考图.png'
    path.parent.mkdir()
    fake_cv2 = SimpleNamespace(imencode=lambda extension, image: (True, bytearray(b'encoded png')))
    module.write_png(path, object(), fake_cv2)
    assert path.read_bytes() == b'encoded png'
    with pytest.raises(RuntimeError, match='encode'):
        module.write_png(path, object(), SimpleNamespace(imencode=lambda *args: (False, None)))


def test_real_cpu_reference_composition_source_frames_audio_and_subject_masks(tmp_path):
    root = os.environ.get('DUOCAST_FAST_ENGINE')
    if not root:
        pytest.skip('Set DUOCAST_FAST_ENGINE to a cv2/numpy runtime; no fake pixel evidence')
    python = Path(root) / 'venv/Scripts/python.exe'
    if not python.is_file():
        pytest.skip('Actual installed CPU preparation Python unavailable')
    assets, edits, samples = assets_and_edits(frames=10, rate=16000)
    # Only the actual selected full-scene camera is prepared.
    assets['plan']['cameras'] = assets['plan']['cameras'][:1]
    assets['cameras'] = {'dual': assets['cameras']['dual']}
    assets['clips'] = {id: clip for id, clip in assets['clips'].items() if clip['cameraId'] == 'dual'}
    for role, colors in [('A', ('red', 'yellow')), ('B', ('blue', 'magenta'))]:
        source = tmp_path / f'{role}.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=c={colors[0]}:s=100x60:r=25:d=0.2',
            '-f', 'lavfi', '-i', f'color=c={colors[1]}:s=100x60:r=25:d=0.2',
            '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]',
            '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)],
            check=True, capture_output=True, timeout=20)
        for id, clip in assets['clips'].items():
            if clip['role'] == role:
                clip.update(path=str(source), fileHash=hashlib.sha256(source.read_bytes()).hexdigest(),
                            frames=10, usableEndFrame=10, coordinates=clip['coordinates'][:10])
                for segment in assets['plan']['cameras'][0]['roles'][role]:
                    segment['fileHash'] = clip['fileHash']
    plan = build_expert_motion_plan(assets, edits, sample_rate=16000, total_samples=samples)
    pcm = b'\x10\x00' * samples
    tracks = {}
    for role in ('master', 'A', 'B'):
        path = tmp_path / f'{role}.wav'
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(pcm if role != 'B' else b'\0\0' * samples)
        tracks[role] = str(path)
    request = {'motionAssets': assets, 'expertPlan': plan, 'cameraEditPlan': edits,
               'sampleRate': 16000, 'sampleCount': samples, 'master': tracks['master'],
               'tracks': {r: tracks[r] for r in 'AB'}, 'outputDir': str(tmp_path / 'prepared')}
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(request), encoding='utf-8')
    helper = Path(__file__).parents[3] / 'scripts/fast-mode/prepare_expert_motion.py'
    subprocess.run([str(python), '-B', str(helper), str(path)], check=True, capture_output=True, timeout=60)
    manifest = json.loads((tmp_path / 'prepared/prepared_segments.json').read_text(encoding='utf-8'))
    assert manifest['requestFileHash'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest['modelInferencePerformed'] is False and manifest['automaticAdoption'] is False
    item = manifest['preparedSegments'][0]
    for entry in item['files'].values():
        assert hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest() == entry['fileHash']
    output = Path(item['files']['referenceVideo']['path'])
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(output), '-map', '0:v:0',
                          '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], check=True, capture_output=True, timeout=20).stdout
    assert len(raw) == plan['segments'][0]['modelFrames'] * 100 * 60 * 3
    def pixel(frame, x, y):
        start = ((frame * 60 + y) * 100 + x) * 3
        return list(raw[start:start+3])
    # Warm-up padding is trimmed later. Visible source 0 starts at frame 8.
    left, right = pixel(8, 25, 30), pixel(8, 75, 30)
    assert left[0] > 200 and left[1] < 30 and right[2] > 200 and right[0] < 30
    left, right = pixel(17, 25, 30), pixel(17, 75, 30)
    assert left[0] > 200 and left[1] > 200 and right[0] > 200 and right[2] > 200
    audio_file = Path(item['files']['masterAudio']['path'])
    with wave.open(str(audio_file), 'rb') as stream:
        data = stream.readframes(stream.getnframes())
    piece = plan['segments'][0]['audioSlice']
    assert len(data) == piece['modelSampleCount'] * 2
    assert data[:piece['prefixSilentSamples']*2] == b'\0\0' * piece['prefixSilentSamples']
    assert data[piece['prefixSilentSamples']*2:(piece['prefixSilentSamples']+samples)*2] == pcm
