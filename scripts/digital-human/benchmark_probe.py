"""Opt-in instrumentation for an isolated local benchmark engine."""
import functools
import json
import os
import sys
import threading
import time
from pathlib import Path


def launch_options(environment):
    port = int(environment.get('DUOCAST_ENGINE_PORT', '8191'))
    blocking = environment.get('DUOCAST_CUDA_BLOCKING', '1')
    if not 1024 <= port <= 65535 or blocking not in {'0', '1'}:
        raise ValueError('Invalid benchmark port or CUDA blocking value')
    return port, blocking


def prompt_worker_alive():
    return any('prompt_worker' in thread.name and thread.is_alive() for thread in threading.enumerate())


def install_probe(root, torch):
    import server
    from aiohttp import web
    identity = json.loads(os.environ['DUOCAST_BENCH_IDENTITY'])
    cache_snapshot = lambda: {'enabled': False, 'installed': False, 'hits': 0, 'misses': 0}
    if identity.get('textConditionCache'):
        from text_condition_cache import install_process_cache
        cache_snapshot = install_process_cache(server.PromptServer)
    from attention_addon import loaded_identity
    identity['attentionAddon'] = loaded_identity()
    identity.update(processId=os.getpid(), cudaLaunchBlocking=os.environ['CUDA_LAUNCH_BLOCKING'],
                    pythonVersion=sys.version.split()[0], torchVersion=str(torch.__version__),
                    cudaVersion=torch.version.cuda, gpu=torch.cuda.get_device_name(0))
    original_init = server.PromptServer.__init__

    @functools.wraps(original_init)
    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        async def inspect_identity(request):
            return web.json_response({**identity, 'promptWorkerAlive': prompt_worker_alive(),
                                      'textCache': cache_snapshot()})

        self.routes.get('/duocast-benchmark/identity')(inspect_identity)

    server.PromptServer.__init__ = initialize
    if not identity.get('profileNodes'):
        return
    import execution
    original = execution.get_output_data
    trace = Path(root) / 'node-timings.jsonl'

    @functools.wraps(original)
    async def measured(prompt_id, unique_id, obj, *args, **kwargs):
        if torch.cuda.is_initialized():
            torch.cuda.synchronize()
        start = time.perf_counter()
        failed = False
        try:
            return await original(prompt_id, unique_id, obj, *args, **kwargs)
        except BaseException:
            failed = True
            raise
        finally:
            record = {'promptId': prompt_id, 'nodeId': unique_id,
                      'className': type(obj).__name__, 'failed': failed}
            try:
                if torch.cuda.is_initialized():
                    torch.cuda.synchronize()
                    record.update(peakAllocatedBytesSinceEngineStart=torch.cuda.max_memory_allocated(),
                                  peakReservedBytesSinceEngineStart=torch.cuda.max_memory_reserved())
                import psutil
                record['processMemory'] = psutil.Process().memory_info()._asdict()
            except Exception as exc:
                record['measurementError'] = type(exc).__name__
            record['wallSeconds'] = time.perf_counter() - start
            # A diagnostic write failure must not replace the original model error.
            try:
                with trace.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record) + '\n')
            except OSError:
                pass

    execution.get_output_data = measured
