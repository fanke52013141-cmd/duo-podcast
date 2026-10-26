"""Recoverable maintenance and backup operations for the local workspace."""
import json
import os
import shutil
import uuid
import tempfile
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from ..services.backup import make_backup, import_backup, MAX_BYTES

router = APIRouter(prefix="/api", tags=["maintenance"])


def safe_child(root, name):
    if not isinstance(name, str) or name in {"", ".", ".."} or Path(name).name != name or any(c in name for c in "/\\:"):
        raise HTTPException(422, "非法回收记录")
    path = root / name
    if path.resolve().parent != root.resolve() or path.is_symlink():
        raise HTTPException(422, "非法回收路径")
    return path


def idle(request):
    if any(j.status.value in {"queued", "running", "cancel_requested", "pause_requested", "recovering"} for j in request.app.state.job_manager.list()):
        raise HTTPException(409, "仍有进行中的任务，请暂停并等待停止后再维护")


@router.get("/projects/{project_id}/backup")
def backup(project_id: str, request: Request):
    store = request.app.state.project_store
    project = store.load(project_id)
    if project is None:
        raise HTTPException(404, "工程不存在")
    store.flush()
    return FileResponse(make_backup(project, request.app.state.artifacts), media_type="application/zip", filename=f"{project_id}-工程备份.zip")


@router.post("/projects/import-backup")
async def restore_package(request: Request):
    idle(request)
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "请选择工程备份 ZIP")
    with tempfile.TemporaryFile() as source:
        total = 0
        while chunk := await upload.read(1024*1024):
            total += len(chunk)
            if total > MAX_BYTES:
                raise HTTPException(422, "备份超过 2GB，请使用本机目录备份方式")
            source.write(chunk)
        source.seek(0)
        return import_backup(source, request.app.state.project_store, request.app.state.artifacts)


@router.get("/maintenance")
def maintenance(request: Request):
    root = request.app.state.artifacts.root.parent
    def size(path):
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink()) if path.exists() else 0
    cache = root / "cache"
    trash = root / "trash"
    records = []
    if trash.is_dir():
        for folder in trash.iterdir():
            if not folder.is_dir() or folder.is_symlink():
                continue
            file = folder / "project.json"
            if file.is_file():
                try:
                    p = json.loads(file.read_text(encoding="utf-8"))
                    records.append({"token": folder.name, "projectId": p["id"], "title": p["title"], "kind": "project"})
                except (ValueError, KeyError):
                    continue
            elif folder.name.startswith("cache-"):
                records.append({"token": folder.name, "kind": "cache"})
    assets = request.app.state.artifacts
    categories = {}
    seen = set()
    for entry in assets._manifest()["artifacts"]:
        path = Path(entry["path"])
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        categories[entry["kind"]] = categories.get(entry["kind"], 0) + path.stat().st_size
    projects = []
    from ..services.backup import referenced_entries
    for project_id in request.app.state.project_store.list_ids():
        project = request.app.state.project_store.load(project_id)
        entries = referenced_entries(project.model_dump(by_alias=True), assets)
        unique = {e["path"] for e in entries}
        projects.append({"id": project_id, "title": project.title, "referencedBytes": sum(Path(p).stat().st_size for p in unique if Path(p).is_file())})
    return {"freeBytes": shutil.disk_usage(root).free, "cacheBytes": size(cache), "mediaBytes": categories, "projects": projects, "trash": records,
            "notice": "只移动可重建的查找缓存；被采用媒体、视频片段及模型缓存不自动删除。工程占用可能共享，不可直接相加。"}


@router.post("/maintenance/cache-trash")
def trash_cache(request: Request):
    idle(request)
    root = request.app.state.artifacts.root.parent
    source = safe_child(root, "cache")
    target = root / "trash" / ("cache-" + uuid.uuid4().hex)
    target.parent.mkdir(exist_ok=True)
    if source.is_dir():
        os.replace(source, target)
    source.mkdir(exist_ok=True)
    return {"token": target.name, "notice": "查找缓存已移入回收区，媒体保持完整"}


@router.post("/maintenance/restore/{token}")
def restore_trash(token: str, request: Request):
    idle(request)
    store = request.app.state.project_store
    root = store.root.parent
    source = safe_child(root / "trash", token)
    if not source.is_dir():
        raise HTTPException(404, "回收记录不存在")
    if token.startswith("cache-"):
        target = safe_child(root, "cache")
        if target.exists() and any(target.iterdir()):
            raise HTTPException(409, "新缓存已有内容，请先移入回收区再恢复旧缓存")
        if target.exists():
            target.rmdir()
        os.replace(source, target)
        return {"restored": "cache"}
    file = source / "project.json"
    if not file.is_file() or file.is_symlink():
        raise HTTPException(422, "回收工程清单无效")
    from ..domain.project import Project
    project = Project.model_validate_json(file.read_text(encoding="utf-8"))
    target = store._path(project.id).parent
    if target.exists() or store.load(project.id):
        raise HTTPException(409, "同 ID 工程已存在，不能覆盖；请先备份并另存")
    jobs = source / "jobs"
    if jobs.is_dir():
        # Do not resurrect old jobs into the active scheduler.
        os.replace(jobs, source / "archived_jobs")
    os.replace(source, target)
    return {"restored": project.id}
