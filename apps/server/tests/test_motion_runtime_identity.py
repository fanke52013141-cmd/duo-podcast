import asyncio

from duocast.services import motion_runtime_identity


def test_freeze_uses_server_selected_root_and_measured_identity(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    root.mkdir()
    monkeypatch.setattr(motion_runtime_identity, 'require_motion_engine', lambda: root)
    monkeypatch.setattr(motion_runtime_identity, 'runtime_identity', lambda path: 'a' * 64 if path == root else None)
    frozen = asyncio.run(motion_runtime_identity.freeze_motion_runtime())
    assert frozen == {'root': str(root), 'identity': 'a' * 64, 'protocol': 'motion-runtime-v1'}
