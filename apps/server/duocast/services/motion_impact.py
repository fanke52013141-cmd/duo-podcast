"""CPU-only proposals from real registered PCM and pinned motion; never runs workers."""
import hashlib
import wave
from pathlib import Path

from .motion_render_inputs import build_motion_worker_inputs
from .motion_segments import build_motion_segment_cache_plan


def project_segment_plan(project, artifacts, motion_store, *, previous=None, seed=1, pronunciation=None):
    timeline = project.audio_timeline
    revision = next((r for r in project.script_revisions if r.id == project.current_draft_revision), None)
    if not project.motion_pack:
        raise ValueError('本工程没有固定动作包')
    if not timeline or not revision or timeline.revision_id != revision.id or not timeline.master_audio_asset_id:
        raise ValueError('请先生成与当前脚本一致的真实配音母轨')
    from .render_inputs import audio_inputs_match
    if not audio_inputs_match(project, pronunciation):
        raise ValueError('脚本、声音或读音依赖已改变，请重新合成配音后规划')
    entry = artifacts.by_id(timeline.master_audio_asset_id)
    if not entry or entry.get('kind') not in {'audio', 'mixed'} or not Path(entry['path']).is_file():
        raise ValueError('配音母轨文件缺失')
    if artifacts._hash_file(entry['path']) != entry.get('fileHash'):
        raise ValueError('配音母轨内容已改变')
    rate, total = timeline.sample_rate, timeline.sample_count
    if not 0 < total <= rate * 300 or total * 2 > 64 * 1024 * 1024:
        raise ValueError('母轨超过规划容量限制')
    with wave.open(entry['path'], 'rb') as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes(), audio.getcomptype()) != (rate, 1, 2, total, 'NONE'):
            raise ValueError('母轨不是完整的PCM16单声道音频')
        pcm = audio.readframes(total)
    if len(pcm) != total * 2:
        raise ValueError('母轨不完整')
    speakers = {turn.id: turn.speaker for turn in revision.turns}
    roles = {role: bytearray(total * 2) for role in 'AB'}
    turns = []
    for unit in timeline.units:
        start = timeline.unit_offsets.get(unit.id)
        if (type(start) is not int or unit.turn_id not in speakers or unit.sample_count <= 0
                or start < 0 or start + unit.sample_count > total):
            raise ValueError('时间轨单元范围或角色无效')
        end = start + unit.sample_count
        role = speakers[unit.turn_id]
        roles[role][start*2:end*2] = pcm[start*2:end*2]
        turns.append({'id': unit.id, 'speaker': role, 'startSample': start, 'endSample': end})
    turns.sort(key=lambda turn: turn['startSample'])
    if any(a['endSample'] > b['startSample'] for a, b in zip(turns, turns[1:])):
        raise ValueError('时间轨存在重叠单元')
    pin = project.motion_pack
    assets = build_motion_worker_inputs(motion_store, artifacts, pin.id, pin.version, project.shot_style,
        turns, sample_rate=rate, total_samples=total, seed=str(seed))
    # A fixed planning namespace compares content only. It cannot certify that
    # the GPU runtime or any rendered cache output is present on this machine.
    return build_motion_segment_cache_plan(assets, turns, sample_rate=rate, total_samples=total,
        role_audio={role:{'pcm':bytes(data)} for role,data in roles.items()},
        render_spec={key:project.render_spec[key] for key in ('fps','resolution','fastQuality') if key in project.render_spec},
        runtime_fingerprint=hashlib.sha256(b'cpu-planning-only-unverified-runtime-v1').hexdigest(), previous=previous)


def motion_impact(project, artifacts, motion_store, previous_project=None, *, previous_seed=1, pronunciation=None):
    baseline = None
    baseline_error = None
    if previous_project is not None:
        try:
            baseline = project_segment_plan(previous_project, artifacts, motion_store, seed=previous_seed)
        except (ValueError, OSError, KeyError, wave.Error) as exc:
            baseline_error = str(exc)
    try:
        plan = project_segment_plan(project, artifacts, motion_store, previous=baseline, pronunciation=pronunciation)
    except (ValueError, OSError, KeyError, wave.Error) as exc:
        return {'available':False,'blockers':[str(exc)],'segments':[], 'runtimeVerified':False,'cacheOutputsVerified':False}
    rows = [{key:row[key] for key in ('cameraId','role','outputStartFrame','outputEndFrame',
             'affectedUtteranceIds','renderContextUtteranceIds','reuseCandidate','reason','resampleWholeTrackDependency')}
            for row in plan['segments']]
    return {'available':True, 'segments':rows, 'fps':25, 'baselineAvailable':baseline is not None,
            'baselineError':baseline_error,'retiredCount':len(plan['retiredKeys']),
            'candidateCount':sum(row['reuseCandidate'] for row in rows),
            'runtimeVerified':False,'cacheOutputsVerified':False,
            'notice':'按正式生成默认种子1规划，对照使用历史任务实际种子；只比较真实PCM和动作输入。尚未验证GPU运行时与缓存输出，候选不等于已复用。48kHz音频按整条角色轨保守失效；仅编码规格改变可保留动作依赖，但需要重新编码。'}
