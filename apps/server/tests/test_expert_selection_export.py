"""Real FFmpeg export verification using synthetic colour and PCM, with no model calls."""
import asyncio
import hashlib
import json
import subprocess
import wave
from types import SimpleNamespace
import pytest
from duocast.services.expert_selection_export import export_selection
from duocast.storage.artifacts import ArtifactStore


@pytest.mark.parametrize('first_frame,start,end', [(0, 24000, 72000), (25, 60000, 84000)])
def test_selection_export_keeps_absolute_audio_range_and_native_frame_count(tmp_path, first_frame, start, end):
    folder = tmp_path/'run'; (folder/'audio').mkdir(parents=True)
    samples, rate = 96000, 48000
    pcm = b'\0\0'*samples
    with wave.open(str(folder/'audio/master.wav'), 'wb') as audio:
        audio.setparams((1,2,rate,0,'NONE','not compressed')); audio.writeframes(pcm)
    core = folder/'core.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=128x72:r=25:d=2',
                    '-an','-c:v','libx264','-pix_fmt','yuv420p',str(core)], check=True, capture_output=True, timeout=15)
    artifacts = ArtifactStore(tmp_path/'artifacts')
    async def command(*args, timeout=30):
        result = await asyncio.to_thread(subprocess.run, list(map(str,args)), check=True, capture_output=True, timeout=timeout)
        return result.stdout
    provider = SimpleNamespace(artifacts=artifacts, manager=SimpleNamespace(checkpoint=lambda _:None), _command=command)
    request = {'dependencyHash':'a'*64, 'expertSelection':{'selectedSampleRange':[start,end]}}
    result = asyncio.run(export_selection(provider, object(), request, 'request', folder, [core],
        [{'coreHash':artifacts._hash_file(str(core))}], {'segments':[{'outputStartFrame':first_frame}]},
        samples,rate,{'master':hashlib.sha256(pcm).hexdigest()}))
    final = artifacts.by_id(result['artifactIds'][0])
    probe = json.loads(subprocess.check_output(['ffprobe','-v','error','-count_frames','-show_streams','-of','json',final['path']]))
    video = next(s for s in probe['streams'] if s['codec_type']=='video')
    assert int(video['nb_read_frames']) == ((end-start)*25+rate-1)//rate
    assert result['meta']['durationSec'] == (end-start)/rate
    assert result['meta']['automaticAdoption'] is False
