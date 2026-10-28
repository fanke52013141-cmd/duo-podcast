"""No model call: sample navigation must work for either opening speaker and short scripts."""
from duocast.domain.project import Project
from duocast.services.render_spec import representative_range
import pytest


@pytest.mark.parametrize('speakers,expected',[
    (['A','B','A'],(0,9)),
    (['B','A','B'],(0,9)),
    (['A','B'],(0,6)),
])
def test_server_range_covers_alternation_or_short_script_without_requiring_three_turns(speakers,expected):
    project=Project.model_validate({'id':'range-test','currentDraftRevision':'R1',
        'scriptRevisions':[{'id':'R1','turns':[{'id':f'T{i}','speaker':role} for i,role in enumerate(speakers)]}],
        'audioTimeline':{'revisionId':'R1','sampleRate':48000,'sampleCount':len(speakers)*144000,
            'units':[{'id':f'U{i}','turnId':f'T{i}','sampleCount':144000} for i in range(len(speakers))],
            'unitOffsets':{f'U{i}':i*144000 for i in range(len(speakers))}}})
    result=representative_range(project)
    assert (result['start'],result['end'])==expected
    assert result['reason']
