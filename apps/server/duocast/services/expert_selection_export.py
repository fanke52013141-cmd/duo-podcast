"""Trim selected absolute-clock cores and the original master once, without rebasing inference."""
import json
from ..domain.motion_pack import content_hash


async def export_selection(provider, job, req, request_key, folder, cores, records, expert, samples, rate, pcm_hashes):
    from ..adapters.expert_motion_provider import _pcm_hash
    if _pcm_hash(folder/'audio/master.wav', rate, samples) != pcm_hashes['master']:
        raise ValueError('Expert master changed before selection export')
    selection = req['expertSelection']
    window_artifacts = []
    selected_windows = selection.get('windows', [])
    if selected_windows and len(selected_windows) != len(cores):
        raise ValueError('Selection core count differs from frozen windows')
    roi_profile = req.get('expertProfile') == 'expert-sage-roi-v1'
    export_geometry = None
    if roi_profile:
        from .expert_assembly import roi_letterbox
        if not cores:
            raise ValueError('Expert ROI selection has no native cores')
        declared_sizes = set()
        for window in selected_windows:
            native = window.get('segment', {}).get('geometry', {}).get('native')
            if native is not None:
                if (not isinstance(native, (list, tuple)) or len(native) < 2
                        or any(type(value) is not int or value <= 0 for value in native[:2])):
                    raise ValueError('Expert ROI declared an invalid native size')
                declared_sizes.add(tuple(native[:2]))
        actual_sizes = set()
        for core in cores:
            probe = json.loads(await provider._command(
                'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                'stream=width,height', '-of', 'json', core))
            stream = probe['streams'][0]
            actual_sizes.add((int(stream['width']), int(stream['height'])))
        if len(actual_sizes) != 1 or len(declared_sizes) > 1 or declared_sizes and declared_sizes != actual_sizes:
            raise ValueError('Expert ROI cores have inconsistent native geometry')
        export_geometry = roi_letterbox(next(iter(actual_sizes)))
    for window, core, record in zip(selected_windows, cores, records):
        segment = window['segment']
        actual_hash = provider.artifacts._hash_file(str(core))
        if actual_hash != record['coreHash']:
            raise ValueError('Expert core changed before export')
        core_id = 'VID-EXPERT-WINDOW-'+content_hash({'hash': actual_hash, 'segment': segment})[:32]
        if not provider.artifacts.by_id(core_id):
            provider.artifacts.register(core_id, 'video', str(core), segment['dependencyHash'],
                {'source':'expertSelectedWindow', 'outputStartFrame':segment['outputStartFrame'],
                 'outputEndFrame':segment['outputEndFrame']}, 'expert-window-v1')
        window_artifacts.append({'windowId': window['windowId'], 'index':window['index'],
            'artifactId':core_id, 'fileHash':actual_hash, 'dependencyHash':segment['dependencyHash'],
            'outputStartFrame':segment['outputStartFrame'], 'outputEndFrame':segment['outputEndFrame']})
    start, end = selection.get('reviewSampleRange', selection['selectedSampleRange'])
    duration = (end-start)/rate
    first_frame = expert['segments'][0]['outputStartFrame']
    offset = start/rate-first_frame/25
    if offset < 0:
        raise ValueError('Selected cores do not cover the audio range')
    key_data = {'requestKey': request_key, 'cores': [r['coreHash'] for r in records],
                'range': [start, end], 'recipe': 'expert-selection-v2'}
    if export_geometry is not None:
        key_data['exportGeometry'] = {key: value for key, value in export_geometry.items() if key != 'filter'}
    key = content_hash(key_data)
    output = folder/'exports'/key
    output.mkdir(parents=True, exist_ok=True)
    final = output/'final.mp4'
    artifact_id = 'VID-EXPERT-SELECTION-'+key[:32]
    existing = provider.artifacts.by_id(artifact_id)
    if existing:
        provider._artifact(artifact_id, {'video'})
    else:
        video_filter=(export_geometry['filter'] if export_geometry is not None else
                      'scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,setsar=1')
        concat = output/'cores.txt'
        concat.write_text('\n'.join("file '"+p.as_posix().replace("'", "'\\''")+"'" for p in cores), encoding='utf-8')
        partial = output/'final.partial.mp4'
        await provider._command('ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', 0, '-i', concat,
            '-i', folder/'audio/master.wav', '-filter_complex',
            f'[0:v]trim=start={offset}:duration={duration},setpts=PTS-STARTPTS,{video_filter}[v];'
            f'[1:a]atrim=start_sample={start}:end_sample={end},asetpts=PTS-STARTPTS[a]',
            '-map', '[v]', '-map', '[a]', '-r', 25, '-frames:v', ((end-start)*25+rate-1)//rate,
            '-c:v', 'libx264', '-crf', 16 if roi_profile else 19, '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', partial)
        raw = json.loads(await provider._command('ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-of', 'json', partial))
        video = next(s for s in raw['streams'] if s['codec_type'] == 'video')
        audio = next(s for s in raw['streams'] if s['codec_type'] == 'audio')
        if (int(video['nb_read_frames']) != ((end-start)*25+rate-1)//rate
                or abs(float(audio['duration'])-duration) > .03
                or abs(float(video['duration'])-duration) > .045
                or (video['width'], video['height']) != (1280, 720)):
            raise ValueError('Selected expert media has an invalid native clock or export geometry')
        await provider._command('ffmpeg', '-v', 'error', '-xerror', '-i', partial, '-f', 'null', '-', timeout=120)
        provider.manager.checkpoint(job)
        partial.replace(final)
        provider.artifacts.register(artifact_id, 'video', str(final), req['dependencyHash'], req, 'expert-selection-v1')
    meta = {
        'durationSec': duration, 'fps': 25, 'resolution': '1280x720', 'qualityStatus': 'needs_review',
        'automaticAdoption': False, 'reviewSampleRange': [start, end],
        'selectedSampleRange': selection['selectedSampleRange'],
        'generatedWindows': sum(not r.get('reused') for r in records),'reusedWindows':sum(bool(r.get('reused')) for r in records),
        'windowArtifacts':window_artifacts, 'path': str(final)}
    if export_geometry is not None:
        meta['exportGeometry'] = {key: value for key, value in export_geometry.items() if key != 'filter'}
    return {'artifactIds': [artifact_id], 'simulated': False, 'meta': meta}
