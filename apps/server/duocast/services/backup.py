"""Portable project packages. No archive path is ever extracted verbatim."""
import copy
import hashlib
import json
import uuid
import zipfile
from pathlib import Path

from fastapi import HTTPException
from ..domain.project import Project

MAX_BYTES = 2 * 1024**3


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def referenced_entries(project, artifacts):
    entries = {e["artifactId"]: e for e in artifacts._manifest()["artifacts"]}
    pending = set(strings(project)) & entries.keys()
    included = {}
    while pending:
        key = pending.pop()
        if key in included:
            continue
        entry = entries[key]
        included[key] = entry
        pending.update((set(strings(entry.get("paramsSnapshot", {}))) & entries.keys()) - included.keys())
    return list(included.values())


def declared_asset_ids(value):
    """Only schema reference fields; prose and arbitrary metadata are not IDs."""
    scalar = {'artifactId', 'masterAudioAssetId', 'adoptedAudioAssetId', 'imageAssetId', 'videoAssetId'}
    plural = {'artifactIds', 'candidateAudioAssetIds'}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in scalar and isinstance(item, str) and item:
                yield item
            elif key in plural and isinstance(item, list):
                yield from (id for id in item if isinstance(id, str) and id)
            yield from declared_asset_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from declared_asset_ids(item)


def make_backup(project, artifacts, motion_store=None):
    data = project.model_dump(mode="json", by_alias=True)
    pack_snapshot = None
    if project.motion_pack and motion_store:
        pack = motion_store.load_version(project.motion_pack.id, project.motion_pack.version)
        if pack:
            pack_snapshot = pack.model_dump(mode='json', by_alias=True)
    entries = referenced_entries({'project':data,'motionPackSnapshot':pack_snapshot}, artifacts)
    root = artifacts.root / "backups"
    root.mkdir(exist_ok=True)
    target = root / (uuid.uuid4().hex + ".zip")
    missing = sorted(set(declared_asset_ids({'project':data,'pack':pack_snapshot})) - {e['artifactId'] for e in entries})
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        saved = []
        for entry in entries:
            file = Path(entry["path"])
            if not file.is_file() or artifacts._hash_file(str(file)) != entry["fileHash"]:
                missing.append(entry["artifactId"])
                continue
            item = copy.deepcopy(entry)
            item["member"] = f"media/{len(saved)}{file.suffix}"
            archive.write(file, item["member"])
            saved.append(item)
        archive.writestr("duocast-backup.json", json.dumps({"format": 1, "project": data, "artifacts": saved, "missing": missing,
            "motionPackSnapshot": pack_snapshot,
            "externalDependencies": {"fastTemplate": project.fast_template, "notice": "不包含密钥、模型和外置引擎；缺失媒体需重新生成"}}, ensure_ascii=False))
    return target


