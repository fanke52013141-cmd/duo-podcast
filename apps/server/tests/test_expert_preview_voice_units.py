from copy import deepcopy
import pytest
from duocast.services.expert_preview import timeline_turns


def project():
    return {'currentDraftRevision': 'R', 'scriptRevisions': [{'id': 'R', 'turns': [
        {'id': 'A1', 'speaker': 'A'}, {'id': 'B1', 'speaker': 'B'}]}],
        'audioTimeline': {'revisionId': 'R', 'unitOffsets': {'u1': 0, 'u2': 120, 'u3': 240},
            'units': [{'id': uid, 'turnId': turn, 'sampleCount': 100, 'adoptedAudioAssetId': uid}
                      for uid, turn in [('u1', 'A1'), ('u2', 'A1'), ('u3', 'B1')]]}}


def test_multiple_voice_units_become_one_complete_turn_in_sample_order():
    data = project()
    expected = [{'id': 'A1', 'speaker': 'A', 'startSample': 0, 'endSample': 220},
                {'id': 'B1', 'speaker': 'B', 'startSample': 240, 'endSample': 340}]
    assert timeline_turns(data) == expected
    data['audioTimeline']['units'].reverse()
    assert timeline_turns(data) == expected


@pytest.mark.parametrize('case', ['missing', 'overlap', 'interleaved'])
def test_incomplete_or_ambiguous_voice_clock_rejected(case):
    data = deepcopy(project())
    if case == 'missing':
        data['audioTimeline']['units'].pop()
    elif case == 'overlap':
        data['audioTimeline']['unitOffsets']['u2'] = 50
    else:
        data['audioTimeline']['units'][1]['turnId'] = 'B1'
        data['audioTimeline']['units'][2]['turnId'] = 'A1'
    with pytest.raises(ValueError):
        timeline_turns(data)
