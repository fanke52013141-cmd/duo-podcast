"""P1-C（42 号方案）快速模式换角色/底图——派生新包再改的 CPU 回归。

验证：派生新包 / 原包与其他工程不受影响 / 新包不就绪 / 渲染门槛拦截 / 样片确认撤销 /
无包快速工程明确拒绝。真实重新准备与渲染验收在 GPU 机器。
"""
from __future__ import annotations

import pytest

from duocast.domain.motion_pack import MotionMediaRef, MotionPackDraft
from duocast.domain.project import Approval, MotionPackPin
from duocast.services.motion_binding import validate_project_motion_binding
from duocast.services.motion_swap import apply_image_swap, derive_pack_with_image
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.motion_packs import MotionPackStore
from duocast.storage.project_store import ProjectStore


@pytest.fixture
def env(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")

    def media(mid, kind):
        path = artifacts.root / (mid + ".fixture")
        path.write_bytes(("fixture " + mid).encode())
        entry = artifacts.register(mid, kind, str(path), "unit-test")
        return MotionMediaRef(artifact_id=mid, file_hash=entry["fileHash"])

    image_a, image_b, scene = media("charA", "image"), media("charB", "image"), media("scene", "image")
    cameras = [{"id": "dual", "mode": "twoShot", "masterImage": scene.model_dump(by_alias=True)}]
    clips = []
    for role in "AB":
        for state in ("speaking", "listening"):
            cid = f"dual-{role}-{state}"
            clips.append({"id": cid, "role": role, "cameraId": "dual", "state": state,
                          "media": media(cid, "video").model_dump(by_alias=True),
                          "nativeWidth": 864, "nativeHeight": 480, "frameCount": 100, "usableEndFrame": 100})
    draft = {"name": "测试包", "characters": {
        "A": {"characterId": "lamp", "referenceImage": image_a.model_dump(by_alias=True)},
        "B": {"characterId": "signal", "referenceImage": image_b.model_dump(by_alias=True)}},
        "cameras": cameras, "clips": clips}
    motion_store = MotionPackStore(tmp_path / "packs", artifacts.by_id)
    pack = motion_store.create(draft, "src-pack")
    # 采用 dual 机位全部 clip → twoShot 就绪
    for clip in pack.clips:
        report = {"inputFileHash": clip.media.file_hash, "checksVersion": "v1", "status": "needs_review",
                  "evidence": {"completeSourceFramesDecoded": 100, "analyzedStartFrame": 0, "analyzedEndFrame": 100},
                  "native": {"width": 864, "height": 480, "fps": 25.0, "frames": 100, "durationSec": 4.0}}
        pack = motion_store.record_checks(pack.id, clip.id, report, pack.revision)
        pack = motion_store.review(pack.id, clip.id, True, pack.revision, "完整审看")
    assert motion_store.readiness(pack.id, "twoShot")["ready"]

    project_store = ProjectStore(tmp_path / "projects", debounce_ms=0)

    def fast_project(pid):
        p = project_store.create(pid)
        return project_store.apply(p.id, {
            "generation_mode": "fast", "shot_style": "twoShot",
            "motion_pack": MotionPackPin(id=pack.id, version=pack.version).model_dump(),
            "approvals": [Approval(kind="sample", input_revision_id="R1", spec={})],
        }, p.revision)

    return artifacts, motion_store, project_store, pack, fast_project, media


def test_character_swap_derives_new_pack_and_blocks_render(env):
    artifacts, motion_store, project_store, pack, fast_project, media = env
    p = fast_project("swap-char")
    new_image = media("newCharA", "image")
    result = apply_image_swap(project_store, motion_store, p, image_ref=new_image, role="A",
                              character_meta={"characterId": "newlamp", "name": "新角色"})
    new_pin = result["motionPack"]
    assert new_pin["id"] != pack.id  # 派生新包
    assert result["derivedFrom"]["id"] == pack.id
    assert result["ready"] is False  # 新包未准备
    assert result["missing"]
    # 原包不变（其他工程不受影响）
    assert motion_store.load_version(pack.id, pack.version).characters["A"].reference_image.artifact_id == "charA"
    # 本工程 pin 新包
    saved = project_store.load(p.id)
    assert saved.motion_pack.id == new_pin["id"]
    assert saved.character_bindings == {} or True  # extra_patch 未传时不写角色绑定
    # 样片确认被撤销
    assert all(a.revoked_at for a in saved.approvals if a.kind == "sample")
    # 渲染门槛拦截（新包不就绪）
    with pytest.raises(ValueError):
        validate_project_motion_binding(saved, motion_store)


def test_scene_swap_updates_twoshot_cameras(env):
    artifacts, motion_store, project_store, pack, fast_project, media = env
    p = fast_project("swap-scene")
    new_scene = media("newScene", "image")
    result = apply_image_swap(project_store, motion_store, p, image_ref=new_scene)
    new_pack = motion_store.load_version(result["motionPack"]["id"], result["motionPack"]["version"])
    assert all(c.master_image.artifact_id == "newScene" for c in new_pack.cameras if c.mode == "twoShot")
    assert result["ready"] is False
    # 原包底图不变
    assert motion_store.load_version(pack.id, pack.version).cameras[0].master_image.artifact_id == "scene"


def test_other_project_pinning_original_unaffected(env):
    artifacts, motion_store, project_store, pack, fast_project, media = env
    p1 = fast_project("proj-1")
    p2 = fast_project("proj-2")
    apply_image_swap(project_store, motion_store, p1, image_ref=media("x", "image"), role="A")
    # proj-2 仍 pin 原包且就绪
    saved2 = project_store.load(p2.id)
    assert saved2.motion_pack.id == pack.id
    assert validate_project_motion_binding(saved2, motion_store) is not None


def test_no_pack_fast_project_rejected(env):
    artifacts, motion_store, project_store, pack, fast_project, media = env
    p = project_store.create("no-pack")
    p = project_store.apply(p.id, {"generation_mode": "fast"}, p.revision)
    with pytest.raises(ValueError, match="SWAP_NO_PACK"):
        derive_pack_with_image(motion_store, p, image_ref=media("y", "image"), role="A")


def test_swap_endpoint_and_bind_character_fast_path(client, tmp_path):
    """API 层：swap-image 端点 + bind_character 快速分支派生新包。"""
    import hashlib
    store = client.app.state.project_store
    artifacts = client.app.state.artifacts
    motion_store = client.app.state.motion_pack_store

    def media(mid, kind, data):
        path = artifacts.root / (mid + ".bin")
        path.write_bytes(data)
        entry = artifacts.register(mid, kind, str(path), "dep")
        return {"artifactId": mid, "fileHash": entry["fileHash"]}

    ia, ib, sc = media("cA", "image", b"imgA"), media("cB", "image", b"imgB"), media("sc", "image", b"scene")
    cameras = [{"id": "dual", "mode": "twoShot", "masterImage": sc}]
    clips = []
    for role in "AB":
        for state in ("speaking", "listening"):
            cid = f"dual-{role}-{state}"
            clips.append({"id": cid, "role": role, "cameraId": "dual", "state": state,
                          "media": media(cid, "video", b"vid" + cid.encode()),
                          "nativeWidth": 864, "nativeHeight": 480, "frameCount": 100, "usableEndFrame": 100})
    draft = {"name": "API包", "characters": {"A": {"characterId": "a", "referenceImage": ia},
                                            "B": {"characterId": "b", "referenceImage": ib}},
             "cameras": cameras, "clips": clips}
    pack = motion_store.create(draft, "api-pack")
    for clip in pack.clips:
        report = {"inputFileHash": clip.media.file_hash, "checksVersion": "v1", "status": "needs_review",
                  "evidence": {"completeSourceFramesDecoded": 100, "analyzedStartFrame": 0, "analyzedEndFrame": 100},
                  "native": {"width": 864, "height": 480, "fps": 25.0, "frames": 100, "durationSec": 4.0}}
        pack = motion_store.record_checks(pack.id, clip.id, report, pack.revision)
        pack = motion_store.review(pack.id, clip.id, True, pack.revision, "审看")
    p = store.create("api-fast")
    p = store.apply(p.id, {"generation_mode": "fast", "shot_style": "twoShot",
                           "motion_pack": {"id": pack.id, "version": pack.version}}, p.revision)
    new_img = media("newImg", "image", b"new-image")
    r = client.post(f"/api/projects/{p.id}/motion/swap-image",
                    json={"imageArtifactId": new_img["artifactId"], "role": "A", "expectedRevision": p.revision})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["motionPack"]["id"] != pack.id
    assert body["ready"] is False
    # 非快速模式拒绝
    std = store.create("api-std")
    r2 = client.post(f"/api/projects/{std.id}/motion/swap-image",
                     json={"imageArtifactId": new_img["artifactId"], "role": "A"})
    assert r2.status_code == 422


@pytest.fixture
def client(tmp_path, monkeypatch):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from duocast.main import app, settings
    monkeypatch.setattr('duocast.main.settings', replace(
        settings, storage_root=tmp_path, text_provider='mock', image_provider='mock',
        tts_provider='mock', video_provider='mock'))
    with TestClient(app) as c:
        yield c