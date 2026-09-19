"""声音业务编排（06 §3.2 voice_svc）：话轮 → 合成单元 → 权威时间轨（01 §11.2）。

三层对象落位：Line（编辑/字幕/读音）→ Turn（自然说话行为）→ Unit（引擎请求单元）。
时间权威 = 引擎返回 sampleCount（48kHz 整数样本），前端不自算。
"""

from __future__ import annotations

import hashlib
import json
import uuid
import wave
from array import array
from pathlib import Path

from ..adapters.base import TTSProvider
from ..domain.audio import AudioTimeline, SynthesisUnit, VoiceBinding
from ..domain.project import Project, ScriptRevision
from ..storage.project_store import ProjectStore


def find_revision(project: Project, revision_id: str | None) -> ScriptRevision:
    rid = revision_id or project.current_draft_revision
    for rev in project.script_revisions:
        if rev.id == rid:
            return rev
    raise KeyError(f"revision not found: {rid}")


def build_units(project: Project, revision: ScriptRevision, bindings: dict[str, dict]) -> list[SynthesisUnit]:
    """按话轮生成合成单元；绑定取 bindings[A|B]，缺省为占位绑定。"""
    units: list[SynthesisUnit] = []
    for turn in revision.turns:
        binding = bindings.get(turn.speaker, {})
        binding_id = binding.get("id") or f"VB-{turn.speaker}-{binding.get('characterId', 'default')}"
        units.append(SynthesisUnit(
            id=f"U-{turn.id}",
            turn_id=turn.id,
            line_ids=[ln.id for ln in turn.lines],
            provider_profile_id=binding.get("providerProfileId", "mock-tts"),
            model_id=binding.get("modelId", ""),
            voice_binding_id=binding_id,
            emotion={"label": turn.tone or "自然"},
            speed_ratio=turn.speed_ratio,
        ))
    return units


def binding_models(bindings: dict[str, dict]) -> list[VoiceBinding]:
    """把前端传入的 A/B 绑定登记为工程级 VoiceBinding（保持 audition_state 状态机）。"""
    out: list[VoiceBinding] = []
    for speaker, b in bindings.items():
        out.append(VoiceBinding(
            id=b.get("id") or f"VB-{speaker}-{b.get('characterId', 'default')}",
            character_id=b.get("characterId", ""),
            provider_profile_id=b.get("providerProfileId", "mock-tts"),
            model_id=b.get("modelId", ""),
            local_ref_audio=b.get("localRefAudio"),
            minimax_voice_id=b.get("minimaxVoiceId"),
            audition_state="none",
        ))
    return out


