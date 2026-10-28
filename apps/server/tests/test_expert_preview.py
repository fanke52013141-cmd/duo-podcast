from copy import deepcopy
import pytest
from duocast.services.expert_preview import select_windows


@pytest.fixture
def source():
    turns=[{'id':f'T{i}', 'speaker':'AB'[i%2], 'startSample':i*48000+4800,
            'endSample':(i+1)*48000-4800} for i in range(8)]
    segments=[]
    for i in range(8):
        segments.append({'outputStartFrame':i*25,'outputEndFrame':(i+1)*25,
            'contextStartFrame':max(0,i*25-8),'contextEndFrame':min(200,(i+1)*25+8),
            'modelFrames':41,'trimStartFrame':8,'trimEndFrame':33,
            'seed':i+10,'dependencyHash':str(i)*64})
    return {'sampleRate':48000,'totalSamples':384000,'totalFrames':200,'segments':segments},turns


def test_two_round_preview_reuses_original_window_identity(source):
    plan,turns=source;before=deepcopy(plan)
    result=select_windows(plan,turns)
    assert result['selectedTurnIds']==['T0','T1','T2','T3']
    assert [w['index'] for w in result['windows']]==[0,1,2,3]
    assert [w['segment'] for w in result['windows']]==plan['segments'][:4]
    assert result['displayFrameRange']==[2,98] and result['modelCallsSubmitted']==0
    assert plan==before


def test_mid_episode_preview_never_rebases_clock_or_seed(source):
    plan,turns=source
    result=select_windows(plan,turns,turn_ids=['T4','T5','T6','T7'])
    assert result['windows'][0]['segment']['outputStartFrame']==100
    assert result['windows'][0]['segment']['seed']==14
    assert result['windows'][0]['segment']['contextStartFrame']==92


def test_regeneration_includes_context_neighbors_and_only_changes_selected_window_seeds(source):
    plan,turns=source
    result=select_windows(plan,turns,turn_ids=['T2','T3'],operation='regenerate',candidate_token='candidate-123')
    assert [w['index'] for w in result['windows']]==[1,2,3,4]
    assert result['reviewSampleRange'] == [48000, 240000]
    assert result['selectedSampleRange'] == [100800, 187200]
    assert result['windows'][0]['reason']=='boundary_context'
    for window in result['windows']:
        expected=deepcopy(plan['segments'][window['index']])
        expected.update(seed=window['segment']['seed'],dependencyHash=window['segment']['dependencyHash'])
        assert window['segment']==expected
        assert window['segment']['seed']!=plan['segments'][window['index']]['seed']
    assert result==select_windows(plan,turns,turn_ids=['T2','T3'],operation='regenerate',candidate_token='candidate-123')


@pytest.mark.parametrize('ids',[[],['T0','T2'],['T1','T0'],['T0','T0'],['missing']])
def test_invalid_selection_rejected(source,ids):
    with pytest.raises(ValueError):select_windows(*source,turn_ids=ids)


def test_fewer_than_four_turns_uses_available_content(source):
    plan,turns=source
    assert select_windows(plan,turns[:2])['selectedTurnIds']==['T0','T1']


def test_changed_snapshot_invalidates_preview_plan(source):
    plan,turns=source
    first=select_windows(plan,turns)
    plan['segments'][0]['seed']+=1
    assert select_windows(plan,turns)['planId']!=first['planId']


def test_incomplete_windows_or_missing_candidate_token_fail(source):
    plan,turns=source
    with pytest.raises(ValueError,match='candidate token'):
        select_windows(plan,turns,operation='regenerate')
    plan['segments'].pop()
    with pytest.raises(ValueError,match='cover'):
        select_windows(plan,turns)
