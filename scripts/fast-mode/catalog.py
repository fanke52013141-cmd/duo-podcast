"""Inspect/register a reviewed, complete fast runtime without running GPU inference."""
import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps' / 'server'))
from duocast.adapters.musetalk_video import require_engine, template_identity


def register(runtime, output, label, reviewed=False):
    spec = importlib.util.spec_from_file_location('template_validation', Path(__file__).with_name('validate_template.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    root = require_engine(str(runtime))
    inspection = module.validate(root)
    entry = {**template_identity(root), 'label': label, 'reviewed': reviewed, 'inspection': inspection,
             'qualityChecks': ['身份', '口型', '牙齿与手部', '闭口倾听', '跨段时序'],
             'notice': '原生尺寸不是感知清晰度证明；reviewed 仅记录人工审看声明'}
    output.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(output.read_text(encoding='utf-8')) if output.exists() else {'templates': []}
    data['templates'] = [e for e in data['templates'] if e['id'] != entry['id']] + [entry]
    fd, temporary = tempfile.mkstemp(dir=output.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return entry


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--reviewed', action='store_true', help='Only set after watching identity, lip sync, listening and continuity.')
    args = parser.parse_args()
    print(json.dumps(register(args.runtime, args.catalog, args.label, args.reviewed), ensure_ascii=False, indent=2))
