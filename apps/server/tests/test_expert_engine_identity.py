from pathlib import Path

import pytest

from duocast.services.expert_engine_identity import measure_expert_engine


@pytest.fixture
def configured(tmp_path):
    engine, models, runtime = (tmp_path / name for name in ('engine', 'models', 'runtime'))
    for folder in (engine, models, runtime):
        folder.mkdir()
    for name in ('main.py', 'nodes.py', 'folder_paths.py'):
        (engine / name).write_text('core ' + name)
    (engine / 'nodeimpl').mkdir()
    (engine / 'nodeimpl' / 'node.py').write_text('node version one')
    (engine / 'comfy').mkdir()
    (engine / 'comfy' / 'sampler.py').write_text('sampler version one')
    (runtime / 'python.exe').write_bytes(b'python test executable')
    (runtime / 'packages').mkdir()
    (runtime / 'packages' / 'version.json').write_text('{"versions":"one"}')
    definitions = {
        '120': ('MultiTalkModelLoader', 'model', 'InfiniteTalk/model.safetensors'),
        '122': ('WanVideoModelLoader', 'model', 'model.safetensors'),
        '129': ('WanVideoVAELoader', 'model_name', 'vae.safetensors'),
        '135': ('WanVideoTextEncodeCached', 'model_name', 't5.safetensors'),
        '137': ('DownloadAndLoadWav2VecModel', 'model', 'TencentGameMate/chinese-wav2vec2-base'),
        '138': ('WanVideoLoraSelect', 'lora', 'lora.safetensors'),
        '232': ('CLIPVisionLoader', 'clip_name', 'clip.safetensors'),
    }
    workflow = {}
    mappings = {}
    for id, (cls, key, name) in definitions.items():
        node_path = models / id / name
        if id == '137':
            node_path.mkdir(parents=True)
            (node_path / 'config.json').write_text('{"model":"wav2vec"}')
            (node_path / 'model.safetensors').write_bytes(b'wav2vec-weights')
        else:
            node_path.parent.mkdir(parents=True, exist_ok=True)
            node_path.write_bytes(('weights ' + id).encode())
        workflow[id] = {'class_type': cls, 'inputs': {key: name}}
        mappings[id] = {'root': str(models), 'path': id + '/' + name}
    workflow['image'] = {'class_type': 'LoadImage', 'inputs': {'image': 'owned.png'}}
    config = {'engine_root': str(engine), 'model_sources': mappings,
        'node_sources': {node['class_type']: 'nodeimpl' for node in workflow.values()},
        'runtime_files': [{'root': str(runtime), 'path': 'python.exe'}],
        'runtime_trees': [{'root': str(runtime), 'path': 'packages'}],
        'engine_files': ['main.py', 'nodes.py', 'folder_paths.py']}
    return workflow, config, engine, models, runtime


def test_same_complete_local_inputs_produce_same_fingerprint_and_are_not_engine_attestation(configured):
    workflow, config, *_ = configured
    first = measure_expert_engine(workflow, **config)
    second = measure_expert_engine(workflow, **config)
    assert first['fingerprint'] == second['fingerprint'] and len(first['fingerprint']) == 64
    assert first['engineBindingVerified'] is False
    assert first['dependencyCoverageVerified'] is False and first['inferenceAuthorized'] is False
    assert len(first['models']) == 7 and 'model.safetensors' in first['models']['137']['files']


@pytest.mark.parametrize('relative', [
    ('models', '122/model.safetensors'),
    ('models', '137/TencentGameMate/chinese-wav2vec2-base/config.json'),
    ('engine', 'nodeimpl/node.py'), ('engine', 'folder_paths.py'),
    ('engine', 'comfy/sampler.py'),
    ('runtime', 'python.exe'), ('runtime', 'packages/version.json'),
])
def test_model_node_engine_and_environment_changes_each_invalidate(configured, relative):
    workflow, config, engine, models, runtime = configured
    original = measure_expert_engine(workflow, **config)['fingerprint']
    roots = {'engine': engine, 'models': models, 'runtime': runtime}
    path = roots[relative[0]] / relative[1]
    path.write_bytes(path.read_bytes() + b'changed')
    assert measure_expert_engine(workflow, **config)['fingerprint'] != original


def test_missing_model_unknown_node_and_incomplete_core_are_rejected(configured):
    workflow, config, *_ = configured
    del config['model_sources']['120']
    with pytest.raises(ValueError, match='seven'):
        measure_expert_engine(workflow, **config)
    config['model_sources']['120'] = {'root': config['model_sources']['122']['root'], 'path': '120/InfiniteTalk/model.safetensors'}
    workflow['new'] = {'class_type': 'NewUnmappedNode', 'inputs': {}}
    with pytest.raises(ValueError, match='node class'):
        measure_expert_engine(workflow, **config)
    del workflow['new']
    config['engine_files'].remove('nodes.py')
    with pytest.raises(ValueError, match='nodes.py'):
        measure_expert_engine(workflow, **config)


@pytest.mark.parametrize('path', ['../nodeimpl/node.py', 'C:/foreign.py', '/absolute', 'missing'])
def test_invalid_or_missing_implementation_path_is_rejected(configured, path):
    workflow, config, *_ = configured
    config['node_sources']['LoadImage'] = path
    with pytest.raises(ValueError):
        measure_expert_engine(workflow, **config)


def test_wrong_model_file_and_missing_runtime_mapping_are_rejected(configured):
    workflow, config, *_ = configured
    config['model_sources']['120']['path'] = '122/model.safetensors'
    with pytest.raises(ValueError, match='model name'):
        measure_expert_engine(workflow, **config)
    config['model_sources']['120']['path'] = '120/InfiniteTalk/model.safetensors'
    config['runtime_trees'] = []
    with pytest.raises(ValueError, match='runtime dependency'):
        measure_expert_engine(workflow, **config)


def test_runtime_measurement_requires_an_actual_interpreter_mapping(configured):
    workflow, config, _, _, runtime = configured
    (runtime / 'not-python.bin').write_bytes(b'test')
    config['runtime_files'] = [{'root': str(runtime), 'path': 'not-python.bin'}]
    with pytest.raises(ValueError, match='interpreter'):
        measure_expert_engine(workflow, **config)


def test_linked_source_is_refused(configured, tmp_path):
    workflow, config, engine, *_ = configured
    link = engine / 'linked'
    try:
        link.symlink_to(engine / 'nodeimpl', target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlinks unavailable on this machine')
    config['node_sources']['LoadImage'] = 'linked'
    with pytest.raises(ValueError, match='Linked'):
        measure_expert_engine(workflow, **config)
