import importlib.util
from pathlib import Path
import wave

import pytest

MODULE = Path(__file__).parents[3] / 'scripts/fast-mode/motion_audio_cache.py'
spec = importlib.util.spec_from_file_location('motion_audio_cache_test', MODULE)
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


def audio(path, pcm, rate=48000):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(pcm)


def test_actual_pcm_change_invalidates_feature_cache(tmp_path):
    path = tmp_path / 'role.wav'
    audio(path, b'\x01\x00' * 48000)
    first = cache.audio_identity(path, 48000, 48000)
    original = cache.feature_key(first, {'model': 'v1'}, 25)
    # No camera or encoding enters the key; only causal feature dependencies.
    assert original == cache.feature_key(dict(first), {'model': 'v1'}, 25)
    audio(path, b'\x02\x00' * 48000)
    assert original != cache.feature_key(cache.audio_identity(path, 48000, 48000), {'model': 'v1'}, 25)
    assert original != cache.feature_key(first, {'model': 'v2'}, 25)
    assert original != cache.feature_key(first, {'model': 'v1'}, 26)


def test_wrong_clock_or_truncated_audio_rejected(tmp_path):
    path = tmp_path / 'role.wav'
    audio(path, b'\x01\x00' * 48000)
    with pytest.raises(ValueError):
        cache.audio_identity(path, 16000, 48000)
    with pytest.raises(ValueError):
        cache.audio_identity(path, 48000, 48001)
    path.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(ValueError, match='truncated'):
        cache.audio_identity(path, 48000, 48000)


def test_model_and_preprocessing_code_changes_invalidate(tmp_path):
    names = ['models/whisper/config.json', 'models/whisper/preprocessor_config.json',
             'models/whisper/pytorch_model.bin', 'vendor/MuseTalk/musetalk/utils/audio_processor.py']
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'v1')
    before = cache.model_identity(tmp_path)
    (tmp_path / names[-1]).write_bytes(b'v2')
    assert before != cache.model_identity(tmp_path)


def test_tensor_cache_roundtrip_and_corruption(tmp_path):
    torch = pytest.importorskip('torch')
    path = tmp_path / 'cache.pt'
    tensor = torch.zeros((25, 50, 384), dtype=torch.float16)
    assert cache.load_features(path, 'key', 25, torch) == (None, 'miss')
    cache.save_features(path, 'key', tensor, 25, torch)
    loaded, status = cache.load_features(path, 'key', 25, torch)
    assert status == 'hit' and torch.equal(loaded, tensor)
    assert cache.load_features(path, 'other', 25, torch) == (None, 'invalid')
    path.write_bytes(b'corrupt')
    assert cache.load_features(path, 'key', 25, torch) == (None, 'invalid')
    tensor[0, 0, 0] = float('nan')
    with pytest.raises(ValueError):
        cache.save_features(path, 'key', tensor, 25, torch)
