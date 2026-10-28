"""Explicit, hash-bound SageAttention package for isolated local experiments."""
import functools
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

_STATE = None


def describe_package(site):
    site = Path(site).resolve(strict=True)
    package = site/'sageattention'
    files = sorted(p for p in package.rglob('*') if p.suffix in {'.py', '.pyd'})
    if not (package/'__init__.py').is_file() or not 1 <= len(files) <= 256:
        raise ValueError('Use a complete isolated sageattention package')
    records = {}
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(package.resolve()):
            raise ValueError('Attention package must not reference external code')
        if path.stat().st_size > 256*1024*1024:
            raise ValueError('Oversized attention package file')
        records[path.relative_to(package).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    return {'path': str(site), 'treeHash': digest, 'files': records}


def load_package(environment):
    global _STATE
    encoded = environment.get('DUOCAST_ATTENTION_ADDON')
    if not encoded:
        return None
    expected = json.loads(encoded)
    observed = describe_package(expected['path'])
    if observed['treeHash'] != expected['treeHash']:
        raise ValueError('Isolated attention package changed after planning')
    if 'sageattention' in sys.modules:
        raise ValueError('Attention package already imported before binding')
    source = Path(observed['path'])/'sageattention/__init__.py'
    spec = importlib.util.spec_from_file_location('sageattention', source,
                submodule_search_locations=[str(source.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules['sageattention'] = module
    spec.loader.exec_module(module)
    _STATE = {'path': str(source.resolve()), 'treeHash': observed['treeHash'], 'calls': 0}
    original = module.sageattn

    @functools.wraps(original)
    def measured(*args, **kwargs):
        result = original(*args, **kwargs)
        _STATE['calls'] += 1  # Successful native calls only; no silent SDPA fallback.
        return result

    module.sageattn = measured
    return _STATE


def loaded_identity():
    return _STATE
