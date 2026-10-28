"""CPU-only, hash-bound crop of existing adopted expert inputs; no model calls."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import time

from benchmark_expert import file_hash, read_json
from expert_roi import joint_crop
from run_motion_acceptance import inspect_media
from run_motion_api_acceptance import atomic_json


def prepare(source_plan, request_path, output):
    from PIL import Image
    started = time.perf_counter()
    source_plan, request_path, output = (Path(p).resolve() for p in (source_plan, request_path, output))
    if output.exists():
        raise ValueError('ROI preparation is immutable; choose a new directory')
    source, request = read_json(source_plan), read_json(request_path)
    original = request['expertPlan']['segments']
    if len(source['segments']) != len(original) or request['motionAssets']['style'] != 'twoShot':
        raise ValueError('Only complete two-shot source snapshots supported in this experiment')
    native = original[0]['geometry']['native'][:2]
    crop = joint_crop(original, native)
    left, top, width, height = crop
    output.mkdir(parents=True)
    plan = {'scope': 'experimental joint face ROI; not adopted quality or production route',
            'sampleRate': request['sampleRate'], 'sampleCount': request['sampleCount'],
            'roi': {'crop': crop, 'sourceNative': native, 'sourcePlan': str(source_plan),
                    'sourcePlanHash': file_hash(source_plan), 'request': str(request_path),
                    'requestHash': file_hash(request_path), 'spatialResize': False,
                    'compositeRecipe': 'confirmed-face-ellipses-1.4x-smooth-edge-v1'}, 'segments': []}
    for index, item in enumerate(source['segments']):
        folder = output/f'segment-{index}'; folder.mkdir()
        built = read_json(item['file'])
        graph = deepcopy(built['workflow'])
        if graph['213']['inputs']['scale_to_length'] != native[1]:
            raise ValueError('Source graph does not preserve expected native scale')
        renamed, inputs = {}, {}
        for key, value in item['inputs'].items():
            path = Path(value['path'])
            if file_hash(path) != value['fileHash']:
                raise ValueError('Frozen source changed before ROI preparation')
            if key in {'anchorImage', 'maskA', 'maskB', 'referenceVideo'}:
                target = folder/('roi_'+path.name)
                if key == 'referenceVideo':
                    check = inspect_media(path, fps=25, width=native[0], height=native[1], require_audio=True)
                    if not check['passed'] or check['video']['decodedFrames'] != item['modelFrames']:
                        raise ValueError('Source reference clock differs')
                    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf',
                        f'crop={width}:{height}:{left}:{top}', '-map', '0:v:0', '-map', '0:a:0',
                        '-c:a', 'copy', '-c:v', 'libx264',
                        '-preset', 'fast', '-crf', '0', '-pix_fmt', 'yuv420p', str(target)],
                        check=True, timeout=120)
                    check = inspect_media(target, fps=25, width=width, height=height, require_audio=True)
                    if not check['passed'] or check['video']['decodedFrames'] != item['modelFrames']:
                        raise ValueError('Cropped reference clock differs')
                else:
                    with Image.open(path) as image:
                        if image.size != tuple(native):
                            raise ValueError('Source image/mask native scale differs')
                        image.crop((left, top, left+width, top+height)).save(target)
                renamed[path.name] = target.name
                inputs[key] = {'path': str(target), 'fileHash': file_hash(target)}
            else:
                # Audio is retained byte-for-byte, without slicing again or changing its clock.
                inputs[key] = deepcopy(value)
        for node in graph.values():
            for key, value in node['inputs'].items():
                if isinstance(value, str) and value in renamed:
                    node['inputs'][key] = renamed[value]
        graph['213']['inputs']['scale_to_length'] = height
        workflow_path = folder/'workflow.json'
        atomic_json(workflow_path, {'workflow': graph, 'scope': plan['scope'], 'roi': plan['roi']})
        entry = {**deepcopy(item), 'file': str(workflow_path), 'inputs': inputs,
                 'nativeSize': [width, height], 'fullReference': deepcopy(item['inputs']['referenceVideo'])}
        plan['segments'].append(entry)
    # Fail if another operation changed source metadata while preparation was in progress.
    if file_hash(source_plan) != plan['roi']['sourcePlanHash'] or file_hash(request_path) != plan['roi']['requestHash']:
        raise ValueError('Source snapshot changed while preparing ROI')
    plan['preparationSeconds'] = time.perf_counter()-started
    atomic_json(output/'plan.json', plan)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-plan', required=True, type=Path)
    parser.add_argument('--request', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    result = prepare(args.source_plan, args.request, args.out)
    print(json.dumps({'crop': result['roi']['crop'], 'spatialResize': False,
                      'preparationSeconds': result['preparationSeconds'], 'qualityAccepted': False}))


if __name__ == '__main__':
    main()
