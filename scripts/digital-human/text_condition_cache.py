"""One-entry process-local text condition cache for opt-in experiments only."""
from copy import deepcopy
import functools
import hashlib
import inspect
import json
import time
from pathlib import Path


class TextConditionCache:
    def __init__(self):
        self.key = None
        self.value = None
        self.hits = 0
        self.misses = 0
        self.computeWallSeconds = 0.0
        self.copyWallSeconds = 0.0
        self.modelHashWallSeconds = 0.0

    def call(self, key, compute):
        if key != self.key:
            # Clear stale state before computation; a failure must not publish a new entry.
            self.key, self.value = None, None
            start = time.perf_counter()
            value = compute()
            self.computeWallSeconds += time.perf_counter()-start
            start = time.perf_counter()
            self.value = deepcopy(value)
            self.copyWallSeconds += time.perf_counter()-start
            self.key = key
            self.misses += 1
            return value
        self.hits += 1
        start = time.perf_counter()
        value = deepcopy(self.value)  # Downstream mutation cannot poison subsequent windows.
        self.copyWallSeconds += time.perf_counter()-start
        return value


def wrap_encoder(cls, resolve_model):
    original = cls.process
    signature = inspect.signature(original)
    source_hash = hashlib.sha256(Path(inspect.getfile(cls)).read_bytes()).hexdigest()
    cache = TextConditionCache()
    model_identity = {}

    @functools.wraps(original)
    def process(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        values = {k: v for k, v in bound.arguments.items() if k != 'self'}
        # Prompt extenders are stateful and need their own identity; do not reuse them.
        if values.get('extender_args') is not None or values.get('use_disk_cache'):
            return original(self, *args, **kwargs)
        model = Path(resolve_model(values['model_name'])).resolve(strict=True)
        stat = model.stat()
        stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if model_identity.get('stamp') != (str(model), stamp):
            hash_started = time.perf_counter()
            digest = hashlib.sha256()
            with model.open('rb') as stream:
                for block in iter(lambda: stream.read(1024*1024), b''):
                    digest.update(block)
            after = model.stat()
            if stamp != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('Text model changed while hashing')
            model_identity.update(stamp=(str(model), stamp), sha256=digest.hexdigest())
            cache.modelHashWallSeconds += time.perf_counter()-hash_started
        key = json.dumps({'arguments': values, 'nodeSourceHash': source_hash,
            'modelPath': str(model), 'modelSha256': model_identity['sha256'],
            'modelBytes': stat.st_size, 'modelMtimeNs': stat.st_mtime_ns},
            sort_keys=True)
        return cache.call(key, lambda: original(self, *args, **kwargs))

    cls.process = process
    return cache


def install_process_cache(server_class):
    original = server_class.add_routes
    status = {'enabled': True, 'installed': False, 'hits': 0, 'misses': 0}
    cache = None

    @functools.wraps(original)
    def add_routes(self, *args, **kwargs):
        nonlocal cache
        import nodes
        import folder_paths
        cls = nodes.NODE_CLASS_MAPPINGS['WanVideoTextEncodeCached']
        cache = wrap_encoder(cls, lambda name: folder_paths.get_full_path_or_raise('text_encoders', name))
        status['installed'] = True
        return original(self, *args, **kwargs)

    server_class.add_routes = add_routes

    def snapshot():
        return {**status, 'hits': cache.hits if cache else 0, 'misses': cache.misses if cache else 0,
                'modelHashWallSeconds': cache.modelHashWallSeconds if cache else 0,
                'encoderCallHostWallSeconds': cache.computeWallSeconds if cache else 0,
                'deepCopyHostWallSeconds': cache.copyWallSeconds if cache else 0,
                'timingScope': 'host wall time, no extra CUDA synchronization; not isolated GPU kernel timing'}
    return snapshot
