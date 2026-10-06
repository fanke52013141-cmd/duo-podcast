"""Isolated runtime shim: immutable CPU weight references avoid D2H block copies."""
import os
from pathlib import Path
import runpy
import sys
import json
import math
import struct
import mmap
import warnings
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(os.environ.get("DUOCAST_ENGINE_HOME", str(Path(__file__).resolve().parent))).resolve()
COMFY = Path(os.environ['DUOCAST_COMFY_ROOT'])
# Configure before importing torch; do not change the shared environment.
os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['CUDA_LAUNCH_BLOCKING'] = '1'
os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'backend:native'
import torch

original_to = torch.nn.Module.to

def guarded_to(self, *args, **kwargs):
    if type(self).__name__ != 'WanAttentionBlock':
        return original_to(self, *args, **kwargs)
    device, dtype, _, _ = torch._C._nn._parse_to(*args, **kwargs)
    if device is None or dtype is not None:
        return original_to(self, *args, **kwargs)
    cache = getattr(self, '_duocast_cpu_weights', None)
    parameters = list(self.parameters())
    if cache is None and parameters and all(p.device.type == 'cpu' for p in parameters):
        # detach holds the immutable CPU storage independently of Parameter.data.
        cache = [(module, {k: (p.detach(), p.requires_grad) for k, p in module._parameters.items() if p is not None},
                  {k: b for k, b in module._buffers.items() if b is not None})
                 for module in self.modules()]
        self._duocast_cpu_weights = cache
    if device.type == 'cpu' and cache is not None:
        for module, weights, buffers in cache:
            for name, (tensor, requires_grad) in weights.items():
                module._parameters[name] = torch.nn.Parameter(tensor, requires_grad=requires_grad)
            for name, tensor in buffers.items():
                # The LoRA schedule counter is mutable; preserve its current value.
                if name == '_step' and module._buffers[name].device.type != 'cpu':
                    tensor = module._buffers[name].cpu()
                module._buffers[name] = tensor
        return self
    return original_to(self, *args, **kwargs)

torch.nn.Module.to = guarded_to
print('DuoCast immutable CPU block weight guard enabled', flush=True)
os.chdir(COMFY)
sys.path.insert(0, str(COMFY))
sys.argv = ['main.py', '--listen', '127.0.0.1', '--port', '8191',
    '--input-directory', str(ROOT/'input'), '--output-directory', str(ROOT/'output'),
    '--temp-directory', str(ROOT/'temp'), '--user-directory', str(ROOT/'user'),
    '--database-url', 'sqlite:///' + (ROOT/'user/comfyui_isolated.db').as_posix(),
    '--disable-auto-launch', '--disable-async-offload', '--disable-dynamic-vram',
    '--disable-mmap', '--disable-cuda-malloc', '--disable-pinned-memory']
import comfy.options
comfy.options.enable_args_parsing()
import comfy.utils
from safetensors.torch import _TYPES
original_load = comfy.utils.load_torch_file

def owned_tensor_load(ckpt, safe_load=False, device=None, return_metadata=False):
    if not str(ckpt).lower().endswith(('.safetensors', '.sft')):
        return original_load(ckpt, safe_load=safe_load, device=device, return_metadata=return_metadata)
    result = {}
    with open(ckpt, 'rb') as file:
        header_size = struct.unpack('<Q', file.read(8))[0]
        if header_size > 100_000_000:
            raise ValueError('Invalid safetensors header length')
        header = json.loads(file.read(header_size))
        base = 8 + header_size
        file_size = os.fstat(file.fileno()).st_size
        mapped = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
        for name, info in header.items():
            if name == '__metadata__':
                continue
            start, end = info['data_offsets']
            dtype = _TYPES[info['dtype']]
            count = math.prod(info['shape'])
            if start < 0 or end < start or base+end > file_size or end-start != count*dtype.itemsize:
                raise ValueError('Invalid tensor offsets: ' + name)
            if count:
                view = memoryview(mapped)[base+start:base+end]
                with warnings.catch_warnings():
                    warnings.filterwarnings('ignore', message='The given buffer is not writable')
                    tensor = torch.frombuffer(view, dtype=dtype).reshape(info['shape'])
            else:
                tensor = torch.empty(info['shape'], dtype=dtype)
            if device is not None and torch.device(device).type != 'cpu':
                tensor = tensor.to(device)
            result[name] = tensor
    print('Loaded retained Python buffer tensor storage: ' + Path(ckpt).name, flush=True)
    return (result, header.get('__metadata__', {})) if return_metadata else result

comfy.utils.load_torch_file = owned_tensor_load
runpy.run_path(str(COMFY/'main.py'), run_name='__main__')
