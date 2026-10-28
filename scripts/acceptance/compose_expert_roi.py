"""Composite verified experimental ROI outputs onto their original moving references."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import wave

from assemble_expert_benchmark import verify_master_pcm
from benchmark_expert import file_hash, read_json
from expert_roi import composite_frame
from run_motion_acceptance import inspect_media
from run_motion_api_acceptance import atomic_json, state_lock


def compose(state_path, roi_plan_path, output):
    import cv2
    import numpy as np
    started=time.perf_counter()
    state_path, roi_plan_path, output=(Path(p).resolve() for p in (state_path,roi_plan_path,output))
    state, plan=read_json(state_path),read_json(roi_plan_path)
    if not state.get('completed') or state.get('errorType'):
        raise ValueError('Only successful completed ROI model results can be composed')
    if file_hash(roi_plan_path)!=state['plan']['sourcePlanSha256']:
        raise ValueError('ROI source plan changed')
    provenance=plan['roi']
    if (file_hash(provenance['sourcePlan'])!=provenance['sourcePlanHash']
            or file_hash(provenance['request'])!=provenance['requestHash']):
        raise ValueError('Original source snapshot changed')
    request=read_json(provenance['request'])
    specs=state['plan']['segments'];results=state['results']
    indices=[s['index'] for s in specs]
    if (not indices or indices!=list(range(indices[0],indices[-1]+1))
            or [r['index'] for r in results]!=indices):
        raise ValueError('Ordered contiguous source windows required')
    if output.exists():raise ValueError('Existing output is immutable')
    work=output.with_suffix('.work')
    if work.exists():raise ValueError('Existing partial composition retained; choose a new output')
    source_segments=request['expertPlan']['segments']
    cursor=source_segments[indices[0]]['outputStartFrame']
    for spec in specs:
        original=source_segments[spec['index']]
        if (original['outputStartFrame']!=cursor
                or original['outputEndFrame']-cursor!=spec['coreFrames']
                or (spec['trimStartFrame'],spec['trimEndFrame'])!=(original['trimStartFrame'],original['trimEndFrame'])
                or spec['trimEndFrame']-spec['trimStartFrame']!=spec['coreFrames']):
            raise ValueError('ROI core/context clock changed')
        cursor=original['outputEndFrame']
    master=verify_master_pcm(request,specs,state['plan']['inputs'])
    crop=provenance['crop'];native=provenance['sourceNative']
    if provenance['spatialResize'] is not False:
        raise ValueError('This compositor only supports native-pixel patches')
    for spec,result in zip(specs,results):
        if spec['nativeSize']!=crop[2:]:raise ValueError('ROI output dimensions differ')
        raw=state_path.parent/result['file']
        reference=plan['segments'][spec['index']]['fullReference']
        if file_hash(raw)!=result['fileHash'] or file_hash(reference['path'])!=reference['fileHash']:
            raise ValueError('ROI output or full reference changed')
        for path,shape in [(raw,crop[2:]),(Path(reference['path']),native)]:
            check=inspect_media(path,fps=25,width=shape[0],height=shape[1],require_audio=False)
            if not check['passed'] or check['video']['decodedFrames']!=spec['modelFrames']:
                raise ValueError('ROI/reference frame count differs')
    work.mkdir(parents=True)
    start_frame=source_segments[indices[0]]['outputStartFrame']
    start_sample=start_frame*request['sampleRate']//25
    end_sample=min(request['sampleCount'],cursor*request['sampleRate']//25)
    audio_path=work/'master-slice.wav'
    with wave.open(str(master),'rb') as audio:
        audio.setpos(start_sample);pcm=audio.readframes(end_sample-start_sample)
    with wave.open(str(audio_path),'wb') as audio:
        audio.setparams((1,2,request['sampleRate'],0,'NONE','not compressed'));audio.writeframes(pcm)
    delivery_frames=min(cursor,request['sampleCount']*25//request['sampleRate'])-start_frame
    if delivery_frames<=0:raise ValueError('No deliverable frames')
    core=work/'composited-native.mp4'
    encoder=subprocess.Popen(['ffmpeg','-v','error','-f','rawvideo','-pix_fmt','bgr24','-s',
        f'{native[0]}x{native[1]}','-r','25','-i','pipe:0','-an','-c:v','libx264','-crf','19',
        '-pix_fmt','yuv420p',str(core)],stdin=subprocess.PIPE)
    written=0;outside_exact=True
    try:
        for spec,result in zip(specs,results):
            raw=state_path.parent/result['file']
            reference=plan['segments'][spec['index']]['fullReference']['path']
            generated,original=cv2.VideoCapture(str(raw)),cv2.VideoCapture(reference)
            try:
                for frame_index in range(spec['modelFrames']):
                    ok1,patch=generated.read();ok2,source=original.read()
                    if not ok1 or not ok2:raise ValueError('ROI/reference decode truncated')
                    if not spec['trimStartFrame']<=frame_index<spec['trimEndFrame']:continue
                    if written>=delivery_frames:continue
                    boxes=[track[frame_index]['faceBox'] for track in source_segments[spec['index']]['sourceByRole'].values()]
                    frame=composite_frame(source,patch,crop,boxes)
                    x,y,w,h=crop
                    outside_exact=outside_exact and all(np.array_equal(a,b) for a,b in [
                        (source[:y],frame[:y]),(source[y+h:],frame[y+h:]),
                        (source[y:y+h,:x],frame[y:y+h,:x]),(source[y:y+h,x+w:],frame[y:y+h,x+w:])])
                    encoder.stdin.write(frame.tobytes());written+=1
            finally:
                generated.release();original.release()
    except BaseException:
        encoder.terminate()
        raise
    finally:
        try:
            encoder.stdin.close()
        except BrokenPipeError:
            pass
        code=encoder.wait(timeout=30)
    if code or written!=delivery_frames or not outside_exact:
        raise ValueError('ROI composition failed frame or untouched-pixel verification')
    duration=(end_sample-start_sample)/request['sampleRate']
    subprocess.run(['ffmpeg','-v','error','-i',str(core),'-i',str(audio_path),'-map','0:v:0','-map','1:a:0',
        '-vf','scale=1280:720:flags=lanczos,unsharp=5:5:0.45:5:5:0,setsar=1',
        '-t',str(duration),'-r','25','-c:v','libx264','-preset','fast','-crf','16',
        '-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart',str(output)],check=True,timeout=180)
    media=inspect_media(output,duration=duration,fps=25,width=1280,height=720)
    if not media['passed'] or media['video']['decodedFrames']!=delivery_frames:
        raise ValueError('ROI delivery media failed')
    report={'scope':'experimental native-pixel face ROI; human quality pending','indices':indices,
        'output':str(output),'crop':crop,'media':media,'frames':written,
        'masterSampleRange':[start_sample,end_sample],'authoritativeMaster':str(master),
        'outsideJointCropExactBeforeEncoding':outside_exact,
        'outsidePixelEqualityAfterLossyEncoding':False,
        'compositionSeconds':time.perf_counter()-started,'engineRunWallSeconds':state['totalWallSeconds'],
        'qualityAccepted':False,'sourceStateHash':file_hash(state_path),'roiPlanHash':file_hash(roi_plan_path)}
    atomic_json(output.with_suffix('.verification.json'),report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',required=True,type=Path)
    parser.add_argument('--roi-plan',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    with state_lock(args.out.with_suffix('.compose.json')):
        report=compose(args.state,args.roi_plan,args.out)
    print(json.dumps({'mediaPassed':report['media']['passed'],'frames':report['frames'],
        'outsideJointCropExactBeforeEncoding':report['outsideJointCropExactBeforeEncoding'],
        'compositionSeconds':report['compositionSeconds'],'qualityAccepted':False}))


if __name__=='__main__':main()
