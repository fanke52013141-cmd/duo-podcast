"""WP5 音频完整性：候选指纹、真实资产失败、母轨严格校验与参考音频哈希。"""

import asyncio
import hashlib
import os
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from duocast.adapters.comfyui_tts import ComfyUITTSProvider
from duocast.api import voice as voice_api
from duocast.domain.audio import AudioTimeline
from duocast.domain.project import Line, PronunciationEntry, ScriptRevision, Turn
from duocast.services import voice_svc
from duocast.services.voice_svc import (
    adopt_unit_candidate,
    effective_pronunciation,
    synthesize_timeline,
)
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.project_store import ProjectStore


class MockAssetTTS:
    capabilities = {"simulated": True}

    def __init__(self):
        self.calls = []
        self.index = 0

    async def synthesize(self, request):
        self.calls.append(request)
        self.index += 1
        return {
            "sampleRate": 48000,
            "sampleCount": 48000 + self.index,
            "audioAssetId": f"AUD-MOCK-{self.index}",
        }


class FileTTS:
    """返回真实 WAV 的测试适配器；可注入各类媒体损伤。"""

    capabilities = {"simulated": False}
    name = "file-tts-integrity"

    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        frames: int = 16,
        channels: int = 1,
        width: int = 2,
        rate: int = 48000,
        declared: int = 16,
        register: bool = True,
        tamper: bool = False,
        remove: bool = False,
    ):
        self.artifacts = artifacts
        self.frames = frames
        self.channels = channels
        self.width = width
        self.rate = rate
        self.declared = declared
        self.register = register
        self.tamper = tamper
        self.remove = remove
        self.calls = []

    async def synthesize(self, request):
        self.calls.append(request)
        import uuid

        asset_id = f"AUD-{uuid.uuid4().hex[:12]}"
        path = self.artifacts.root / "audio" / f"{asset_id}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(self.channels)
            writer.setsampwidth(self.width)
            writer.setframerate(self.rate)
            sample = (1).to_bytes(self.width, "little")
            writer.writeframes(sample * self.frames * self.channels)
        if self.register:
            self.artifacts.register(asset_id, "audio", str(path), f"dep-{asset_id}")
        if self.tamper:
            path.write_bytes(path.read_bytes() + b"tampered")
        if self.remove:
            path.unlink()
        return {
            "sampleRate": 48000,
            "sampleCount": self.declared,
            "audioAssetId": asset_id,
            "audioPath": str(path),
        }


class MissingAssetTTS:
    capabilities = {"simulated": False}
    name = "missing-asset-tts"

    async def synthesize(self, request):
        return {"sampleRate": 48000, "sampleCount": 16}


def fixture(tmp_path, *, pronunciation=None, turns=2):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    created = store.create("audio-integrity")
    speakers = ("A", "B")
    revision = ScriptRevision(id="R1", turns=[
        Turn(
            id=f"T-{index}",
            speaker=speakers[index % 2],
            lines=[Line(id=f"L-{index}", spoken_text=f"第{index}句 AI")],
        )
        for index in range(turns)
    ])
    project = store.apply(created.id, {
        "script_revisions": [revision],
        "current_draft_revision": revision.id,
        "pronunciation_dict": pronunciation or [],
    }, created.revision)
    return store, project, revision


def test_candidate_adoption_uses_effective_pronunciation(tmp_path):
    store, project, revision = fixture(
        tmp_path, pronunciation=[PronunciationEntry(term="AI", read="人工智能")])
    pronunciation = effective_pronunciation(
        project, [{"term": "AI", "read": "全局读法"}, {"term": "GPU", "read": "显卡"}])
    tts = MockAssetTTS()
    asyncio.run(synthesize_timeline(store, tts, project, revision, {}, pronunciation=pronunciation))
    current = store.load(project.id)
    candidate = asyncio.run(synthesize_timeline(
        store, tts, current, revision, {}, target_turn_ids=["T-0"],
        pronunciation=pronunciation,
    ))

    assert tts.calls[-1]["lineTexts"] == ["第0句 人工智能"]
    asset_id = candidate.units[0].candidate_audio_asset_ids[-1]
    adopted = adopt_unit_candidate(
        store, project.id, "U-T-0", asset_id, pronunciation=pronunciation)

    assert adopted.units[0].adopted_audio_asset_id == asset_id
    assert adopted.units[0].pronunciation_revision == candidate.units[0].pronunciation_revision
    assert adopted.master_audio_asset_id is None  # mock 无媒体流程保持可用


