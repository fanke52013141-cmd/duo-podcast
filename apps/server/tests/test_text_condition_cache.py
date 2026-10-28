import importlib
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]/'scripts/digital-human'))
module = importlib.import_module('text_condition_cache')


def test_single_entry_reuse_does_not_reuse_downstream_mutation():
    cache = module.TextConditionCache()
    first = cache.call('same', lambda: {'embeds': [1, 2]})
    first['embeds'][0] = 99
    second = cache.call('same', lambda: pytest.fail('Repeated text recomputed'))
    assert second == {'embeds': [1, 2]}
    second['embeds'].append(3)
    assert cache.call('same', lambda: None) == {'embeds': [1, 2]}
    assert (cache.hits, cache.misses) == (2, 1)


def test_failed_new_condition_cannot_publish_stale_cache():
    cache = module.TextConditionCache()
    cache.call('old', lambda: [1])
    def failed():
        raise RuntimeError('encoding failed')
    with pytest.raises(RuntimeError):
        cache.call('new', failed)
    assert cache.key is None and cache.value is None
    assert cache.call('old', lambda: [2]) == [2]


def test_wrapper_invalidates_model_prompt_and_precision_and_skips_extender(tmp_path):
    model = tmp_path/'model';model.write_bytes(b'first')
    class Encoder:
        calls = 0
        def process(self, model_name, precision, positive_prompt, negative_prompt,
                    quantization='disabled', use_disk_cache=False, device='gpu', extender_args=None):
            self.calls += 1
            return {'value': [self.calls]}
    cache = module.wrap_encoder(Encoder, lambda name: model)
    encoder = Encoder()
    args = ['model', 'bf16', 'positive', 'negative']
    assert encoder.process(*args) == encoder.process(*args) == {'value': [1]}
    model.write_bytes(b'changed weights')
    assert encoder.process(*args) == {'value': [2]}
    assert encoder.process('model', 'fp32', 'positive', 'negative') == {'value': [3]}
    assert encoder.process('model', 'fp32', 'different', 'negative') == {'value': [4]}
    encoder.process(*args, extender_args={'seed': 1})
    encoder.process(*args, extender_args={'seed': 1})
    assert encoder.calls == 6 and cache.hits == 1
