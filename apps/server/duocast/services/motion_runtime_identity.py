"""Freeze server-selected runtime code/models when a motion render is queued."""
import asyncio

from ..adapters.musetalk_video import require_motion_engine
from .fast_runtime import runtime_identity


async def freeze_motion_runtime():
    root = require_motion_engine()
    identity = await asyncio.to_thread(runtime_identity, root)
    return {'root': str(root), 'identity': identity, 'protocol': 'motion-runtime-v1'}
