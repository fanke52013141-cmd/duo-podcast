"""Prepare an approved two-shot motion clip without per-person coordinates."""
import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

def prepare(video,stage,timeline,detector_path,output,allow_low=False):
    import cv2
    import numpy as np
    from validate_template import validate
    output=output.resolve()
    assets=output/'assets'
    if output.exists() and any(output.iterdir()):raise ValueError('Use an empty output directory; active runtimes are never overwritten')
    assets.mkdir(parents=True,exist_ok=True)
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_streams','-of','json',str(video)]))['streams'][0]
    if int(probe['height'])<720 and not allow_low:raise ValueError('New production templates should use native 720p or higher')
    manifest=json.loads(timeline.read_text(encoding='utf-8-sig'))
    if not isinstance(manifest.get('turns'),list):raise ValueError('Timeline must provide source speaking turns')
    shutil.copy2(stage,assets/'stage.png')
    (assets/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    previous_dir=Path.cwd()
    local_detector=output/'detector.onnx'
    shutil.copy2(detector_path,local_detector)
    coordinates={}
    try:
        os.chdir(output)
        for speaker,x in (('A','0'),('B','iw/2')):
            target=assets/f'template_{speaker}.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-i',str(video),'-vf',f'fps=25,crop=trunc(iw/4)*2:ih:{x}:0','-an','-c:v','libx264','-crf','14','-pix_fmt','yuv420p',str(target)],check=True)
            capture=cv2.VideoCapture(str(target))
            boxes=[];previous=None;detector=None
            while True:
                ok,frame=capture.read()
                if not ok:break
                height,width=frame.shape[:2]
                if detector is None:detector=cv2.FaceDetectorYN.create('detector.onnx','',(width,height),.65,.3,5000)
                _,faces=detector.detect(frame)
                if faces is None:
                    capture.release()
                    raise ValueError(f'{speaker} frame {len(boxes)}: no confident face; validate source quality before adoption')
                face=max(faces,key=lambda value:value[2]*value[3])
                left,top,w,h=face[:4]
                box=np.array([max(0,left),max(0,top),min(width,left+w),min(height,top+h)])
                if previous is not None:box=.8*box+.2*previous
                boxes.append(np.rint(box).astype(int).tolist());previous=box
            capture.release();coordinates[speaker]=boxes
    finally:
        os.chdir(previous_dir)
        local_detector.unlink(missing_ok=True)
    (assets/'face_coordinates.json').write_text(json.dumps(coordinates),encoding='utf-8')
    result=validate(output)
    (output/'template_validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('video','stage','timeline','detector','output'):parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--allow-low-resolution',action='store_true')
    args=parser.parse_args()
    print(json.dumps(prepare(args.video.resolve(),args.stage.resolve(),args.timeline.resolve(),args.detector.resolve(),args.output,args.allow_low_resolution),ensure_ascii=False,indent=2))