def test_changed_pronunciation_rejects_candidate_without_writeback(tmp_path):
    store, project, revision = fixture(
        tmp_path, pronunciation=[PronunciationEntry(term="AI", read="人工智能")])
    original = effective_pronunciation(project)
    tts = MockAssetTTS()
    asyncio.run(synthesize_timeline(store, tts, project, revision, {}, pronunciation=original))
    current = store.load(project.id)
    candidate = asyncio.run(synthesize_timeline(
        store, tts, current, revision, {}, target_turn_ids=["T-0"],
        pronunciation=original,
    ))
    asset_id = candidate.units[0].candidate_audio_asset_ids[-1]
    before = store.load(project.id)
    changed = store.apply(project.id, {
        "pronunciation_dict": [PronunciationEntry(term="AI", read="新读法")],
    }, before.revision)

    with pytest.raises(ValueError, match="CANDIDATE_NOT_FOUND"):
        adopt_unit_candidate(
            store, project.id, "U-T-0", asset_id,
            pronunciation=effective_pronunciation(changed),
        )

    assert store.load(project.id).model_dump() == changed.model_dump()


def test_adopt_api_passes_merged_pronunciation_without_changing_response(tmp_path, monkeypatch):
    store, project, _ = fixture(
        tmp_path, pronunciation=[PronunciationEntry(term="AI", read="工程读法")])
    captured = {}

    def fake_adopt(store_arg, project_id, unit_id, asset_id, **kwargs):
        captured.update(kwargs)
        return AudioTimeline(revision_id="R1")

    monkeypatch.setattr(voice_api, "adopt_unit_candidate", fake_adopt)
    machine = SimpleNamespace(get_global_pronunciation=lambda: [
        {"term": "AI", "read": "全局读法"}, {"term": "GPU", "read": "显卡"},
    ])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        project_store=store,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        machine=machine,
        runtime_settings=SimpleNamespace(tts_provider="mock"),
    )))

    response = asyncio.run(voice_api.adopt(
        project.id, "U-T-0", {"candidateAudioAssetId": "AUD-1"}, request))

    assert captured["pronunciation"] == [
        {"term": "AI", "read": "工程读法"}, {"term": "GPU", "read": "显卡"},
    ]
    assert captured["require_media"] is False
    assert set(response) == {"projectId", "unitId", "candidateAudioAssetId", "audioTimeline"}


def test_real_synthesis_missing_asset_fails_without_writeback(tmp_path):
    store, project, revision = fixture(tmp_path)
    asyncio.run(synthesize_timeline(store, MockAssetTTS(), project, revision, {}))
    before = store.load(project.id)
    artifacts = ArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="INVALID_AUDIO: 真实 TTS 未返回音频资产"):
        asyncio.run(synthesize_timeline(
            store, MissingAssetTTS(), before, revision, {},
            artifact_store=artifacts, artifacts_root=artifacts.root,
        ))

    assert store.load(project.id).model_dump() == before.model_dump()


@pytest.mark.parametrize("case", [
    "short", "long", "stereo", "wrong_rate", "wrong_width",
    "missing_manifest", "missing_file", "tampered",
])
def test_real_synthesis_rejects_bad_unit_media_without_writeback(tmp_path, case):
    store, project, revision = fixture(tmp_path, turns=1)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    options = {}
    if case == "short":
        options["frames"] = 15
    elif case == "long":
        options["frames"] = 17
    elif case == "stereo":
        options["channels"] = 2
    elif case == "wrong_rate":
        options["rate"] = 24000
    elif case == "wrong_width":
        options["width"] = 1
    elif case == "missing_manifest":
        options["register"] = False
    elif case == "missing_file":
        options["remove"] = True
    elif case == "tampered":
        options["tamper"] = True

    with pytest.raises(ValueError, match="INVALID_MASTER_INPUT"):
        asyncio.run(synthesize_timeline(
            store, FileTTS(artifacts, **options), project, revision, {},
            artifact_store=artifacts, artifacts_root=artifacts.root,
        ))

    saved = store.load(project.id)
    assert saved.audio_timeline is None
    assert saved.audio_history == []


