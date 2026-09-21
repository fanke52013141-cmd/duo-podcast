"""视觉域模型的向后兼容与正反打序列化测试。"""

from duocast.domain.visual import (
    CameraAsset,
    DialogueCameraGroup,
    Rect,
    ShotPlan,
    ShotSegment,
    VisualVariant,
)
from duocast.services.visual_svc import apply_visual_variant
from duocast.storage.project_store import ProjectStore


def test_existing_visual_variant_json_loads_as_two_shot():
    """缺少新增字段的已存工程继续按原同框行为读取。"""
    variant = VisualVariant.model_validate({
        "id": "V3",
        "masterImage": {"artifactId": "image-v3", "path": "v3.png"},
        "aspect": "landscape",
        "personRegions": {
            "A": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
            "B": {"x": 0.5, "y": 0.2, "width": 0.3, "height": 0.4},
        },
    })

    assert variant.mode == "twoShot"
    assert variant.camera_groups == []
    assert variant.master_image.artifact_id == "image-v3"
    # person_regions 是原有的未类型化 dict 字段；保留原读取行为。
    assert variant.person_regions["A"]["x"] == 0.1


def test_existing_shot_plan_json_loads_with_empty_segments():
    """旧 ranges 格式继续保留，并不要求迁移为机位片段。"""
    plan = ShotPlan.model_validate({
        "id": "S2",
        "preset": "speakerCrop",
        "ranges": [{"startSample": 480, "endSample": 960, "crop": {"x": 0.1}}],
        "manualFixed": True,
    })

    assert plan.mode == "twoShot"
    assert plan.camera_group_id is None
    assert plan.segments == []
    assert plan.ranges[0]["startSample"] == 480


def test_over_shoulder_group_and_shot_plan_round_trip_with_camel_case():
    group = DialogueCameraGroup(
        id="CG1",
        visual_variant_id="V3",
        mode="overShoulder",
        character_reference_images={
            "A": {"artifactId": "char-a", "path": "a.png"},
            "B": {"artifactId": "char-b", "path": "b.png"},
        },
        scene_reference_image={"artifactId": "scene-1", "path": "scene.png"},
        camera_assets=[
            CameraAsset(
                id="CA-A",
                master_image={"artifactId": "ots-a", "path": "ots-a.png"},
                mode="overShoulder",
                subject_speaker="A",
                foreground_speaker="B",
                subject_region=Rect(x=0.2, y=0.1, width=0.5, height=0.7),
                foreground_mask={"artifactId": "mask-b", "path": "mask-b.png"},
            ),
            CameraAsset(
                id="CA-B",
                master_image={"artifactId": "ots-b", "path": "ots-b.png"},
                mode="overShoulder",
                subject_speaker="B",
                foreground_speaker="A",
            ),
        ],
    )
    variant = VisualVariant(id="V3", mode="overShoulder", camera_groups=[group])
    plan = ShotPlan(
        id="S3",
        mode="overShoulder",
        camera_group_id="CG1",
        segments=[
            ShotSegment(start_sample=0, end_sample=48_000, camera_asset_id="CA-A", speaker="A"),
            ShotSegment(start_sample=48_000, end_sample=96_000, camera_asset_id="CA-B", speaker="B", manual_fixed=True),
        ],
    )

    variant_payload = variant.model_dump(by_alias=True, mode="json")
    plan_payload = plan.model_dump(by_alias=True, mode="json")

    assert variant_payload["mode"] == "overShoulder"
    assert variant_payload["cameraGroups"][0]["cameraAssets"][0]["subjectSpeaker"] == "A"
    assert variant_payload["cameraGroups"][0]["sceneReferenceImage"]["artifactId"] == "scene-1"
    assert plan_payload["cameraGroupId"] == "CG1"
    assert plan_payload["segments"][1]["manualFixed"] is True
    restored_variant = VisualVariant.model_validate(variant_payload)
    restored_plan = ShotPlan.model_validate(plan_payload)
    assert restored_variant.mode == "overShoulder"
    assert restored_variant.camera_groups == variant.camera_groups
    assert restored_plan == plan


def test_over_shoulder_generation_merges_two_cameras_into_one_group(tmp_path):
    store = ProjectStore(tmp_path / "projects", debounce_ms=0)
    project = store.create("EP-OTS")
    first = apply_visual_variant(store, project, {
        "aspect": "landscape", "mode": "overShoulder", "cameraGroupId": "CG1",
        "cameraAssetId": "CA-A", "subjectSpeaker": "A", "foregroundSpeaker": "B",
        "artifactId": "img-a", "path": "a.png",
    })
    updated = store.load("EP-OTS")
    assert first[0].mode == "overShoulder"
    second = apply_visual_variant(store, updated, {
        "aspect": "landscape", "mode": "overShoulder", "cameraGroupId": "CG1",
        "cameraAssetId": "CA-B", "subjectSpeaker": "B", "foregroundSpeaker": "A",
        "artifactId": "img-b", "path": "b.png",
    })
    assert len(second) == 1
    saved = store.load("EP-OTS")
    assert len(saved.visual_variants) == 1
    cameras = saved.visual_variants[0].camera_groups[0].camera_assets
    assert [(c.id, c.subject_speaker, c.foreground_speaker) for c in cameras] == [
        ("CA-A", "A", "B"), ("CA-B", "B", "A"),
    ]
