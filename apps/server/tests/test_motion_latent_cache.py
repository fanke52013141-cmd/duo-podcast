"""Seeded posterior-cache contract; GPU test requires the actual installed VAE."""
import importlib.util
import os
from pathlib import Path
import sys

import pytest

MODULE = Path(__file__).parents[3] / "scripts/fast-mode/motion_worker.py"
sys.path.insert(0, str(MODULE.parent))
spec = importlib.util.spec_from_file_location("motion_latent_worker_test", MODULE)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def test_latent_key_binds_seed_code_and_source_and_frame_seed_is_order_independent():
    identity = {"config": "config-hash", "weights": "weights-hash", "implementation": "vae-code-hash"}
    base = worker.latent_key("a"*64, [[0, 0, 20, 20]], identity, 42)
    assert base == worker.latent_key("a"*64, [[0, 0, 20, 20]], dict(reversed(list(identity.items()))), 42)
    assert base != worker.latent_key("a"*64, [[0, 0, 20, 20]], identity, 43)
    assert base != worker.latent_key("a"*64, [[0, 0, 20, 20]], {**identity, "implementation": "new-code"}, 42)
    assert base != worker.latent_key("b"*64, [[0, 0, 20, 20]], identity, 42)
    assert base != worker.latent_key("a"*64, [[0, 0, 21, 20]], identity, 42)
    forward = {i: worker.latent_frame_seed(base, i) for i in range(3)}
    reverse = {i: worker.latent_frame_seed(base, i) for i in reversed(range(3))}
    assert forward == reverse and len(set(forward.values())) == 3
    with pytest.raises(ValueError):
        worker.latent_key("a"*64, [], identity, True)


def test_corrupt_and_invalid_tensor_cache_is_explicit_miss(tmp_path):
    torch = pytest.importorskip("torch", reason="No fake tensors stand in for cache validation")
    target = tmp_path / "latents.pt"
    assert worker.load_latents(target, "key", 2, torch) == ({}, "miss")
    target.write_bytes(b"not a torch file")
    assert worker.load_latents(target, "key", 2, torch) == ({}, "invalid")
    for tensor in (torch.zeros((1, 4, 32, 32)), torch.full((1, 8, 32, 32), float("nan"))):
        torch.save({"cacheKey": "key", "latents": {0: tensor}}, target)
        assert worker.load_latents(target, "key", 2, torch) == ({}, "invalid")


def test_actual_gpu_vae_cold_warm_partial_order_and_seed(tmp_path):
    torch = pytest.importorskip("torch", reason="Actual CUDA VAE required")
    np = pytest.importorskip("numpy")
    if not torch.cuda.is_available():
        pytest.skip("Actual CUDA GPU required")
    configured = os.environ.get("DUOCAST_FAST_ENGINE")
    if not configured:
        pytest.skip("Set DUOCAST_FAST_ENGINE to installed runtime with actual SD-VAE weights")
    root = Path(configured).resolve()
    weights = root / "models/sd-vae/diffusion_pytorch_model.bin"
    if not weights.is_file():
        pytest.skip("Actual installed VAE weights missing")
    os.environ["HF_HUB_OFFLINE"] = "1"
    sys.path.insert(0, str(root / "vendor/MuseTalk"))
    from musetalk.models.vae import VAE
    vae = VAE(str(root / "models/sd-vae"), use_float16=True)
    crop = np.arange(256*256*3, dtype=np.uint8).reshape(256, 256, 3)
    identity = {"config": worker.file_hash(root / "models/sd-vae/config.json"),
                "weights": worker.file_hash(weights),
                "implementation": worker.file_hash(root / "vendor/MuseTalk/musetalk/models/vae.py")}
    key = worker.latent_key("a"*64, [[0, 0, 256, 256]]*2, identity, 42)
    torch.manual_seed(777)
    before_cpu = torch.random.get_rng_state().clone()
    before_cuda = torch.cuda.get_rng_state().clone()
    with torch.inference_mode():
        cold = {i: worker.encode_seeded_latent(vae, crop, torch, key, i) for i in (0, 1)}
        assert torch.equal(torch.random.get_rng_state(), before_cpu)
        assert torch.equal(torch.cuda.get_rng_state(), before_cuda)
        reverse = {i: worker.encode_seeded_latent(vae, crop, torch, key, i) for i in (1, 0)}
        assert all(torch.equal(cold[i], reverse[i]) for i in cold)
        path = tmp_path / "partial.pt"
        torch.save({"cacheKey": key, "latents": {1: cold[1]}}, path)
        warm, status = worker.load_latents(path, key, 2, torch)
        assert status == "hit" and set(warm) == {1}
        warm[0] = worker.encode_seeded_latent(vae, crop, torch, key, 0)
        assert all(torch.equal(cold[i], warm[i]) for i in cold)
        path.write_bytes(b"corrupted")
        assert worker.load_latents(path, key, 2, torch) == ({}, "invalid")
        repaired = worker.encode_seeded_latent(vae, crop, torch, key, 0)
        assert torch.equal(cold[0], repaired)
        other_key = worker.latent_key("a"*64, [[0, 0, 256, 256]]*2, identity, 43)
        changed = worker.encode_seeded_latent(vae, crop, torch, other_key, 0)
        assert not torch.equal(cold[0], changed)
