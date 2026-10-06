"""Real ffmpeg clock/cache regression; dummy motion is not a visual quality test."""
import asyncio
import json
import shutil
import wave

import pytest

from duocast.adapters.comfyui_video import ComfyUIVideoProvider
from duocast.adapters.over_shoulder_video import render_over_shoulder
from duocast.adapters.over_shoulder_video import validate_native_clip
from duocast.storage.artifacts import ArtifactStore
from test_over_shoulder import visual
from test_render_dependencies import project


class ExportProvider(ComfyUIVideoProvider):
    def __init__(self, root):
        self.artifacts = ArtifactStore(root / 'artifacts')
        self.input_dir = root / 'input'; self.input_dir.mkdir()
        self.base_url = 'http://unused.invalid'
        self.motion_implementation_hash = 'test-clock-v1'
        self.calls = []

    async def _prepare_engine(self, client):
        pass

    async def _shot(self, client, digest, index, start, end, rate, data, tracks, folder, clip, **options):
        self.calls.append((start, end, options['camera']['subjectSpeaker']))
        await self._command('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=navy:s=864x480:r=25',
            '-frames:v', round((end - start) * 25), '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', clip)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg unavailable')
@pytest.mark.parametrize('fps', [25, 30])
def test_final_and_preview_share_camera_clips_with_exact_frame_clock(tmp_path, fps):
    async def exercise():
        provider = ExportProvider(tmp_path)
        p, v = project(), visual()
        p.audio_timeline.master_audio_asset_id = 'AUD-real'
        audio = tmp_path / 'master.wav'
        with wave.open(str(audio), 'wb') as writer:
            writer.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            writer.writeframes(bytes(p.audio_timeline.sample_count * 2))
        provider.artifacts.register('AUD-real', 'audio', str(audio), 'audio-clock')
        for n, camera in enumerate(v['cameraGroups'][0]['cameraAssets']):
            image = tmp_path / f'image-{n}.png'; image.write_bytes(b'fixture image ' + bytes([n]))
            entry = provider.artifacts.register(camera['masterImage']['artifactId'], 'image', str(image), f'image-{n}')
            camera['masterImage']['fileHash'] = entry['fileHash']
        req = {'projectSnapshot': p.model_dump(by_alias=True), 'revisionId': 'R1', 'visualVariantId': 'V2',
               'aspect': 'landscape', 'resolution': '854x480', 'fps': fps, 'seed': 1}
        full = await render_over_shoulder(provider, req, v, None)
        assert [s for _, _, s in provider.calls] == ['A', 'B']
        assert full['meta']['generationStats']['generated'] == 2
        preview_req = {**req, 'rangeStartSec': .24, 'rangeEndSec': 2.1}
        preview = await render_over_shoulder(provider, preview_req, v, None)
        assert len(provider.calls) == 2
        assert preview['meta']['generationStats']['cached'] == 2
        assert not preview['meta']['generationStats']['engineUsed']
        probe = json.loads(await provider._command('ffprobe', '-v', 'error', '-show_streams', '-of', 'json', preview['meta']['path']))
        stream = next(s for s in probe['streams'] if s['codec_type'] == 'video')
        assert int(stream['nb_frames']) == round((2.1 - .24) * fps)
    asyncio.run(exercise())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg unavailable')
def test_short_native_video_is_rejected_before_cache_registration(tmp_path):
    async def exercise():
        provider = ExportProvider(tmp_path)
        clip = tmp_path / 'short.mp4'
        await provider._command('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=s=864x480:r=25',
            '-frames:v', 15, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', clip)
        with pytest.raises(RuntimeError, match='帧数'):
            await validate_native_clip(provider, clip, 2)
    asyncio.run(exercise())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg unavailable')
@pytest.mark.parametrize('fps', [25, 30])
def test_same_camera_transition_keeps_full_frame_clock(tmp_path, fps):
    async def exercise():
        provider = ExportProvider(tmp_path)
        p, v = project(), visual()
        p.audio_timeline.units[-1].sample_count = 260249
        p.audio_timeline.sample_count = 64000 + 260249
        p.audio_timeline.master_audio_asset_id = 'AUD-real'
        audio = tmp_path / 'master.wav'
        with wave.open(str(audio), 'wb') as writer:
            writer.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            writer.writeframes(bytes(p.audio_timeline.sample_count * 2))
        provider.artifacts.register('AUD-real', 'audio', str(audio), 'audio-clock')
        for n, camera in enumerate(v['cameraGroups'][0]['cameraAssets']):
            image = tmp_path / f'image-{n}.png'; image.write_bytes(b'fixture image ' + bytes([n]))
            entry = provider.artifacts.register(camera['masterImage']['artifactId'], 'image', str(image), f'image-{n}')
            camera['masterImage']['fileHash'] = entry['fileHash']
        req = {'projectSnapshot': p.model_dump(by_alias=True), 'revisionId': 'R1', 'visualVariantId': 'V2',
               'aspect': 'landscape', 'resolution': '854x480', 'fps': fps, 'seed': 1}
        result = await render_over_shoulder(provider, req, v, None)
        assert len(result['meta']['continuityTransitions']) == 1
        assert result['meta']['continuityTransitions'][0]['cameraAssetId'] == 'CA-B'
        assert result['meta']['continuityTransitions'][0]['frames'] == round(.12 * fps)
        check = result['meta']['continuityTransitions'][0]['qualityCheck']
        assert check['before']['windowMaxMeanGrayChange'] == 0
        assert check['after']['windowMaxMeanGrayChange'] >= 0
        assert check['needsReview'] == (check['after']['windowMaxMeanGrayChange'] > .5)
    asyncio.run(exercise())


def test_continuity_window_detects_a_jump_after_the_first_frame():
    from duocast.adapters.over_shoulder_video import continuity_measure

    class Frames:
        async def _command(self, *args):
            size = 96 * 54
            return bytes(size) if '-sseof' in args else bytes(size * 2) + bytes([90]) * size * 3

    result = asyncio.run(continuity_measure(Frames(), 'before', 'after', 25,
        {'x': 0, 'y': 0, 'width': 1, 'height': 1}))
    assert result['boundaryMeanGrayChange'] == 0
    assert result['windowMaxMeanGrayChange'] == 90
