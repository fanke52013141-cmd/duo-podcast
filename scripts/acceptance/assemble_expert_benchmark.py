"""Assemble completed real benchmark windows on their original PCM timeline."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import wave

from run_motion_acceptance import compare_audio, inspect_media
from run_motion_api_acceptance import atomic_json, state_lock


def verify_master_pcm(request, segments, inputs):
    """Bind the delivery master to the frozen mixed PCM used by every model window."""
    master = Path(request['master'])
    for value in inputs.values():
        path = Path(value['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != value['fileHash']:
            raise ValueError('Frozen preparation input changed')
    with wave.open(str(master), 'rb') as audio:
        if (audio.getframerate(), audio.getnframes(), audio.getnchannels(), audio.getsampwidth()) != (
                request['sampleRate'], request['sampleCount'], 1, 2):
            raise ValueError('Master PCM clock differs')
        for spec in segments:
            piece = request['expertPlan']['segments'][spec['index']]['audioSlice']
            entry = inputs[spec['workflow']['242']['inputs']['audio']]
            with wave.open(entry['path'], 'rb') as prepared:
                if (prepared.getframerate(), prepared.getnchannels(), prepared.getsampwidth()) != (request['sampleRate'], 1, 2):
                    raise ValueError('Prepared PCM format differs')
                observed = prepared.readframes(prepared.getnframes())
            audio.setpos(piece['startSample'])
            expected = (b'\0\0'*piece['prefixSilentSamples']
                        + audio.readframes(piece['endSample']-piece['startSample'])
                        + b'\0\0'*piece['suffixSilentSamples'])
            if observed != expected:
                raise ValueError('Master no longer matches frozen model conditioning audio')
    return master


def assemble(state_path, request_path, reference, output):
    state = json.loads(state_path.read_text(encoding='utf-8'))
    request = json.loads(request_path.read_text(encoding='utf-8'))
    if not state.get('completed') or state.get('errorType'):
        raise ValueError('Only completed successful benchmark runs can be assembled')
    segments = state['plan']['segments']
    indices = [item['index'] for item in segments]
    if indices != list(range(len(request['expertPlan']['segments']))):
        raise ValueError('Full ordered timeline required; screening windows cannot form a full clip')
    if [item['index'] for item in state['results']] != indices:
        raise ValueError('Every planned window requires exactly one ordered result')
    if any(item.get('nativeSize', [864, 480]) != [864, 480] for item in segments):
        raise ValueError('ROI outputs require native source composition before full-frame assembly')
    cursor = 0
    for spec, original in zip(segments, request['expertPlan']['segments']):
        if (original['outputStartFrame'] != cursor or original['outputEndFrame'] <= cursor
                or original['outputEndFrame']-cursor != spec['coreFrames']
                or (spec['trimStartFrame'], spec['trimEndFrame']) != (original['trimStartFrame'], original['trimEndFrame'])
                or spec['trimEndFrame']-spec['trimStartFrame'] != spec['coreFrames']
                or not 0 <= spec['trimStartFrame'] < spec['trimEndFrame'] <= spec['modelFrames']):
            raise ValueError('Window clock, core length or context trim differs')
        cursor = original['outputEndFrame']
    if cursor != (request['sampleCount']*25+request['sampleRate']-1)//request['sampleRate']:
        raise ValueError('Window timeline does not cover the authoritative sample clock')
    root = state_path.resolve().parent
    output = output.resolve()
    if output.exists():
        raise ValueError('Use a new output path; existing media are immutable')
    for item in state['results']:
        path = root/item['file']
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['fileHash']:
            raise ValueError('Benchmark source video changed')
    # The original prepared per-window mixed PCM must still bind the full master.
    master = verify_master_pcm(request, segments, state['plan']['inputs'])
    duration = request['sampleCount']/request['sampleRate']
    reference_media = inspect_media(reference, duration=duration, fps=25, width=1280, height=720)
    if not reference_media['passed']:
        raise ValueError('Reference media does not match the authoritative delivery clock')
    expected_frames = reference_media['video']['decodedFrames']
    start = time.perf_counter()
    cores = []
    for spec, result in zip(segments, state['results']):
        if spec['index'] != result['index']:
            raise ValueError('Result/source window order differs')
        original = request['expertPlan']['segments'][spec['index']]
        if (spec['trimStartFrame'], spec['trimEndFrame']) != (original['trimStartFrame'], original['trimEndFrame']):
            raise ValueError('Context trim differs from original audio plan')
        target = root/f'assembled-core-{spec["index"]}.mp4'
        if target.exists():
            raise ValueError('Partial assembly retained; use a new explicit assembly run')
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(root/result['file']), '-vf',
            f'trim=start_frame={spec["trimStartFrame"]}:end_frame={spec["trimEndFrame"]},setpts=PTS-STARTPTS',
            '-an', '-r', '25', '-c:v', 'libx264', '-crf', '19', '-pix_fmt', 'yuv420p', str(target)], check=True, timeout=120)
        cores.append(target)
    concat = root/'assembled-concat.txt'
    concat.write_text(''.join("file '"+p.as_posix().replace("'", "'\\''")+"'\n" for p in cores), encoding='utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', str(concat),
        '-i', str(master), '-map', '0:v:0', '-map', '1:a:0', '-vf',
        f'trim=end_frame={expected_frames},setpts=PTS-STARTPTS,scale=1280:720:flags=lanczos,unsharp=5:5:0.45:5:5:0,setsar=1',
        '-t', str(request['sampleCount']/request['sampleRate']), '-r', '25', '-c:v', 'libx264',
        '-preset', 'fast', '-crf', '16', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k',
        '-movflags', '+faststart', str(output)], check=True, timeout=180)
    media = inspect_media(output, duration=request['sampleCount']/request['sampleRate'], fps=25, width=1280, height=720)
    audio = compare_audio(output, reference)
    report = {'runId': state['plan']['runId'], 'media': media, 'audioComparedWithReference': audio,
              'assemblyAndCheckSeconds': time.perf_counter()-start,
              'engineRunWallSeconds': state['totalWallSeconds'], 'qualityAccepted': False,
              'limitations': ['Source preparation predates benchmark; time is not full cold production delivery',
                              'Pixel/decoding validation does not approve lip synchronization or naturalness']}
    atomic_json(output.with_suffix('.verification.json'), report)
    if not media['passed'] or not audio['passed']:
        raise ValueError('Assembly verification failed; evidence retained')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    with state_lock(args.out.with_suffix('.assembly.json')):
        result = assemble(args.state, args.request, args.reference, args.out)
    print(json.dumps({'mediaPassed': result['media']['passed'], 'decodedAudioEqual': result['audioComparedWithReference']['passed'],
                      'engineRunWallSeconds': result['engineRunWallSeconds'], 'qualityAccepted': False}))


if __name__ == '__main__':
    main()