def test_master_is_published_from_validated_partial_file(tmp_path, monkeypatch):
    store, project, revision = fixture(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    observed = []
    real_replace = os.replace

    def checked_replace(source, destination):
        source = Path(source)
        destination = Path(destination)
        if destination.name.startswith("master_"):
            assert source.suffix == ".partial"
            assert not destination.exists()
            observed.append((source, destination))
        return real_replace(source, destination)

    # voice_svc.os 与 artifacts.os 是同一个模块；只拦截 master 目标路径。
    monkeypatch.setattr(voice_svc.os, "replace", checked_replace)
    timeline = asyncio.run(synthesize_timeline(
        store, FileTTS(artifacts), project, revision, {},
        artifact_store=artifacts, artifacts_root=artifacts.root,
    ))

    assert observed
    entry = artifacts.by_id(timeline.master_audio_asset_id)
    master_path = Path(entry["path"])
    assert master_path.is_file()
    assert hashlib.sha256(master_path.read_bytes()).hexdigest() == entry["fileHash"]
    with wave.open(str(master_path), "rb") as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (1, 2, 48000)
        assert reader.getnframes() == timeline.sample_count
        assert len(reader.readframes(reader.getnframes())) == timeline.sample_count * 2
    assert not list(master_path.parent.glob("*.partial"))
    assert not list(master_path.parent.glob(".*.partial"))


def test_tampered_reused_asset_keeps_previous_timeline_and_master(tmp_path):
    store, project, revision = fixture(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    first = asyncio.run(synthesize_timeline(
        store, FileTTS(artifacts), project, revision, {},
        artifact_store=artifacts, artifacts_root=artifacts.root,
    ))
    before = store.load(project.id)
    unit_entry = artifacts.by_id(first.units[0].adopted_audio_asset_id)
    Path(unit_entry["path"]).write_bytes(Path(unit_entry["path"]).read_bytes() + b"changed")

    with pytest.raises(ValueError, match="哈希不一致"):
        asyncio.run(synthesize_timeline(
            store, FileTTS(artifacts), before, revision, {},
            artifact_store=artifacts, artifacts_root=artifacts.root,
        ))

    after = store.load(project.id)
    assert after.audio_timeline.master_audio_asset_id == first.master_audio_asset_id
    assert after.model_dump() == before.model_dump()


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_real_candidate_adoption_media_failure_keeps_old_timeline(tmp_path, damage):
    store, project, revision = fixture(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    tts = FileTTS(artifacts)
    asyncio.run(synthesize_timeline(
        store, tts, project, revision, {},
        artifact_store=artifacts, artifacts_root=artifacts.root,
    ))
    current = store.load(project.id)
    candidate = asyncio.run(synthesize_timeline(
        store, tts, current, revision, {}, target_turn_ids=["T-0"],
        artifact_store=artifacts, artifacts_root=artifacts.root,
    ))
    asset_id = candidate.units[0].candidate_audio_asset_ids[-1]
    entry = artifacts.by_id(asset_id)
    if damage == "missing":
        Path(entry["path"]).unlink()
    else:
        Path(entry["path"]).write_bytes(Path(entry["path"]).read_bytes() + b"changed")
    before = store.load(project.id)

    with pytest.raises(ValueError, match="ADOPTION_MEDIA|INVALID_MASTER_INPUT"):
        adopt_unit_candidate(
            store, project.id, "U-T-0", asset_id,
            artifact_store=artifacts, artifacts_root=artifacts.root,
            require_media=True,
        )

    assert store.load(project.id).model_dump() == before.model_dump()


def reference_provider(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    provider = ComfyUITTSProvider(
        "http://127.0.0.1:8188", tmp_path / "input", {},
        artifacts.root, artifacts,
    )
    return provider, artifacts


def test_reference_asset_hash_is_checked_and_stale_copy_is_repaired(tmp_path):
    provider, artifacts = reference_provider(tmp_path)
    source = tmp_path / "reference.wav"
    payload = b"reference-audio-bytes"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    artifacts.register("REF", "audio", str(source), "dep-ref")

    name = provider._ensure_ref_in_input(
        "A", {"artifactId": "REF", "fileHash": digest})
    copied = provider.input_dir / name
    assert copied.read_bytes() == payload

    copied.write_bytes(b"x" * len(payload))  # 同大小损坏也必须修复
    assert provider._ensure_ref_in_input(
        "A", {"artifactId": "REF", "fileHash": digest}) == name
    assert copied.read_bytes() == payload

    source.write_bytes(b"y" * len(payload))
    with pytest.raises(RuntimeError, match="TTS_REF_HASH"):
        provider._ensure_ref_in_input("A", {"artifactId": "REF", "fileHash": digest})


def test_reference_binding_hash_must_match_artifact_registration(tmp_path):
    provider, artifacts = reference_provider(tmp_path)
    source = tmp_path / "reference.wav"
    source.write_bytes(b"reference")
    artifacts.register("REF", "audio", str(source), "dep-ref")

    with pytest.raises(RuntimeError, match="TTS_REF_HASH"):
        provider._ensure_ref_in_input(
            "A", {"artifactId": "REF", "fileHash": "0" * 64})


def test_reference_copy_is_hash_checked_after_write(tmp_path, monkeypatch):
    provider, artifacts = reference_provider(tmp_path)
    source = tmp_path / "reference.wav"
    payload = b"reference"
    source.write_bytes(payload)
    provider.refs = {"A": str(source)}

    def broken_destination_hash(path):
        if Path(path).parent == provider.input_dir:
            return "0" * 64
        return artifacts._hash_file(path)

    monkeypatch.setattr(provider.artifacts, "_hash_file", broken_destination_hash)
    with pytest.raises(RuntimeError, match="复制后哈希校验失败"):
        provider._ensure_ref_in_input("A")
