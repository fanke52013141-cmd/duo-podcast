import copy
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from duocast.adapters.over_shoulder_video import configure_camera_workflow
from duocast.services.over_shoulder import camera_pair, shot_plan, shot_chunks
from duocast.services.render_inputs import validate_render_options
from test_render_dependencies import project


def visual():
    return {"id": "V2", "mode": "overShoulder", "aspect": "landscape", "cameraGroups": [{"id": "CG", "cameraAssets": [
        {"id": f"CA-{s}", "mode": "overShoulder", "subjectSpeaker": s, "foregroundSpeaker": 'B' if s == 'A' else 'A',
         "masterImage": {"artifactId": f"IMG-{s}"}, "subjectRegion": {"x": .1, "y": .05, "width": .7, "height": .9}} for s in ('A', 'B')]}]}


def test_ots_plan_uses_next_turn_offset_and_covers_silent_gap():
    plan = shot_plan(project().model_dump(by_alias=True), visual())
    assert [(s['startSample'], s['endSample'], s['speaker']) for s in plan] == [(0, 64000, 'A'), (64000, 112000, 'B')]
    assert [s['cameraAssetId'] for s in plan] == ['CA-A', 'CA-B']


@pytest.mark.parametrize('change', ['missing', 'same_image', 'same_role', 'out_of_bounds', 'nan', 'duplicate'])
def test_invalid_camera_pair_rejected(change):
    v = visual(); assets = v['cameraGroups'][0]['cameraAssets']
    if change == 'missing': assets.pop()
    if change == 'same_image': assets[1]['masterImage'] = copy.deepcopy(assets[0]['masterImage'])
    if change == 'same_role': assets[0]['foregroundSpeaker'] = 'A'
    if change == 'out_of_bounds': assets[0]['subjectRegion']['width'] = 1
    if change == 'nan': assets[0]['subjectRegion']['x'] = float('nan')
    if change == 'duplicate': assets.append(copy.deepcopy(assets[0]))
    with pytest.raises(ValueError): camera_pair(v)


def test_overlapping_turns_cannot_silently_drop_other_voice():
    p = project().model_dump(by_alias=True)
    p['audioTimeline']['unitOffsets']['U-T2'] = 24000
    with pytest.raises(ValueError, match='重叠'): shot_plan(p, visual())


@pytest.mark.parametrize('speaker, node', [('A', '218'), ('B', '241')])
def test_single_subject_workflow_never_conditions_foreground_voice(speaker, node):
    wf = json.loads((Path(__file__).parents[1] / 'duocast/adapters/multitalk_workflow.json').read_text(encoding='utf-8'))
    configure_camera_workflow(wf, camera_pair(visual())[speaker])
    assert wf['198']['inputs']['audio_1'] == [node, 0]
    assert 'audio_2' not in wf['198']['inputs']
    assert wf['198']['inputs']['ref_target_masks'] == ['251', 0]
    assert 'foreground' in wf['135']['inputs']['positive_prompt']


def test_real_render_accepts_complete_ots_assets_and_rejects_listener_profile():
    from duocast.domain.visual import VisualVariant
    p = project(); p.visual_variants.append(VisualVariant.model_validate(visual()))
    validate_render_options(p, {'visualVariantId': 'V2'})
    with pytest.raises(HTTPException) as exc:
        validate_render_options(p, {'visualVariantId': 'V2', 'generationProfile': 'dialogue'})
    assert exc.value.status_code == 422


def test_long_turn_is_balanced_and_sample_exact():
    pieces = shot_chunks({'startSample': 454009, 'endSample': 714258}, 48000)
    assert pieces == [(454009, 584133), (584133, 714258)]
    assert max(b - a for a, b in pieces) <= 5 * 48000


def test_public_upload_writes_both_cameras_to_one_variant(tmp_path):
    from test_round4_optimizations import make_client, PNG_BYTES
    client, store, _, _ = make_client(tmp_path)
    with client:
        store.create('ots', 'over shoulder')
        ids = []
        for speaker in ('A', 'B'):
            response = client.post('/api/projects/ots/visual/upload-master', files={'file': (f'{speaker}.png', PNG_BYTES + speaker.encode(), 'image/png')},
                data={'mode': 'overShoulder', 'aspect': 'landscape', 'cameraGroupId': 'CG', 'cameraAssetId': f'CA-{speaker}',
                      'subjectSpeaker': speaker, 'foregroundSpeaker': 'B' if speaker == 'A' else 'A'})
            assert response.status_code == 200, response.text
            ids.append(response.json()['variantId'])
        assert ids[0] == ids[1]
        p = store.load('ots')
        assert len(p.visual_variants) == 1
        assert set(camera_pair(p.visual_variants[0].model_dump(by_alias=True))) == {'A', 'B'}


@pytest.mark.parametrize('data', [{'mode': 'wrong'}, {'mode': 'overShoulder'}, {'mode': 'overShoulder', 'subjectSpeaker': 'A', 'foregroundSpeaker': 'A'}])
def test_public_upload_rejects_invalid_camera_metadata_before_writing(tmp_path, data):
    from test_round4_optimizations import make_client, PNG_BYTES
    client, store, _, artifacts = make_client(tmp_path)
    with client:
        store.create('ots', 'over shoulder')
        response = client.post('/api/projects/ots/visual/upload-master', files={'file': ('A.png', PNG_BYTES, 'image/png')}, data=data)
        assert response.status_code == 422
        assert not store.load('ots').visual_variants


def test_partial_marker_does_not_block_resumable_render(tmp_path):
    from duocast.adapters.over_shoulder_video import read_marker
    marker = tmp_path / 'partial.json'; marker.write_text('{broken', encoding='utf-8')
    assert read_marker(marker) == {}


def test_over_shoulder_does_not_silently_ignore_adaptive_performance():
    from duocast.domain.visual import VisualVariant
    p = project(); p.visual_variants.append(VisualVariant.model_validate(visual()))
    with pytest.raises(HTTPException) as exc:
        validate_render_options(p, {'visualVariantId': 'V2', 'performanceMode': 'adaptive'})
    assert exc.value.status_code == 422


def test_ots_camera_mode_is_independent_of_old_two_shot_preset():
    from duocast.domain.visual import VisualVariant
    from duocast.services.render_inputs import resolve_camera_mode
    p = project(); p.camera_mode = 'twoShot'
    p.visual_variants.append(VisualVariant.model_validate(visual()))
    assert resolve_camera_mode(p, {'visualVariantId': 'V2'}) == 'speaker'
    with pytest.raises(HTTPException):
        validate_render_options(p, {'visualVariantId': 'V2', 'cameraMode': 'twoShot'})
