"""Audio-only master reference must work; equal durations cannot hide a real shift."""
import importlib
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import wave
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'scripts/acceptance'))
module=importlib.import_module('verify_master_audio')
pytestmark=pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg required for real audio codec regression')


@pytest.fixture
def audio_delivery(tmp_path):
    samples=[int(7000*math.sin(2*math.pi*440*i/48000)) if 6000<i<40000 else 0 for i in range(48000)]
    def write(path,values):
        with wave.open(str(path),'wb') as audio:
            audio.setparams((1,2,48000,0,'NONE','not compressed'))
            audio.writeframes(struct.pack('<48000h',*values))
    master=tmp_path/'master.wav';write(master,samples)
    def video(name,shift):
        source=tmp_path/(name+'.wav');write(source,([0]*shift+samples)[:48000])
        output=tmp_path/(name+'.mp4')
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=32x32:r=25:d=1',
            '-i',str(source),'-t','1','-c:v','libx264','-c:a','aac','-b:a','192k',str(output)],check=True,timeout=20)
        return output
    return master,video


def test_audio_only_aac_reference_does_not_require_a_video_track(tmp_path,audio_delivery):
    master,create=audio_delivery
    report=module.verify(create('correct',0),master,tmp_path/'verification')
    assert report['passed'] and report['decodedAudioSha256'][0]==report['decodedAudioSha256'][1]


def test_same_duration_shifted_audio_is_rejected(tmp_path,audio_delivery):
    master,create=audio_delivery
    report=module.verify(create('shifted',2400),master,tmp_path/'verification')
    assert not report['passed'] and report['failure']=='decoded_audio_mismatch'