def import_backup(source, store, artifacts, motion_store=None):
    # Validate every member, metadata and hash before creating a project.
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if any(name.startswith(("/", "\\")) or ".." in Path(name).parts or "\\" in name or ":" in name for name in names):
                raise ValueError("备份含非法成员路径")
            if len(infos) > 20000 or len(set(names)) != len(names) or sum(i.file_size for i in infos) > MAX_BYTES:
                raise ValueError("备份超出容量或包含重复文件")
            if any(i.compress_size and i.file_size / i.compress_size > 1000 for i in infos):
                raise ValueError("备份压缩比例异常")
            meta_info = archive.getinfo("duocast-backup.json")
            if meta_info.file_size > 32 * 1024**2:
                raise ValueError("备份清单过大")
            meta = json.loads(archive.read(meta_info))
            if not isinstance(meta, dict) or meta.get("format") != 1 or not isinstance(meta.get("artifacts"), list):
                raise ValueError("不支持的备份版本")
            validated_project = Project.model_validate(meta["project"])
            pack_snapshot = meta.get('motionPackSnapshot')
            pack_draft = None
            if pack_snapshot is not None:
                from ..domain.motion_pack import MotionPack, MotionPackDraft
                pack = MotionPack.model_validate(pack_snapshot)
                pin = validated_project.motion_pack
                if not pin or (pin.id, pin.version) != (pack.id, pack.version):
                    raise ValueError('动作包快照与工程固定版本不一致')
                pack_draft = pack.model_dump(mode='json',by_alias=True,include=set(MotionPackDraft.model_fields))
                if motion_store is None:
                    raise ValueError('当前服务不支持动作包迁移')
            if validated_project.schema_version != 1:
                raise ValueError("不支持的工程结构版本，请使用对应版本软件迁移")
            validated = []
            asset_ids, asset_paths = set(), {}
            for entry in meta["artifacts"]:
                if (not isinstance(entry.get('artifactId'), str) or not entry['artifactId']
                        or not isinstance(entry.get('path'), str) or not entry['path']
                        or entry['artifactId'] in asset_ids):
                    raise ValueError('备份媒体标识或路径缺失、重复')
                if entry['path'] in asset_paths and asset_paths[entry['path']] != entry.get('fileHash'):
                    raise ValueError('同一媒体路径包含不同内容')
                asset_ids.add(entry['artifactId']); asset_paths[entry['path']] = entry.get('fileHash')
                member = entry["member"]
                if member not in names or not member.startswith("media/") or ".." in member or "\\" in member:
                    raise ValueError("备份媒体路径无效")
                if entry["kind"] not in {"audio", "image", "video", "subtitle", "mixed"}:
                    raise ValueError("备份媒体类型无效")
                digest = hashlib.sha256()
                with archive.open(member) as file:
                    for chunk in iter(lambda: file.read(1024*1024), b''):
                        digest.update(chunk)
                if digest.hexdigest() != entry["fileHash"]:
                    raise ValueError("备份媒体校验失败")
                validated.append(entry)
    except (KeyError, TypeError, ValueError, AttributeError, RuntimeError, zipfile.BadZipFile, OSError) as exc:
        raise HTTPException(422, "无法导入工程备份：" + str(exc)) from exc
    project_id = "IMPORT-" + uuid.uuid4().hex[:12]
    folder = artifacts.root / "imported" / project_id
    folder.mkdir(parents=True)
    id_map = {entry["artifactId"]: "RESTORED-" + uuid.uuid4().hex for entry in validated}
    path_map = {entry["path"]: str(folder / (id_map[entry["artifactId"]] + Path(entry["member"]).suffix)) for entry in validated}
    old_id = meta["project"]["id"]

    def rewrite(value):
        if isinstance(value, str):
            if value.startswith("/api/artifacts/"):
                for before, after in id_map.items():
                    if value == f"/api/artifacts/{before}/file":
                        return f"/api/artifacts/{after}/file"
            return id_map.get(value, path_map.get(value, value))
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        if isinstance(value, dict):
            out = {k: rewrite(v) for k, v in value.items()}
            for key in ('candidateSampleCounts', 'candidate_sample_counts'):
                if isinstance(value.get(key), dict):
                    out[key] = {id_map.get(id, id): count for id, count in value[key].items()}
            if out.get("id") == old_id and "scriptRevisions" in out:
                out["id"] = project_id
            if out.get("projectId") == old_id:
                out["projectId"] = project_id
            if out.get("imageAssetId") in id_map.values():
                out["imagePath"] = f"/api/artifacts/{out['imageAssetId']}/file"
            return out
        return value

    with zipfile.ZipFile(source) as archive:
        import shutil
        for entry in validated:
            file = Path(path_map[entry["path"]])
            with archive.open(entry['member']) as origin, file.open('wb') as target:
                shutil.copyfileobj(origin, target, length=1024*1024)
            artifacts.register(id_map[entry["artifactId"]], entry["kind"], str(file), entry.get("dependencyHash", ""), rewrite(entry.get("paramsSnapshot", {})), entry.get("workflowVersion"))
    data = rewrite(meta["project"])
    imported_pack = None
    if pack_draft is not None:
        imported_pack = motion_store.create(rewrite(pack_draft))
        data['motionPack'] = {'id':imported_pack.id,'version':imported_pack.version}
    data["id"] = project_id
    # Runtime-bound confirmations need a fresh check after relocation; media stays available.
    for approval in data.get("approvals", []):
        if approval.get("kind") in {"sample", "voice"}:
            approval["revokedAt"] = "imported"
            approval["revokedReason"] = "工程已迁移，请检查本机声音和视频运行依赖"
    created = store.create(project_id, data["title"])
    data.pop("revision", None)
    saved = store.apply(project_id, data, created.revision)
    store.flush()
    return {"projectId": saved.id, "missing": meta.get("missing", []),
            "summary": {"sourceProjectId": old_id, "mediaCount": len(validated),
                        "mediaBytes": sum(Path(path).stat().st_size for path in path_map.values()),
                        "scriptVersions": len(saved.script_revisions),
                        "recheck": ["声音与参考音频", "样片与生成规格"],
                        "motionPack": {'imported':imported_pack is not None,
                                       'id':imported_pack.id if imported_pack else None,
                                       'requiresReview':validated_project.motion_pack is not None},
                        "external": ["服务凭据", "模型与引擎", "外置动作模板"]},
            "notice": "已另存工程；模型/模板不包含在备份中，迁移后须重新检查。"}
