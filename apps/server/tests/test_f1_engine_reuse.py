"""F1 真实引擎语义下的复用验证。

mock TTS 不产音频文件，F1 的「复用 + 母轨拼接」路径在演示模式走不到。
这里用一个会落盘 WAV 并登记产物的适配器模拟真实引擎，验证：
1. 首次整期合成：每个单元都请求 TTS，并拼出母轨；
2. 改一句后：只有改动单元请求 TTS，其余复用旧资产，母轨仍能拼出完整时长。
"""

import asyncio
import wave
from pathlib import Path

from duocast.domain.project import Line, ScriptRevision, Turn
from duocast.services.voice_svc import synthesize_timeline
from duocast.storage.artifacts import ArtifactStore
from duocast.storage.project_store import ProjectStore


class FileTTS:
    """与 ComfyUI 适配器同构：返回 sampleCount + audioAssetId + audioPath，并登记产物。"""

    capabilities = {"simulated": False}
    name = "file-tts"

    def __init__(self, media: Path, artifacts: ArtifactStore) -> None:
        self.media = media
        self.artifacts = artifacts
        self.calls: list[dict] = []

    async def synthesize(self, request):
        self.calls.append(request)
        index = len(self.calls)
        asset_id = f"AUD-{index}"
        path = self.media / f"{asset_id}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(b"\x00\x00" * 48000)
        self.artifacts.register(asset_id, "audio", str(path), f"dep-{asset_id}", file_hash="")
        return {"sampleRate": 48000, "sampleCount": 48000,
                "audioAssetId": asset_id, "audioPath": str(path)}


def _project(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    media = tmp_path / "media"
    media.mkdir()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = store.create("engine-reuse")
    revision = ScriptRevision(id="R1", turns=[
        Turn(id="T1", speaker="A", lines=[Line(id="L1", spoken_text="第一句")]),
        Turn(id="T2", speaker="B", lines=[Line(id="L2", spoken_text="第二句")]),
        Turn(id="T3", speaker="A", lines=[Line(id="L3", spoken_text="第三句")]),
    ])
    saved = store.apply(project.id, {"script_revisions": [revision], "current_draft_revision": "R1"}, 0)
    return store, artifacts, media, saved, revision


def test_real_engine_synthesis_builds_master_track(tmp_path):
    store, artifacts, media, project, revision = _project(tmp_path)
    tts = FileTTS(media, artifacts)
    timeline = asyncio.run(synthesize_timeline(
        store, tts, project, revision, {}, artifact_store=artifacts, artifacts_root=artifacts.root))

    assert len(tts.calls) == 3
    assert timeline.reused_unit_ids == []
    assert timeline.master_audio_asset_id, "真实引擎必须拼出整期母轨"
    master = artifacts.by_id(timeline.master_audio_asset_id)
    assert Path(master["path"]).is_file()


def test_real_engine_reuses_unchanged_units_and_keeps_master(tmp_path):
    store, artifacts, media, project, revision = _project(tmp_path)
    first_tts = FileTTS(media, artifacts)
    asyncio.run(synthesize_timeline(
        store, first_tts, project, revision, {}, artifact_store=artifacts, artifacts_root=artifacts.root))
    assert len(first_tts.calls) == 3

    saved = store.load(project.id)
    changed = ScriptRevision(id="R2", turns=[
        Turn(id="T1", speaker="A", lines=[Line(id="L1", spoken_text="第一句")]),
        Turn(id="T2", speaker="B", lines=[Line(id="L2", spoken_text="第二句（改过）")]),
        Turn(id="T3", speaker="A", lines=[Line(id="L3", spoken_text="第三句")]),
    ])
    project = store.apply(project.id, {
        "script_revisions": [*saved.script_revisions, changed],
        "current_draft_revision": "R2",
    }, saved.revision)

    second_tts = FileTTS(media, artifacts)
    second = asyncio.run(synthesize_timeline(
        store, second_tts, project, changed, {}, artifact_store=artifacts, artifacts_root=artifacts.root))

    assert len(second_tts.calls) == 1, "只应为改动的那一句请求引擎"
    assert set(second.reused_unit_ids) == {"U-T1", "U-T3"}
    # 母轨仍按 3 个单元拼接，且复用单元的文件路径可从产物清单取回
    master = artifacts.by_id(second.master_audio_asset_id)
    assert Path(master["path"]).is_file()
    assert second.sample_count > 3 * 48000
