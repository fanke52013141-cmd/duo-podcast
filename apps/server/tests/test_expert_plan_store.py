import pytest
from duocast.storage.expert_plans import ExpertPlanStore


def test_plans_survive_restart_and_are_isolated_by_project(tmp_path):
    store = ExpertPlanStore(tmp_path)
    plan = {'planId': 'EP-'+'a'*24, 'projectId': 'one', 'modelCallsSubmitted': 0}
    store.save(plan)
    store.save(plan)
    store.save({**plan, 'projectId': 'two'})
    assert ExpertPlanStore(tmp_path).list_for('one') == [plan]
    assert len(store.list_for('two')) == 1
    assert store.list_for('missing') == []
    assert not list(tmp_path.glob('*.tmp'))


def test_path_input_is_never_accepted(tmp_path):
    with pytest.raises(ValueError):
        ExpertPlanStore(tmp_path).save({'planId': '../outside', 'projectId': 'one'})
