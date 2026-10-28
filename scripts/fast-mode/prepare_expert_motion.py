"""CPU-only owned reference preparation. No model inference or network access."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from motion_worker import ClipReader, compose_frame, file_hash, validate_request


def write_png(path, image, cv2):
    """Use Python's Unicode-aware filesystem API on Windows."""
    ok, encoded = cv2.imencode('.png', image)
    if not ok:
        raise RuntimeError('Cannot encode expert reference image')
    Path(path).write_bytes(bytes(encoded))


def _write_audio(source, target, rate, total_samples, piece):
    import wave
    with wave.open(str(source), 'rb') as audio:
        if (audio.getframerate(), audio.getnframes(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (
                rate, total_samples, 1, 2, 'NONE'):
            raise ValueError('Expert input audio differs from authoritative PCM16 sample clock')
        start, end = piece['startSample'], piece['endSample']
        audio.setpos(start)
        pcm = audio.readframes(end - start)
        if len(pcm) != (end - start) * 2:
            raise ValueError('Expert source PCM is truncated')
    with wave.open(str(target), 'wb') as audio:
        audio.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * piece['prefixSilentSamples'] + pcm + b'\0\0' * piece['suffixSilentSamples'])


def prepare(request, request_hash):
    import cv2
    import numpy as np
    from collections import OrderedDict

    expanded, geometry = validate_request(request)
    assets, plan = request['motionAssets'], request['expertPlan']
    if (plan['packId'], plan['packVersion'], plan['sampleRate'], plan['totalSamples'], plan['totalFrames']) != (
            assets['packId'], assets['packVersion'], request['sampleRate'], request['sampleCount'], assets['plan']['totalFrames']):
        raise ValueError('Expert preparation is not bound to this motion/audio snapshot')
    output = Path(request['outputDir'])
    if not output.is_absolute() or output.resolve() != output:
        raise ValueError('Expert output directory must be an owned absolute directory')
    output.mkdir(parents=True, exist_ok=True)
    clips = assets['clips']
    used = {item['clipId'] for segment in plan['segments'] for source in segment['sourceByRole'].values() for item in source}
    for clip_id in used:
        if file_hash(clips[clip_id]['path']) != clips[clip_id]['fileHash']:
            raise ValueError('Expert source video hash changed')
    audio_hashes = {name: file_hash(path) for name, path in {'master': request['master'], **request['tracks']}.items()}
    cache = {'frames': OrderedDict(), 'bytes': 0}
    readers, prepared = {}, []
    try:
        readers = {id: ClipReader(clips[id], cv2, cache) for id in used}
        for segment in plan['segments']:
            camera = segment['cameraId']
            roles = segment['geometry']['visibleRoles']
            native = geometry[camera]['native']
            width, height = native[:2]
            key = segment['dependencyHash']
            if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                raise ValueError('Invalid frozen expert segment key')
            prefix = f'expert_{request_hash[:24]}_{key[:24]}'
            names = {'anchorImage': prefix + '_anchor.png', 'referenceVideo': prefix + '_reference.mp4',
                     'masterAudio': prefix + '_mix.wav',
                     'roleAudio': {role: prefix + f'_{role}.wav' for role in roles},
                     'roleMasks': {role: prefix + f'_mask_{role}.png' for role in roles}}
            folder = output / key
            folder.mkdir(exist_ok=True)
            flattened = {'anchorImage': names['anchorImage'], 'referenceVideo': names['referenceVideo'],
                         'masterAudio': names['masterAudio'],
                         **{'audio' + role: name for role, name in names['roleAudio'].items()},
                         **{'mask' + role: name for role, name in names['roleMasks'].items()}}
            if (set(roles) != set(expanded[camera]) or list(segment['geometry']['native']) != list(native)
                    or {r: tuple(v) for r, v in segment['geometry']['bodyBoxes'].items()} != geometry[camera]['bodyBoxes']
                    or len(segment['referenceOutputFrames']) != segment['modelFrames']):
                raise ValueError('Expert prepared geometry/roles differ from adopted source')
            _write_audio(request['master'], folder / names['masterAudio'], request['sampleRate'], request['sampleCount'], segment['audioSlice'])
            for role in roles:
                _write_audio(request['tracks'][role], folder / names['roleAudio'][role], request['sampleRate'], request['sampleCount'], segment['audioSlice'])
                mask = np.zeros((height, width, 3), dtype=np.uint8)
                x1, y1, x2, y2 = segment['geometry']['bodyBoxes'][role]
                mask[y1:y2, x1:x2] = 255
                write_png(folder / names['roleMasks'][role], mask, cv2)
            target = folder / names['referenceVideo']
            partial = target.with_suffix('.partial.mp4')
            process = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
                '-s', f'{width}x{height}', '-r', '25', '-i', 'pipe:0', '-f', 'lavfi',
                '-i', f'anullsrc=r={request["sampleRate"]}:cl=mono', '-map', '0:v:0', '-map', '1:a:0',
                '-frames:v', str(segment['modelFrames']), '-t', str(segment['modelFrames'] / 25),
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '19', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-shortest', str(partial)], stdin=subprocess.PIPE)
            try:
                for index, output_frame in enumerate(segment['referenceOutputFrames']):
                    frames = {}
                    for role in roles:
                        source = segment['sourceByRole'][role][index]
                        expected = expanded[camera][role][output_frame]
                        clip = clips[source['clipId']]
                        if ((source['clipId'], source['sourceFrame']) != expected[:2]
                                or source['videoFileHash'] != clip['fileHash']
                                or source['faceBox'] != clip['coordinates'][source['sourceFrame']]
                                or source['state'] != clip['state']):
                            raise ValueError('Expert reference frame changed from adopted plan')
                        frames[role] = readers[source['clipId']].get(source['sourceFrame'])
                    frame = compose_frame(frames, geometry[camera]['bodyBoxes']) if len(roles) == 2 else frames[roles[0]]
                    if index == 0:
                        write_png(folder / names['anchorImage'], frame, cv2)
                    process.stdin.write(frame.tobytes())
            finally:
                process.stdin.close()
                code = process.wait()
            if code:
                raise RuntimeError('Expert reference encoding failed')
            partial.replace(target)
            files = {kind: {'path': str(folder / name), 'fileHash': file_hash(folder / name)} for kind, name in flattened.items()}
            item={'segmentDependencyHash': key, 'cameraId': camera, 'inputNames': names, 'files': files,
                             'native': {'width': width, 'height': height, 'frames': segment['modelFrames'], 'fps': 25},
                             'sourceTrace': {'packId': assets['packId'], 'packVersion': assets['packVersion'],
                                             'visibleRoles': roles, 'sourceByRole': segment['sourceByRole'],
                                             'referenceOutputFrames': segment['referenceOutputFrames'],
                                             'referenceAudio': 'embedded_silence'}}
            if request.get('roiCrop'):
                from expert_roi_media import crop_prepared
                item=crop_prepared(item,request['roiCrop'])
            prepared.append(item)
        for id in used:
            if file_hash(clips[id]['path']) != clips[id]['fileHash']:
                raise ValueError('Expert source video changed while preparing')
        for name, path in {'master': request['master'], **request['tracks']}.items():
            if file_hash(path) != audio_hashes[name]:
                raise ValueError('Expert source PCM changed while preparing')
        result = {'formatVersion': 1, 'requestFileHash': request_hash, 'preparedSegments': prepared,
                  'modelInferencePerformed': False, 'automaticAdoption': False}
        manifest = output / 'prepared_segments.json'
        temporary = output / 'prepared_segments.partial.json'
        temporary.write_text(json.dumps(result, indent=2), encoding='utf-8')
        temporary.replace(manifest)
        return result
    finally:
        for reader in readers.values():
            reader.close()


if __name__ == '__main__':
    source = Path(sys.argv[1]).resolve()
    if source.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Expert preparation request exceeds limit')
    raw = source.read_bytes()
    prepare(json.loads(raw), hashlib.sha256(raw).hexdigest())
