"""Cooperative task boundaries: stop dispatch and adoption, retain produced files."""
import hashlib
import json


class JobInterrupted(Exception):
    pass


class ControlledProvider:
    def __init__(self, provider, checkpoint, completed=None, remember=None):
        self._provider = provider
        self._checkpoint = checkpoint
        self._completed = completed if completed is not None else {}
        self._remember = remember

    def __getattr__(self, name):
        target = getattr(self._provider, name)
        if name not in {"generate", "rewrite", "synthesize", "audition", "render", "submit"}:
            return target

        async def call(*args, **kwargs):
            self._checkpoint()
            key = hashlib.sha256(json.dumps([name, args, {k:v for k,v in kwargs.items() if k != 'progress'}], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if key in self._completed:
                result = self._completed[key]
            else:
                result = await target(*args, **kwargs)
                if isinstance(result, dict):
                    self._completed[key] = result
                    if self._remember:
                        self._remember()
            self._checkpoint()
            return result
        return call
