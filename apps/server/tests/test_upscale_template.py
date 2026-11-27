"""CPU-only guards for safe template upscaling; no Real-ESRGAN/GPU required."""
import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3] / 'scripts/fast-mode/upscale_template.py'
spec = importlib.util.spec_from_file_location('upscale_template_test', SOURCE)
upscale = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upscale)


def test_rerun_clears_stale_generated_frames_but_refuses_unknown_work(tmp_path):
    stale = tmp_path/'frames_A/f9999.png'
    stale.parent.mkdir()
    stale.write_bytes(b'old longer template')
    (tmp_path/'sr_A').mkdir()
    assert upscale.reset_work(tmp_path) == tmp_path
    assert not stale.exists() and not (tmp_path/'sr_A').exists()
    (tmp_path/'keep.txt').write_text('user data', encoding='utf-8')
    with pytest.raises(ValueError, match='全新目录'):
        upscale.reset_work(tmp_path)
    assert (tmp_path/'keep.txt').read_text(encoding='utf-8') == 'user data'


def test_stabilize_rejects_missing_or_extra_sr_frames(tmp_path):
    pytest.importorskip('cv2')
    frames, sr = tmp_path/'frames', tmp_path/'sr'
    frames.mkdir(); sr.mkdir()
    (frames/'f0001.png').write_bytes(b'fixture')
    with pytest.raises(ValueError, match='不一致'):
        upscale.stabilize(frames, sr, tmp_path/'stable', 'A', 32, 32)
    (sr/'f0001.png').write_bytes(b'fixture')
    (sr/'f0002.png').write_bytes(b'old')
    with pytest.raises(ValueError, match='不一致'):
        upscale.stabilize(frames, sr, tmp_path/'stable', 'A', 32, 32)
