"""Measure explicitly configured *local* expert dependencies; never attest a server.

Only server-owned configuration belongs at this boundary. A digest of local
files does not prove that a ComfyUI HTTP process actually loaded those files.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath

SCHEMA = 'expert-engine-local-measurement-v1'
_MODEL_NODES = {
    '120': ('MultiTalkModelLoader', 'model', False),
    '122': ('WanVideoModelLoader', 'model', False),
    '129': ('WanVideoVAELoader', 'model_name', False),
    '135': ('WanVideoTextEncodeCached', 'model_name', False),
    '137': ('DownloadAndLoadWav2VecModel', 'model', True),
    '138': ('WanVideoLoraSelect', 'lora', False),
    '232': ('CLIPVisionLoader', 'clip_name', False),
}
_REQUIRED_ENGINE_FILES = {'main.py', 'nodes.py', 'folder_paths.py'}
_MAX_FILES = 100_000
_SUPPORTED_NODE_CLASSES = {
    'MultiTalkModelLoader', 'WanVideoModelLoader', 'WanVideoVAELoader', 'LoadImage',
    'WanVideoBlockSwap', 'WanVideoTextEncodeCached', 'DownloadAndLoadWav2VecModel',
    'WanVideoLoraSelect', 'WanVideoImageToVideoEncode', 'WanVideoClipVisionEncode',
    'MultiTalkWav2VecEmbeds', 'WanVideoSampler', 'WanVideoDecode',
    'LayerUtility: ImageScaleByAspectRatio V2', 'VHS_LoadAudioUpload', 'VHS_VideoCombine',
    'CLIPVisionLoader', 'ImageToMask', 'BatchMasksNode', 'VHS_LoadVideo', 'WanVideoEncode',
}


def _relative(value):
    if not isinstance(value, str) or not value or ':' in value or '\x00' in value:
        raise ValueError('Invalid configured relative path')
    path = PurePosixPath(value.replace('\\', '/'))
    if path.is_absolute() or any(part in ('', '.', '..') for part in path.parts):
        raise ValueError('Configured path must stay below its declared root')
    return path


def _linked(path):
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


def _root(value):
    if not isinstance(value, str) or not value:
        raise ValueError('A server-owned absolute dependency root is required')
    root = Path(value)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('Dependency root must be an existing absolute directory')
    root = Path(os.path.abspath(root))
    if any(_linked(part) for part in (root, *root.parents)):
        raise ValueError('Dependency root must not use symlinks or junctions')
    return root


def _path(root, relative):
    rel = _relative(relative)
    path = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts:
        current = current / part
        if _linked(current):
            raise ValueError('Linked dependency is not measurable')
    if not path.exists() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Missing or escaped expert dependency')
    return path


def _file(path):
    if not path.is_file() or _linked(path):
        raise ValueError('Expert dependency must be an ordinary file')
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('Dependency changed while measuring')
    return {'sha256': digest.hexdigest(), 'bytes': after.st_size}


def _tree(path):
    if not path.is_dir() or _linked(path):
        raise ValueError('Expert dependency tree must be an ordinary directory')
    result = {}
    pending = [path]
    while pending:
        directory = pending.pop()
        for entry in directory.iterdir():
            if _linked(entry):
                raise ValueError('Linked file in expert dependency tree')
            if entry.is_dir():
                # Bytecode and transient caches do not define source identity.
                if entry.name not in {'__pycache__', '.git'}:
                    pending.append(entry)
            elif entry.is_file():
                if entry.suffix.lower() not in {'.pyc', '.pyo'}:
                    result[entry.relative_to(path).as_posix()] = _file(entry)
                    if len(result) > _MAX_FILES:
                        raise ValueError('Expert dependency tree exceeds file limit')
            else:
                raise ValueError('Unsupported special expert dependency')
    if not result:
        raise ValueError('Expert dependency tree is empty')
    return dict(sorted(result.items()))


def measure_expert_engine(workflow, *, engine_root, model_sources, node_sources,
                          runtime_files, runtime_trees, engine_files=None):
    """Hash all explicitly mapped dependencies and return a non-attesting record.

    Each model source is {root: absolute server path, path: root-relative path}.
    Node source paths and engine files are relative to engine_root. runtime_files
    and runtime_trees are explicit lists of absolute-root/relative-path pairs.
    Runtime configuration must include at least the interpreter and dependency
    environment; this routine cannot verify an unmeasured running process.
    """
    if not isinstance(workflow, dict) or not workflow:
        raise ValueError('Frozen expert workflow is required')
    engine = _root(engine_root)
    if not isinstance(model_sources, dict) or set(model_sources) != set(_MODEL_NODES):
        raise ValueError('All seven expert model nodes require explicit file mappings')
    models = {}
    for id, (node_class, field, directory) in _MODEL_NODES.items():
        node = workflow.get(id, {})
        if node.get('class_type') != node_class or not isinstance(node.get('inputs'), dict):
            raise ValueError('Frozen expert model node missing or changed')
        value = node['inputs'].get(field)
        expected = _relative(value)
        item = model_sources[id]
        if not isinstance(item, dict) or set(item) != {'root', 'path'}:
            raise ValueError('Each expert model requires root and relative path')
        actual = _relative(item['path'])
        if len(actual.parts) < len(expected.parts) or actual.parts[-len(expected.parts):] != expected.parts:
            raise ValueError('Expert model mapping does not match workflow model name')
        source = _path(_root(item['root']), item['path'])
        measured = _tree(source) if directory else _file(source)
        models[id] = {'class': node_class, 'workflowName': value, 'files': measured}

    classes = set()
    for node in workflow.values():
        if not isinstance(node, dict) or not isinstance(node.get('class_type'), str) or not isinstance(node.get('inputs'), dict):
            raise ValueError('Malformed expert workflow node')
        classes.add(node['class_type'])
    if classes - _SUPPORTED_NODE_CLASSES:
        raise ValueError('Unsupported expert node class requires dependency-policy review')
    if not isinstance(node_sources, dict) or set(node_sources) != classes:
        raise ValueError('Every expert node class needs a server-configured implementation path')
    implementations = {}
    measured_sources = {}
    for name, rel in sorted(node_sources.items()):
        source = _path(engine, rel)
        if source not in measured_sources:
            measured_sources[source] = _tree(source) if source.is_dir() else _file(source)
        implementations[name] = {'path': _relative(rel).as_posix(),
                                 'files': measured_sources[source]}
    engine_files = list(engine_files or [])
    if not _REQUIRED_ENGINE_FILES.issubset(set(engine_files)):
        raise ValueError('Comfy main.py, nodes.py and folder_paths.py must be measured')
    core = {rel: _file(_path(engine, rel)) for rel in sorted(set(engine_files))}
    core_tree = _tree(_path(engine, 'comfy'))

    if (not isinstance(runtime_files, list) or not any(
            isinstance(item, dict) and isinstance(item.get('path'), str)
            and re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)?)?(?:\.exe)?',
                             PurePosixPath(item['path'].replace('\\', '/')).name, re.I)
            for item in runtime_files)):
        raise ValueError('Runtime file mappings must include the Python interpreter')

    def runtime_entries(entries, *, directory):
        if not isinstance(entries, list) or not entries:
            raise ValueError('Explicit interpreter and runtime dependency mappings are required')
        result = []
        for item in entries:
            if not isinstance(item, dict) or set(item) != {'root', 'path'}:
                raise ValueError('Runtime mappings require root and relative path')
            path = _path(_root(item['root']), item['path'])
            result.append({'path': _relative(item['path']).as_posix(),
                           'files': _tree(path) if directory else _file(path)})
        return sorted(result, key=lambda value: value['path'])

    payload = {'schemaVersion': SCHEMA, 'models': models, 'nodeImplementations': implementations,
               'engineFiles': core, 'comfyCoreTree': core_tree, 'runtimeFiles': runtime_entries(runtime_files, directory=False),
               'runtimeTrees': runtime_entries(runtime_trees, directory=True)}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                            allow_nan=False).encode()).hexdigest()
    return {**payload, 'fingerprint': fingerprint, 'dependencyCoverageVerified': False,
            'engineBindingVerified': False, 'inferenceAuthorized': False,
            'notice': 'Configured files only; complete transitive dependencies and the running HTTP process are not attested.'}
