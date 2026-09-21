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
        unit = SynthesisUnit(
            id=f"U-{turn.id}",
            turn_id=turn.id,
            line_ids=[ln.id for ln in turn.lines],
            provider_profile_id=binding.get("providerProfileId", "mock-tts"),
            model_id=binding.get("modelId", ""),
            voice_binding_id=binding_id,
            emotion={"label": turn.tone or "自然"},
            speed_ratio=turn.speed_ratio,
        )
        unit.pronunciation_revision = _unit_fingerprint(turn, unit)
        units.append(unit)
    return units


def _unit_fingerprint(turn, unit: SynthesisUnit) -> str:
    """确定某个已采用单元还能否服务当前话轮。

    版本 ID 本身不足以判断：用户可在同一草稿内编辑一条台词。把会影响音频的
    文本和请求参数写入单元，局部合成才不会错误复用旧声音。
    """
    payload = {
        "turnId": turn.id, "speaker": turn.speaker,
        "lineTexts": [line.spoken_text for line in turn.lines],
        "voiceBindingId": unit.voice_binding_id,
        "providerProfileId": unit.provider_profile_id, "modelId": unit.model_id,
        "emotion": unit.emotion, "speedRatio": unit.speed_ratio,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _effective_bindings(project: Project, supplied: dict[str, dict]) -> dict[str, dict]:
    """局部重做未显式传声线时沿用工程已保存的绑定。"""
    persisted = {
        binding.id[3:4] if binding.id.startswith("VB-") and binding.id[3:4] in ("A", "B") else "": {
            "id": binding.id, "characterId": binding.character_id,
            "providerProfileId": binding.provider_profile_id, "modelId": binding.model_id,
            "localRefAudio": binding.local_ref_audio, "minimaxVoiceId": binding.minimax_voice_id,
        }
        for binding in project.voice_bindings
    }
    return {**persisted, **(supplied or {})}


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
    target_turn_ids: list[str] | None = None,
) -> AudioTimeline:
    """逐单元请求 TTS，累积整数样本偏移，构造并写回 AudioTimeline。

    真实引擎（返回 audioPath）时在写回前拼接整期母轨（关口 A：③页直接试听）。
    """
    bindings = _effective_bindings(project, bindings)
    units = build_units(project, revision, bindings)
    if not units:
        raise ValueError("EMPTY_SCRIPT: 没有可合成的发言")
    valid_turn_ids = {unit.turn_id for unit in units}
    if target_turn_ids is not None and (
        not target_turn_ids or len(target_turn_ids) != len(set(target_turn_ids))
        or any(turn_id not in valid_turn_ids for turn_id in target_turn_ids)
    ):
        raise ValueError("INVALID_TURN_IDS: 必须选择当前脚本中的非空、无重复话轮")
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
    # 全量合成延续旧语义：生成后立即成为整期采用版本。局部合成只登记候选，
    # 直到调用 adopt 接口才会替换采用音频和母轨。
    is_full_synthesis = target_turn_ids is None
    target_set = set(target_turn_ids or [])
    current = store.load(project.id) if store is not None else None
    candidates_by_unit = _candidate_units_for_revision(current, revision.id) if current else {}
    adopted_by_unit = _adopted_units_for_revision(current, revision.id) if current else {}
    offset = lead_in_ms * 48
    unit_paths: dict[str, str] = {}
    turn_speaker = {t.id: t.speaker for t in revision.turns}
    for index, unit in enumerate(units):
        line_texts = [ln.spoken_text for ln in revision_turn_lines(revision, unit)]
        if not "".join(line_texts).strip():
            raise ValueError(f"INVALID_AUDIO: 话轮 {unit.turn_id} 没有可朗读文本，请在脚本中删除或补全后重试")
        previous_adopted = adopted_by_unit.get(unit.id)
        previous_candidates = candidates_by_unit.get(unit.id)
        if previous_adopted:
            _copy_candidate_state(unit, previous_adopted)
        if previous_candidates:
            _merge_candidates(unit, previous_candidates)
        should_synthesize = is_full_synthesis or unit.turn_id in target_set
        if should_synthesize:
            res = await tts.synthesize({
                "lineTexts": line_texts, "voiceBindingId": unit.voice_binding_id,
                "speaker": turn_speaker.get(unit.turn_id, "A"),
                "providerProfileId": unit.provider_profile_id, "modelId": unit.model_id,
                "emotion": unit.emotion, "speedRatio": unit.speed_ratio,
            })
            sample_count = res.get("sampleCount", 0)
            if type(sample_count) is not int or sample_count <= 0:
                raise ValueError("INVALID_AUDIO: 音频样本数必须为正整数")
            if res.get("sampleRate") != timeline.sample_rate:
                raise ValueError("INVALID_SAMPLE_RATE: 适配器必须先转换为 48kHz 母轨")
            asset_id = res.get("audioAssetId")
            if asset_id:
                if asset_id not in unit.candidate_audio_asset_ids:
                    unit.candidate_audio_asset_ids.append(asset_id)
                unit.candidate_sample_counts[asset_id] = sample_count
            if is_full_synthesis:
                # 保持已有「整期生成后即可试听」行为；局部生成不会走到这里。
                unit.adopted_audio_asset_id = asset_id
                unit.sample_count = sample_count
                if res.get("audioPath"):
                    unit_paths[unit.id] = res["audioPath"]
            elif not unit.adopted_audio_asset_id:
                # 没有已采用音频时，候选仍保留自己的权威长度，供采用接口建立时间线。
                unit.sample_count = sample_count
        if not is_full_synthesis and unit.adopted_audio_asset_id and previous_adopted:
            unit.sample_count = previous_adopted.sample_count
        timeline.unit_offsets[unit.id] = offset
        offset += unit.sample_count
        if index < len(gaps):
            offset += gaps[index] * 48
        timeline.units.append(unit)
    timeline.sample_count = offset + tail_out_ms * 48

    if is_full_synthesis and artifact_store is not None and artifacts_root is not None and len(unit_paths) == len(units):
        timeline.master_audio_asset_id = _build_master_track(
            timeline, unit_paths, revision.id, artifact_store, Path(artifacts_root))

    # 写回工程（原子 apply：audio_timeline + voice_bindings）
    voice_bindings = binding_models(bindings)
    if current is None:
        raise KeyError(project.id)
    patch = {"audio_history": [*current.audio_history, timeline]}
    # 旧任务始终保留结果，但不可替换已经编辑过的当前配音。
    if (is_full_synthesis and current.revision == project.revision
            and current.current_draft_revision == revision.id):
        patch.update(audio_timeline=timeline, voice_bindings=voice_bindings,
                     approvals=[a for a in current.approvals if a.kind not in ('voice', 'sample')])
    elif (not is_full_synthesis and current.current_draft_revision == revision.id):
        # 候选本身不替换母轨，但采用时必须按产生候选时的声线配置校验输入指纹。
        patch["voice_bindings"] = voice_bindings
    store.apply(project.id, patch, expected_revision=current.revision)
    return timeline


