"""A screening subset must never be presented as a complete generated episode."""
import importlib
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]/'scripts/acceptance'))
module = importlib.import_module('assemble_expert_benchmark')


@pytest.mark.parametrize('state', [
    {'completed': False},
    {'completed': True, 'errorType': 'Stopped'},
    {'completed': True, 'plan': {'segments': [{'index': 0}, {'index': 2}]}},
])
def test_incomplete_or_screening_runs_never_start_assembly(tmp_path, monkeypatch, state):
    path = tmp_path/'state.json'
    path.write_text(json.dumps(state), encoding='utf-8')
    request = tmp_path/'request.json'
    request.write_text(json.dumps({'expertPlan': {'segments': [0, 1, 2]}}), encoding='utf-8')
    command = Mock(side_effect=AssertionError('No encoding before full validation'))
    monkeypatch.setattr(module.subprocess, 'run', command)
    with pytest.raises(ValueError):
        module.assemble(path, request, tmp_path/'reference.mp4', tmp_path/'out.mp4')
    command.assert_not_called()


@pytest.mark.parametrize('results', [[], [{'index':0},{'index':0}], [{'index':1}]])
def test_missing_duplicate_or_reordered_results_rejected(tmp_path, results):
    state={'completed':True,'plan':{'segments':[{'index':0}]},'results':results}
    request={'expertPlan':{'segments':[{}]}}
    path=tmp_path/'state.json';path.write_text(json.dumps(state),encoding='utf-8')
    source=tmp_path/'request.json';source.write_text(json.dumps(request),encoding='utf-8')
    with pytest.raises(ValueError,match='exactly one ordered result'):
        module.assemble(path,source,tmp_path/'ref.mp4',tmp_path/'out.mp4')


def test_context_and_authoritative_clock_cannot_be_redefined(tmp_path):
    spec={'index':0,'coreFrames':64,'modelFrames':81,'trimStartFrame':8,'trimEndFrame':72}
    original={'outputStartFrame':0,'outputEndFrame':64,'trimStartFrame':8,'trimEndFrame':72}
    state={'completed':True,'plan':{'segments':[spec]},'results':[{'index':0}]}
    request={'sampleRate':48000,'sampleCount':96000,'expertPlan':{'segments':[original]}}
    path=tmp_path/'state.json';path.write_text(json.dumps(state),encoding='utf-8')
    source=tmp_path/'request.json';source.write_text(json.dumps(request),encoding='utf-8')
    with pytest.raises(ValueError,match='authoritative sample clock'):
        module.assemble(path,source,tmp_path/'ref.mp4',tmp_path/'out.mp4')
