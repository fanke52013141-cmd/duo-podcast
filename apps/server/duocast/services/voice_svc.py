"""声音业务编排（06 §3.2 voice_svc）：话轮 → 合成单元 → 权威时间轨（01 §11.2）。

三层对象落位：Line（编辑/字幕/读音）→ Turn（自然说话行为）→ Unit（引擎请求单元）。
时间权威 = 引擎返回 sampleCount（48kHz 整数样本），前端不自算。
"""

from __future__ import annotations

import uuid
from typing import Any

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
    for i, turn in enumerate(revision.turns, start=1):
        binding = bindings.get(turn.speaker, {})
        binding_id = binding.get("id") or f"VB-{turn.speaker}-{binding.get('characterId', 'default')}"
        units.append(SynthesisUnit(
            id=f"U{i:02d}",
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
) -> AudioTimeline:
    """逐单元请求 TTS，累积整数样本偏移，构造并写回 AudioTimeline。"""
    units = build_units(project, revision, bindings)
    timeline = AudioTimeline(
        revision_id=revision.id,
        unit_offsets={},
        transition_gap_ms=transition_gap_ms or [320] * max(len(units) - 1, 0),
    )
    offset = 0
    for unit in units:
        line_texts = [ln.spoken_text for ln in revision_turn_lines(revision, unit)]
        res = await tts.synthesize({"lineTexts": line_texts, "voiceBindingId": unit.voice_binding_id})
        sample_count = int(res.get("sampleCount", 0))
        unit.sample_count = sample_count
        unit.candidate_audio_asset_ids = [f"audio-{uuid.uuid4().hex[:8]}"]
        timeline.unit_offsets[unit.id] = offset
        offset += sample_count
        timeline.units.append(unit)
    timeline.sample_count = offset

    # 写回工程（原子 apply：audio_timeline + voice_bindings）
    voice_bindings = binding_models(bindings)
    store.apply(project.id, {
        "audio_timeline": timeline.model_dump(by_alias=True),
        "voice_bindings": [b.model_dump(by_alias=True) for b in voice_bindings],
    }, expected_revision=project.revision)
    return timeline


def revision_turn_lines(revision: ScriptRevision, unit: SynthesisUnit) -> list:
    for turn in revision.turns:
        if turn.id == unit.turn_id:
            return turn.lines
    return []
