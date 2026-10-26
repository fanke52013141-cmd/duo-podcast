"""Content-keyed role features, independent of cameras and export encoding."""
import hashlib
import json
import os
import pickle
import uuid
import wave
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def audio_identity(path, sample_rate, sample_count):
    digest = hashlib.sha256()
    with wave.open(str(path), 'rb') as audio:
        if (audio.getframerate(), audio.getnframes(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (
                sample_rate, sample_count, 1, 2, 'NONE'):
            raise ValueError('Role track does not match authoritative PCM16 sample clock')
        remaining = sample_count
        while remaining:
            frames = min(remaining, 65536)
            block = audio.readframes(frames)
            if len(block) != frames * 2:
                raise ValueError('Role audio is truncated')
            digest.update(block)
            remaining -= frames
    return {'pcmHash': digest.hexdigest(), 'sampleRate': sample_rate, 'sampleCount': sample_count}


def model_identity(root):
    root = Path(root)
    files = ['models/whisper/config.json', 'models/whisper/preprocessor_config.json',
             'models/whisper/pytorch_model.bin', 'vendor/MuseTalk/musetalk/utils/audio_processor.py']
    packages = {}
    for name in ('torch', 'transformers', 'librosa', 'numpy', 'einops', 'soxr', 'scipy', 'soundfile'):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = 'unavailable'
    return {'files': {name: hash_file(root / name) for name in files}, 'packages': packages}


def feature_key(audio, model, frames):
    return hashlib.sha256(json.dumps({'audio': audio, 'model': model, 'frames': frames,
        'fps': 25, 'dtype': 'float16', 'recipe': 'whisper-chunks-pad-last-v1'},
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def valid_features(tensor, frames, torch):
    return (isinstance(tensor, torch.Tensor) and tuple(tensor.shape) == (frames, 50, 384)
            and tensor.dtype in (torch.float16, torch.float32)
            and bool(torch.isfinite(tensor).all()))


def load_features(path, key, frames, torch):
    """Return explicit miss/invalid reasons; corrupt cache never enters inference."""
    if not path.is_file():
        return None, 'miss'
    try:
        if path.stat().st_size > frames * 50 * 384 * 4 + 1024 * 1024:
            return None, 'invalid'
        record = torch.load(path, map_location='cpu', weights_only=True)
        if (isinstance(record, dict) and record.get('cacheKey') == key
                and valid_features(record.get('features'), frames, torch)):
            return record['features'], 'hit'
    except (OSError, ValueError, RuntimeError, EOFError, TypeError, pickle.UnpicklingError):
        pass
    return None, 'invalid'


def save_features(path, key, tensor, frames, torch):
    if not valid_features(tensor, frames, torch):
        raise ValueError('Incomplete or nonfinite role audio features')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        torch.save({'cacheKey': key, 'features': tensor.cpu()}, temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
