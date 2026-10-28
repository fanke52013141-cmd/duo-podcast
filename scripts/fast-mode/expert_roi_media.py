"""Owned CPU ROI preparation and native core composition; never submits model jobs."""
import json
from pathlib import Path
import subprocess
import sys
from expert_roi_geometry import composite_frame
from motion_worker import file_hash


def crop_prepared(prepared, crop):
    import cv2
    x,y,width,height=crop
    full=prepared['files']['referenceVideo'].copy()
    root=Path(full['path']).parent
    for kind in ('anchorImage','maskA','maskB','referenceVideo'):
        entry=prepared['files'][kind]
        path=Path(entry['path'])
        if file_hash(path)!=entry['fileHash']:raise ValueError('Full native source changed')
        target=root/('roi_'+path.name)
        if kind=='referenceVideo':
            subprocess.run(['ffmpeg','-v','error','-y','-i',str(path),'-vf',f'crop={width}:{height}:{x}:{y}',
                '-map','0:v:0','-map','0:a:0','-c:a','copy','-c:v','libx264','-crf','0','-pix_fmt','yuv420p',str(target)],check=True,timeout=180)
            prepared['inputNames'][kind]=target.name
        else:
            import numpy as np
            image=cv2.imdecode(np.frombuffer(path.read_bytes(),dtype=np.uint8),cv2.IMREAD_COLOR)
            patch=image[y:y+height,x:x+width]
            if patch.shape[:2]!=(height,width):raise ValueError('ROI crop outside source')
            ok,data=cv2.imencode('.png',patch)
            if not ok:raise ValueError('Cannot encode ROI input')
            target.write_bytes(bytes(data))
            if kind=='anchorImage':prepared['inputNames'][kind]=target.name
            else:prepared['inputNames']['roleMasks'][kind[-1]]=target.name
        prepared['files'][kind]={'path':str(target),'fileHash':file_hash(target)}
    prepared['fullReference']=full
    prepared['sourceNative']=dict(prepared['native'])
    prepared['native'].update(width=width,height=height)
    prepared['roiCrop']=crop
    return prepared


def compose(request):
    import cv2
    spec=request['segment'];prepared=request['prepared'];core=request['core']
    if file_hash(core['path'])!=core['fileHash'] or file_hash(prepared['fullReference']['path'])!=prepared['fullReference']['fileHash']:
        raise ValueError('ROI/reference media changed')
    source=cv2.VideoCapture(prepared['fullReference']['path']);patch=cv2.VideoCapture(core['path'])
    output=Path(request['output']);native=prepared['sourceNative']
    process=subprocess.Popen(['ffmpeg','-v','error','-y','-f','rawvideo','-pix_fmt','bgr24','-s',
        f'{native["width"]}x{native["height"]}','-r','25','-i','pipe:0','-an','-c:v','libx264','-crf','19','-pix_fmt','yuv420p',str(output)],stdin=subprocess.PIPE)
    written=0
    try:
        for i in range(spec['modelFrames']):
            ok,frame=source.read()
            if not ok:raise ValueError('Full reference truncated')
            if not spec['trimStartFrame']<=i<spec['trimEndFrame']:continue
            ok,region=patch.read()
            if not ok:raise ValueError('ROI core truncated')
            boxes=[track[i]['faceBox'] for track in spec['sourceByRole'].values()]
            result=composite_frame(frame,region,prepared['roiCrop'],boxes)
            process.stdin.write(result.tobytes());written+=1
        if patch.read()[0]:raise ValueError('ROI core contains extra frames')
    except BaseException:
        process.terminate();raise
    finally:
        source.release();patch.release();process.stdin.close()
        code=process.wait(timeout=30)
    if code or written!=spec['outputEndFrame']-spec['outputStartFrame']:raise ValueError('ROI core composition failed')
    return {'frames':written,'fileHash':file_hash(output),'outsideCropExactBeforeEncoding':True}


if __name__=='__main__':
    data=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    print(json.dumps(compose(data)))