def revision_turn_lines(revision: ScriptRevision, unit: SynthesisUnit) -> list:
    for turn in revision.turns:
        if turn.id == unit.turn_id:
            return turn.lines
    return []


def _copy_candidate_state(destination: SynthesisUnit, source: SynthesisUnit) -> None:
    """复制已采用状态；只允许同一输入指纹的版本被复用。"""
    if source.pronunciation_revision != destination.pronunciation_revision:
        return
    destination.adopted_audio_asset_id = source.adopted_audio_asset_id
    destination.sample_count = source.sample_count
    _merge_candidates(destination, source)


def _merge_candidates(destination: SynthesisUnit, source: SynthesisUnit) -> None:
    if source.pronunciation_revision != destination.pronunciation_revision:
        return
    for asset_id in source.candidate_audio_asset_ids:
        if asset_id not in destination.candidate_audio_asset_ids:
            destination.candidate_audio_asset_ids.append(asset_id)
        count = source.candidate_sample_counts.get(asset_id)
        if type(count) is int and count > 0:
            destination.candidate_sample_counts[asset_id] = count


def _adopted_units_for_revision(project: Project | None, revision_id: str) -> dict[str, SynthesisUnit]:
    if not project or not project.audio_timeline or project.audio_timeline.revision_id != revision_id:
        return {}
    return {unit.id: unit for unit in project.audio_timeline.units if unit.adopted_audio_asset_id}


