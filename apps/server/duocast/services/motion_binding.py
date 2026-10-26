"""Resolve explicit project selections once; render always rechecks pinned media."""
from ..domain.motion_pack import MotionPack, ShotStyle
from ..domain.project import MotionPackPin, Project
from ..storage.motion_packs import MotionPackStore


def resolve_motion_binding(store: MotionPackStore, payload: dict, style: ShotStyle) -> MotionPackPin:
    if not isinstance(payload, dict) or set(payload) - {"id", "version"} or "id" not in payload:
        raise ValueError("motionPack accepts only id and optional version")
    pack_id, version = payload["id"], payload.get("version")
    if not isinstance(pack_id, str) or version is not None and not isinstance(version, str):
        raise ValueError("Invalid motion pack identity")
    pack = store.load(pack_id) if version is None else store.load_version(pack_id, version)
    if pack is None:
        raise ValueError("Motion pack version does not exist")
    pin = MotionPackPin(id=pack.id, version=pack.version)
    check = store.readiness(pin.id, style, pin.version)
    if not check["ready"]:
        raise ValueError("Motion pack is not usable for shot style: " + ",".join(check["missing"]))
    return pin


def validate_project_motion_binding(project: Project, store: MotionPackStore) -> MotionPack | None:
    if project.motion_pack is None:
        return None
    pin = project.motion_pack
    resolve_motion_binding(store, pin.model_dump(), project.shot_style)
    return store.load_version(pin.id, pin.version)


def motion_binding_dependency(project: Project) -> dict:
    return {"motionPack": project.motion_pack.model_dump() if project.motion_pack else None,
            "shotStyle": project.shot_style}
