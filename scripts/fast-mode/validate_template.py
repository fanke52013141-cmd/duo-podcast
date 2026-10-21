"""Validate a prepared local motion template without loading GPU models."""
import argparse
import json
import math
import subprocess
from pathlib import Path

def validate(root):
    manifest=json.loads((root/'assets/manifest.json').read_text(encoding='utf-8'))
    coordinates=json.loads((root/'assets/face_coordinates.json').read_text(encoding='utf-8'))
    turns=manifest['turns']
    dimensions=[]
    for speaker in ('A','B'):
        path=root/f'assets/template_{speaker}.mp4'
        result=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_streams','-of','json',str(path)],capture_output=True,text=True,check=True,timeout=30)
        video=json.loads(result.stdout)['streams'][0]
        width,height=int(video['width']),int(video['height'])
        frames=int(video['nb_frames'])
        if video['avg_frame_rate']!='25/1':raise ValueError('Motion templates must use 25 fps')
        if len(coordinates[speaker])!=frames:raise ValueError('Coordinates do not match template frame count')
        for box in coordinates[speaker]:
            if len(box)!=4 or any(type(v) is not int for v in box):raise ValueError('Coordinates must be integer boxes')
            x,y,x2,y2=box
            if not 0<=x<x2<=width or not 0<=y<y2<=height:raise ValueError('Face box outside frame')
        duration=frames/25
        role=[t for t in turns if t['speaker']==speaker]
        if not role or any(not all(isinstance(t[k],(int,float)) and math.isfinite(t[k]) for k in ('start','end')) or not 0<=t['start']<t['end']<=duration+.04 for t in role):
            raise ValueError('Invalid source turn timing')
        has_speech=any(any(t['start']<=i/25<t['end'] for t in role) for i in range(frames))
        has_idle=any(not any(t['start']-.12<=i/25<t['end']+.12 for t in role) for i in range(frames))
        if not has_speech or not has_idle:raise ValueError('Each role needs speaking and closed-mouth listening banks')
        dimensions.append({'speaker':speaker,'width':width,'height':height,'frames':frames,'nativeHD':height>=720})
    if len({(d['width'],d['height'],d['frames']) for d in dimensions})!=1:raise ValueError('Role dimensions and frame counts must match')
    return {'roles':dimensions,'note':'Dimensions do not prove perceptual sharpness, identity or lip-sync quality.'}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',required=True,type=Path)
    args=parser.parse_args()
    print(json.dumps(validate(args.runtime.resolve()),ensure_ascii=False,indent=2))
