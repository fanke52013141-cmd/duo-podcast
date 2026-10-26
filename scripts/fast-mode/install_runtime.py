"""Install versioned worker code into an existing external model runtime."""
import argparse
import json
import shutil
from pathlib import Path

def install(source, target):
    target=target.resolve()
    if not (target/'models').is_dir() or not (target/'assets').is_dir():
        raise ValueError('Target must be an existing fast-mode runtime containing models/ and assets/')
    files={source/'worker.py':'worker.py',source/'render_geometry.py':'render_geometry.py',source/'motion_worker.py':'motion_worker.py',
           source.parents[1]/'apps/server/duocast/services/fast_quality.py':'quality.py'}
    for origin,name in files.items():
        if not origin.is_file():raise ValueError(f'Missing source: {origin}')
        destination=target/name
        if destination.is_symlink():raise ValueError('Refuse symlink destination')
        shutil.copy2(origin,destination)
    (target/'runtime_version.json').write_text(json.dumps({'implementation':'deferred-scale-v2','files':list(files.values())},indent=2),encoding='utf-8')
    return target

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',required=True,type=Path)
    args=parser.parse_args()
    print(install(Path(__file__).resolve().parent,args.runtime))