async def synthesize_timeline(
    store: ProjectStore,
    tts: TTSProvider,
    project: Project,
    revision: ScriptRevision,
    bindings: dict[str, dict],
    transition_gap_ms: list[int] | None = None,
    lead_in_ms: int = 0,
    tail_out_ms: int = 0,
    artifact_store=None,
    artifacts_root: Path | None = None,
) -> AudioTimeline:
    """逐单元请求 TTS，累积整数样本偏移，构造并写回 AudioTimeline。

    真实引擎（返回 audioPath）时在写回前拼接整期母轨（关口 A：③页直接试听）。
    """
    units = build_units(project, revision, bindings)
    if not units:
        raise ValueError("EMPTY_SCRIPT: 没有可合成的发言")
    gaps = transition_gap_ms if transition_gap_ms is not None else [320] * (len(units) - 1)
    if len(gaps) != len(units) - 1:
        raise ValueError("INVALID_GAPS: 停顿数量必须等于发言数减一")
    if any(type(ms) is not int or ms < 0 for ms in [*gaps, lead_in_ms, tail_out_ms]):
        raise ValueError("INVALID_GAPS: 停顿必须是非负整数毫秒")
    timeline = AudioTimeline(
        revision_id=revision.id,
        unit_offsets={},
        transition_gap_ms=gaps,
        lead_in_ms=lead_in_ms,
        tail_out_ms=tail_out_ms,
    )
    offset = lead_in_ms * 48
    unit_paths: dict[str, str] = {}
    for index, unit in enumerate(units):
        line_texts = [ln.spoken_text for ln in revision_turn_lines(revision, unit)]
        res = await tts.synthesize({
            "lineTexts": line_texts, "voiceBindingId": unit.voice_binding_id,
            "providerProfileId": unit.provider_profile_id, "modelId": unit.model_id,
            "emotion": unit.emotion, "speedRatio": unit.speed_ratio,
        })
        sample_count = res.get("sampleCount", 0)
        if type(sample_count) is not int or sample_count <= 0:
            raise ValueError("INVALID_AUDIO: 音频样本数必须为正整数")
        if res.get("sampleRate") != timeline.sample_rate:
            raise ValueError("INVALID_SAMPLE_RATE: 适配器必须先转换为 48kHz 母轨")
        unit.sample_count = sample_count
        unit.candidate_audio_asset_ids = [res["audioAssetId"]] if res.get("audioAssetId") else []
        unit.adopted_audio_asset_id = res.get("audioAssetId")
        if res.get("audioPath"):
            unit_paths[unit.id] = res["audioPath"]
        timeline.unit_offsets[unit.id] = offset
        offset += sample_count
        if index < len(gaps):
            offset += gaps[index] * 48
        timeline.units.append(unit)
    timeline.sample_count = offset + tail_out_ms * 48

    if artifact_store is not None and artifacts_root is not None and len(unit_paths) == len(units):
        timeline.master_audio_asset_id = _build_master_track(
            timeline, unit_paths, revision.id, artifact_store, Path(artifacts_root))

    # 写回工程（原子 apply：audio_timeline + voice_bindings）
    voice_bindings = binding_models(bindings)
    current = store.load(project.id)
    if current is None:
        raise KeyError(project.id)
    patch = {"audio_history": [*current.audio_history, timeline]}
    # 旧任务始终保留结果，但不可替换已经编辑过的当前配音。
    if current.revision == project.revision and current.current_draft_revision == revision.id:
        patch.update(audio_timeline=timeline, voice_bindings=voice_bindings,
                     approvals=[a for a in current.approvals if a.kind not in ('voice', 'sample')])
    store.apply(project.id, patch, expected_revision=current.revision)
    return timeline


def revision_turn_lines(revision: ScriptRevision, unit: SynthesisUnit) -> list:
    for turn in revision.turns:
        if turn.id == unit.turn_id:
            return turn.lines
    return []


def _build_master_track(
    timeline: AudioTimeline,
    unit_paths: dict[str, str],
    revision_id: str,
    artifact_store,
    artifacts_root: Path,
) -> str:
    """按整数样本偏移把各单元 PCM16/48k 单声道写入静音母轨，登记为 mixed 产物。"""
    master = array("h", bytes(timeline.sample_count * 2))
    for unit in timeline.units:
        path = unit_paths.get(unit.id)
        if not path:
            continue
        with wave.open(path, "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != timeline.sample_rate:
                raise ValueError("INVALID_MASTER_INPUT: 单元音频必须是 48kHz PCM16 单声道")
            frames = wf.readframes(wf.getnframes())
        start = timeline.unit_offsets.get(unit.id, 0)
        end = min(start + wf.getnframes(), timeline.sample_count)
        n = max(0, end - start)
        master[start:end] = array("h", frames[: n * 2])
    artifacts_root.mkdir(parents=True, exist_ok=True)
    asset_id = f"AUD-M-{uuid.uuid4().hex[:12]}"
    path = artifacts_root / "audio" / f"master_{revision_id}_{asset_id}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(timeline.sample_rate)
        out.writeframes(master.tobytes())
    dependency = hashlib.sha256(json.dumps({
        "revisionId": revision_id,
        "units": [{"id": u.id, "asset": u.adopted_audio_asset_id, "offset": timeline.unit_offsets.get(u.id, 0)} for u in timeline.units],
        "gaps": timeline.transition_gap_ms, "leadIn": timeline.lead_in_ms, "tailOut": timeline.tail_out_ms,
    }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    artifact_store.register(asset_id, "mixed", str(path), dependency,
                            params_snapshot={"revisionId": revision_id, "sampleCount": timeline.sample_count})
    return asset_id
