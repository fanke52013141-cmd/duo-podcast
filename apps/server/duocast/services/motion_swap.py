"""P1-C（42 号方案）：快速模式换角色/底图——派生新动作包再改，绝不改动共享包。

语义（与用户确认）：
- 动作包是全局共享资产。换角色/底图时**复制当前 pin 的包为新包**，在新包上改参考图，
  本工程 pin 新包；其他工程仍 pin 原包，不受影响。
- 新包经 MotionPackStore.create 生成，checks/reviews 天然为空 → readiness=false →
  现有渲染门槛（render.py validate_project_motion_binding / 前端 useMotionReadiness）
  自动拦截 → 引导用户走 prepare/inspect/review 重新准备。
- 无动作包的快速模板工程：人物/底图烧死在运行时文件，无法更换（明确拒绝，不静默无效）。

本模块只做「派生 + 失效 + 重绑定」的 CPU 可验逻辑；真实重新准备与渲染验收在 GPU 机器。
"""
from __future__ import annotations

from ..domain.motion_pack import MotionMediaRef, MotionPackDraft
from ..domain.project import MotionPackPin, Project
from .approvals import revoke


def derive_pack_with_image(motion_store, project: Project, *, image_ref: MotionMediaRef,
                           role: str | None = None, camera_id: str | None = None,
                           character_meta: dict | None = None):
    """从工程 pin 的包派生新包并替换参考图，返回 (new_pack, new_pin)。原包不变。"""
    pin = project.motion_pack
    if pin is None:
        raise ValueError("SWAP_NO_PACK: 本工程未绑定动作包；快速模板的人物/底图固定在运行时，无法更换，请新建已绑动作包的节目")
    source = motion_store.load_version(pin.id, pin.version)
    if source is None:
        raise ValueError("SWAP_PACK_MISSING: 绑定的动作包版本不存在")
    draft = MotionPackDraft.model_validate(source.model_dump(include=set(MotionPackDraft.model_fields)))
    if role is not None:
        if role not in draft.characters:
            raise ValueError(f"SWAP_ROLE: 动作包没有角色 {role}")
        update = {"reference_image": image_ref}
        if character_meta:
            if character_meta.get("characterId"):
                update["character_id"] = str(character_meta["characterId"])[:255]
            if character_meta.get("name"):
                update["name"] = str(character_meta["name"])[:128]
        draft.characters[role] = draft.characters[role].model_copy(update=update)
        suffix = f"换角色{role}"
    elif camera_id is not None:
        if not any(c.id == camera_id for c in draft.cameras):
            raise ValueError(f"SWAP_CAMERA: 动作包没有机位 {camera_id}")
        draft.cameras = [c.model_copy(update={"master_image": image_ref}) if c.id == camera_id else c
                         for c in draft.cameras]
        suffix = f"换机位{camera_id}底图"
    else:
        if not any(c.mode == "twoShot" for c in draft.cameras):
            raise ValueError("SWAP_CAMERA: 动作包没有同框机位可换底图")
        draft.cameras = [c.model_copy(update={"master_image": image_ref}) if c.mode == "twoShot" else c
                         for c in draft.cameras]
        suffix = "换底图"
    draft = draft.model_copy(update={"name": f"{draft.name}·{suffix}"[:128]})
    new_pack = motion_store.create(draft, pack_id=None)
    return new_pack, MotionPackPin(id=new_pack.id, version=new_pack.version)


def apply_image_swap(project_store, motion_store, project: Project, *, image_ref: MotionMediaRef,
                     role: str | None = None, camera_id: str | None = None,
                     character_meta: dict | None = None, expected_revision: int | None = None,
                     extra_patch: dict | None = None) -> dict:
    """派生新包 + 重绑定本工程 + 撤销样片确认，返回结果（含就绪状态与重新准备引导）。"""
    new_pack, new_pin = derive_pack_with_image(
        motion_store, project, image_ref=image_ref, role=role,
        camera_id=camera_id, character_meta=character_meta)
    patch = {
        "motion_pack": new_pin.model_dump(),
        # 换角色/底图属视觉输入变更：撤销样片确认（与 apply_visual_variant 同口径）
        "approvals": revoke(project.approvals, ("sample",), "更换动作包角色/底图"),
        **(extra_patch or {}),
    }
    saved = project_store.apply(project.id, patch,
                                expected_revision=expected_revision if expected_revision is not None else project.revision)
    check = motion_store.readiness(new_pack.id, saved.shot_style, new_pack.version)
    return {
        "projectId": project.id,
        "motionPack": new_pin.model_dump(),
        "derivedFrom": {"id": project.motion_pack.id, "version": project.motion_pack.version},
        "ready": check["ready"],
        "missing": check["missing"],
        "notice": "已派生新动作包并绑定本工程（原包与其他工程不受影响）。新包尚未准备：请重新准备动作素材、检查并人工采用后再渲染。",
        "project": saved,
    }