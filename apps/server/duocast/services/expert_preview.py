"""Select frozen expert windows; previews never rebase clocks or sampling seeds."""
from copy import deepcopy
import hashlib
import json
import re


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def timeline_turns(project):
    timeline = project['audioTimeline']
    if timeline['revisionId'] != project['currentDraftRevision']:
        raise ValueError('Current script and adopted voice revision differ')
    revision = next(r for r in project['scriptRevisions'] if r['id'] == timeline['revisionId'])
    speakers = {t['id']: t['speaker'] for t in revision['turns']}
    turns = []
    units = sorted(timeline['units'], key=lambda u: timeline['unitOffsets'][u['id']])
    for unit in units:
        if not unit.get('adoptedAudioAssetId'):
            raise ValueError('Every selected voice unit must have an adopted audio asset')
        start = timeline['unitOffsets'][unit['id']]
        if type(start) is not int or type(unit['sampleCount']) is not int or unit['sampleCount'] <= 0:
            raise ValueError('Voice units require positive integer sample ranges')
        if turns and start < turns[-1]['endSample']:
            raise ValueError('Voice units must not overlap')
        if turns and turns[-1]['id'] == unit['turnId']:
            turns[-1]['endSample'] = start+unit['sampleCount']
            continue
        if any(t['id'] == unit['turnId'] for t in turns):
            raise ValueError('Voice units for a turn must be consecutive')
        turns.append({'id': unit['turnId'], 'speaker': speakers[unit['turnId']],
                      'startSample': start, 'endSample': start+unit['sampleCount']})
    if [t['id'] for t in turns] != [t['id'] for t in revision['turns']]:
        raise ValueError('Adopted voice must cover every current script turn in order')
    return turns


def checked_timeline(plan, turns):
    rate, total = plan['sampleRate'], plan['totalSamples']
    if type(rate) is not int or not 1 <= rate <= 384000 or type(total) is not int or not 0 < total <= rate*300:
        raise ValueError('Invalid authoritative audio clock')
    count = (total*25+rate-1)//rate
    if plan['totalFrames'] != count or not turns or len(turns) > 10000:
        raise ValueError('Missing turns or mismatched frame clock')
    ids, end = set(), 0
    for turn in turns:
        if (not isinstance(turn['id'], str) or not turn['id'] or turn['id'] in ids
                or turn['speaker'] not in {'A', 'B'}
                or type(turn['startSample']) is not int or type(turn['endSample']) is not int
                or not end <= turn['startSample'] < turn['endSample'] <= total):
            raise ValueError('Turns must have unique IDs and ordered nonoverlapping sample ranges')
        ids.add(turn['id']); end = turn['endSample']
    cursor = 0
    for segment in plan['segments']:
        left, right = segment['outputStartFrame'], segment['outputEndFrame']
        if (type(left) is not int or type(right) is not int or left != cursor or not left < right <= count
                or not 0 <= segment['contextStartFrame'] <= left < right <= segment['contextEndFrame'] <= count
                or segment['trimEndFrame']-segment['trimStartFrame'] != right-left
                or not 0 <= segment['trimStartFrame'] < segment['trimEndFrame'] <= segment['modelFrames']):
            raise ValueError('Invalid frozen window coverage/context')
        cursor = right
    if cursor != count:
        raise ValueError('Frozen windows must cover the full episode')


def select_windows(plan, turns, *, turn_ids=None, operation='preview', candidate_token=None):
    """Pure plan. Caller must separately verify media/runtime, execute and adopt."""
    checked_timeline(plan, turns)
    if operation not in {'preview', 'regenerate', 'continue'}:
        raise ValueError('Unsupported window operation')
    selected = [t['id'] for t in (turns if operation=='continue' else turns[:4])] if turn_ids is None else turn_ids
    if (not isinstance(selected, list) or not selected or len(selected) != len(set(selected))
            or any(t not in {v['id'] for v in turns} for t in selected)):
        raise ValueError('Select distinct existing turn IDs')
    positions = [i for i, t in enumerate(turns) if t['id'] in selected]
    if selected != [turns[i]['id'] for i in positions] or positions != list(range(positions[0], positions[-1]+1)):
        raise ValueError('Selected turns must be contiguous and in script order')
    if operation == 'preview' and len(selected) > 4:
        raise ValueError('Preview supports up to four complete turns')
    if operation == 'regenerate' and (not isinstance(candidate_token, str)
            or re.fullmatch(r'[A-Za-z0-9_-]{8,128}', candidate_token) is None):
        raise ValueError('Regeneration requires a stable unique candidate token')
    chosen = turns[positions[0]:positions[-1]+1]
    rate = plan['sampleRate']
    left = chosen[0]['startSample']*25//rate
    right = (chosen[-1]['endSample']*25+rate-1)//rate
    windows, bindings = [], {}
    for index, original in enumerate(plan['segments']):
        intersects = original['outputStartFrame'] < right and original['outputEndFrame'] > left
        # Rebuilding a round also refreshes neighboring windows whose conditioning overlaps it.
        context = original['contextStartFrame'] < right and original['contextEndFrame'] > left
        if not (intersects or operation == 'regenerate' and context):
            continue
        segment = deepcopy(original)
        if operation == 'regenerate':
            key = digest({'sourceDependency': original['dependencyHash'], 'candidateToken': candidate_token})
            segment.update(seed=int(key[:16], 16) % (2**63), dependencyHash=key)
        window_id = f'EW-{index:05d}'
        windows.append({'windowId': window_id, 'index': index,
                        'reason': 'selected_turn' if intersects else 'boundary_context',
                        'sourceDependencyHash': original['dependencyHash'], 'segment': segment})
        bindings[window_id] = [t['id'] for t in turns
            if t['startSample']*25 < original['outputEndFrame']*rate
            and t['endSample']*25 > original['outputStartFrame']*rate]
    if not windows:
        raise ValueError('Selection does not intersect model windows')
    payload = {'formatVersion': 1, 'operation': operation, 'sourcePlanHash': digest(plan),
        'selectedTurnIds': selected, 'selectedSampleRange': [chosen[0]['startSample'], chosen[-1]['endSample']],
        'reviewSampleRange': ([(windows[0]['segment']['outputStartFrame']*rate+24)//25,
            min(plan['totalSamples'], (windows[-1]['segment']['outputEndFrame']*rate+24)//25)]
            if operation == 'regenerate' else [chosen[0]['startSample'], chosen[-1]['endSample']]),
        'sampleRate': rate,
        'totalSamples': plan['totalSamples'], 'totalFrames': plan['totalFrames'],
        'fullWindowCount': len(plan['segments']),
        'displayFrameRange': [left, right], 'windows': windows, 'turnWindowBindings': bindings,
        'preserveAbsoluteClock': True, 'rebaseSeed': False, 'candidateToken': candidate_token,
        'modelCallsSubmitted': 0, 'qualityAccepted': False,
        'reuseRequires': ['same frozen dependencies', 'verified output hash and native clock', 'valid runtime identity']}
    if operation=='continue':
        payload.update(selectedSampleRange=[0,plan['totalSamples']],reviewSampleRange=[0,plan['totalSamples']],displayFrameRange=[0,plan['totalFrames']])
    payload['planId'] = 'EP-'+digest(payload)[:24]
    return payload
