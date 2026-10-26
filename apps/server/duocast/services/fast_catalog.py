"""Versioned fast runtimes: the catalog points at complete, immutable directories."""
import json
import os
from pathlib import Path


def catalog():
    from ..adapters.musetalk_video import engine_root, require_engine, template_identity
    path = os.environ.get('DUOCAST_FAST_CATALOG')
    entries = []
    if path and Path(path).is_file():
        entries = json.loads(Path(path).read_text(encoding='utf-8')).get('templates', [])
    elif engine_root():
        try:
            root = require_engine()
            entries = [{**template_identity(root), 'label': '当前本机模板', 'reviewed': True, 'legacy': True}]
        except ValueError:
            pass
    results = []
    for entry in entries:
        try:
            actual = template_identity(require_engine(entry['runtimeRoot']))
            ready = actual['version'] == entry.get('version') and entry.get('reviewed') is True
            results.append({**entry, 'ready': ready})
        except (ValueError, OSError, KeyError):
            results.append({**entry, 'ready': False})
    return results


def selected_root(template_id=None):
    if not template_id:
        return None
    entry = next((item for item in catalog() if item.get('id') == template_id and item.get('ready')), None)
    if not entry:
        raise ValueError('模板不存在、未审看或文件已改变，请恢复模板后重试')
    return entry['runtimeRoot']
