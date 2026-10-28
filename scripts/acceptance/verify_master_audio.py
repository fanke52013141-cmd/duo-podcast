"""Verify delivery audio against an independently AAC-encoded canonical PCM master."""
import argparse
from pathlib import Path
import subprocess
import time
import wave

from run_motion_acceptance import execute,inspect_media,sha256_file
from run_motion_api_acceptance import atomic_json


def verify(video,master,work):
    video,master,work=(Path(p).resolve() for p in (video,master,work))
    if work.exists():raise ValueError('Existing audio verification is immutable; use a new evidence directory')
    with wave.open(str(master),'rb') as audio:
        count,rate=audio.getnframes(),audio.getframerate()
        if audio.getnchannels()!=1 or audio.getsampwidth()!=2 or rate!=48000 or count<=0:
            raise ValueError('Expected canonical PCM16/48000Hz/mono master')
    work.mkdir(parents=True)
    started=time.perf_counter();master_hash=sha256_file(master)
    report={'passed':False,'masterSha256':master_hash,'sampleRate':rate,'sampleCount':count,
        'encoding':'AAC/192k from unchanged canonical PCM','pcmComparisonFormat':'s16le/48000Hz/mono',
        'executions':[],'limitations':['AAC is lossy; decoded equality is to independently encoded master, not original PCM bytes',
        'Does not approve visible lip synchronization, role identity or naturalness']}
    try:
        media=inspect_media(video,duration=count/rate,fps=25)
        report['videoMedia']=media
        if not media['passed']:raise ValueError('Delivery video media failed before audio comparison')
        reference=work/'master-aac.m4a'
        subprocess.run(['ffmpeg','-v','error','-i',str(master),'-c:a','aac','-b:a','192k',
            '-movflags','+faststart',str(reference)],check=True,timeout=120)
        reference_hash=sha256_file(reference)
        hashes=[]
        for path in (video,reference):
            execution,_=execute(['ffmpeg','-v','error','-xerror','-nostdin','-i',str(path),
                '-map','0:a:0','-ac','1','-ar','48000','-f','s16le','-'],120)
            report['executions'].append(execution)
            if execution['exitCode'] or execution['timedOut']:raise ValueError('Audio stream failed strict decode')
            hashes.append(execution['stdoutSha256'])
        if (sha256_file(master)!=master_hash or sha256_file(video)!=media['input']['sha256']
                or sha256_file(reference)!=reference_hash):
            raise ValueError('Audio verification input changed')
        report.update(passed=hashes[0]==hashes[1],decodedAudioSha256=hashes,referenceSha256=reference_hash)
        if not report['passed']:report['failure']='decoded_audio_mismatch'
    except BaseException as error:
        report['errorType']=type(error).__name__
        raise
    finally:
        report['verificationSeconds']=time.perf_counter()-started
        atomic_json(work/'verification.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video',required=True,type=Path)
    parser.add_argument('--master',required=True,type=Path)
    parser.add_argument('--work',required=True,type=Path)
    args=parser.parse_args()
    report=verify(args.video,args.master,args.work)
    print({'passed':report['passed'],'verificationSeconds':report['verificationSeconds']})
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