def _candidate_units_for_revision(project: Project | None, revision_id: str) -> dict[str, SynthesisUnit]:
    """按时间倒序合并候选，历史 JSON 不含新字段时自然跳过。"""
    if not project:
        return {}
    merged: dict[str, SynthesisUnit] = {}
    sources = [project.audio_timeline, *reversed(project.audio_history)]
    for timeline in sources:
        if not timeline or timeline.revision_id != revision_id:
            continue
        for unit in timeline.units:
            existing = merged.get(unit.id)
            if existing is None:
                merged[unit.id] = unit.model_copy(deep=True)
            else:
                _merge_candidates(existing, unit)
    return merged


def _timeline_from_adopted_units(
    project: Project,
    revision: ScriptRevision,
    bindings: dict[str, dict],
    candidate_units: dict[str, SynthesisUnit],
    unit_id: str,
    candidate_asset_id: str,
) -> AudioTimeline:
    """将一个候选提升为采用版本，并验证其余话轮均能复用当前脚本的采用音频。"""
    base_units = build_units(project, revision, bindings)
    previous = _adopted_units_for_revision(project, revision.id)
    timeline = project.audio_timeline
    gaps = (timeline.transition_gap_ms if timeline and len(timeline.transition_gap_ms) == len(base_units) - 1
            else [320] * (len(base_units) - 1))
    lead = timeline.lead_in_ms if timeline else 0
    tail = timeline.tail_out_ms if timeline else 0
    result = AudioTimeline(revision_id=revision.id, transition_gap_ms=gaps,
                           lead_in_ms=lead, tail_out_ms=tail)
    offset = lead * 48
    for index, unit in enumerate(base_units):
        old = previous.get(unit.id)
        candidate = candidate_units.get(unit.id)
        if old:
            _copy_candidate_state(unit, old)
        if candidate:
            _merge_candidates(unit, candidate)
        if unit.id == unit_id:
            if candidate_asset_id not in unit.candidate_audio_asset_ids:
                raise ValueError("CANDIDATE_NOT_FOUND: 候选音频不属于该合成单元")
            count = unit.candidate_sample_counts.get(candidate_asset_id)
            if type(count) is not int or count <= 0:
                raise ValueError("CANDIDATE_INVALID: 候选音频缺少有效样本数")
            unit.adopted_audio_asset_id = candidate_asset_id
            unit.sample_count = count
        if not unit.adopted_audio_asset_id or unit.sample_count <= 0:
            raise ValueError(f"ADOPTION_INCOMPLETE: 话轮 {unit.turn_id} 尚无可采用音频")
        result.unit_offsets[unit.id] = offset
        offset += unit.sample_count
        if index < len(gaps):
            offset += gaps[index] * 48
        result.units.append(unit)
    result.sample_count = offset + tail * 48
    return result


def _paths_for_adopted_units(timeline: AudioTimeline, artifact_store) -> dict[str, str]:
    if artifact_store is None:
        return {}
    paths: dict[str, str] = {}
    for unit in timeline.units:
        entry = artifact_store.by_id(unit.adopted_audio_asset_id or "")
        if not entry or not entry.get("path"):
            return {}
        paths[unit.id] = entry["path"]
    return paths


def adopt_unit_candidate(
    store: ProjectStore,
    project_id: str,
    unit_id: str,
    candidate_asset_id: str,
    artifact_store=None,
    artifacts_root: Path | None = None,
) -> AudioTimeline:
    """采用候选音频并重建整期权威时间线与母轨。"""
    project = store.load(project_id)
    if project is None:
        raise KeyError("PROJECT_NOT_FOUND")
    revision = find_revision(project, project.current_draft_revision)
    bindings = _effective_bindings(project, {})
    candidates = _candidate_units_for_revision(project, revision.id)
    canonical = _timeline_from_adopted_units(
        project, revision, bindings, candidates, unit_id, candidate_asset_id)
    paths = _paths_for_adopted_units(canonical, artifact_store)
    if artifact_store is not None and artifacts_root is not None and len(paths) == len(canonical.units):
        canonical.master_audio_asset_id = _build_master_track(
            canonical, paths, revision.id, artifact_store, Path(artifacts_root))
    updated = store.apply(project_id, {
        "audio_timeline": canonical,
        "audio_history": [*project.audio_history, canonical],
        "approvals": [approval for approval in project.approvals
                      if approval.kind not in ("voice", "sample")],
    }, expected_revision=project.revision)
    return updated.audio_timeline


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
