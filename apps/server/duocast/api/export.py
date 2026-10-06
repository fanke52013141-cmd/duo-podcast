"""Export the adopted immutable video and its authoritative audio timeline."""
import json
import uuid
import wave
import zipfile
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from ..domain.project import Project

router = APIRouter(prefix="/api/projects/{project_id}/export", tags=["export"])

def srt_time(seconds):
    millis = round(seconds*1000)
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"

@router.get("/package")
def export_package(project_id: str, request: Request, version: str | None = None):
    p = request.app.state.project_store.load(project_id)
    if p is None:
        raise HTTPException(404, "工程不存在")
    manifest = next((v for v in p.output_history if v["version"] == version), None) if version else p.output_manifest
    if version and p.output_manifest and p.output_manifest["version"] == version:
        manifest = p.output_manifest
    if not manifest or not manifest.get("artifactIds"):
        raise HTTPException(409, "请先生成含交付清单的完整成片")
    assets = request.app.state.artifacts
    video = assets.by_id(manifest["artifactIds"][0])
    if not video or not Path(video["path"]).is_file():
        raise HTTPException(409, "成片文件缺失")
    if video.get("fileHash") != assets._hash_file(video["path"]):
        raise HTTPException(409, "成片文件完整性校验失败")
    # Deliver all media from the render's frozen input, including historical versions.
    frozen = video.get("paramsSnapshot", {}).get("projectSnapshot")
    if not frozen or frozen.get("id") != project_id:
        raise HTTPException(409, "成片缺少本工程的声画快照")
    p = Project.model_validate(frozen)
    t = p.audio_timeline
    master = assets.by_id(t.master_audio_asset_id)
    if not master or not Path(master["path"]).is_file():
        raise HTTPException(409, "主音轨缺失")
    if master.get("fileHash") != assets._hash_file(master["path"]):
        raise HTTPException(409, "主音轨完整性校验失败")
    revision = next(r for r in p.script_revisions if r.id == t.revision_id)
    turns = {turn.id: turn for turn in revision.turns}
    import hashlib
    key = hashlib.sha256(json.dumps({"version": manifest["version"], "video":video["fileHash"], "audio":master.get("fileHash"), "script":frozen["scriptRevisions"], "format":2},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    folder = assets.root / "export" / key
    folder.mkdir(parents=True, exist_ok=True)
    target = folder/"delivery.zip"
    if target.is_file():
        return FileResponse(target, media_type="application/zip", filename=f"{p.id}-{manifest['version']}.zip")
    with wave.open(master["path"]) as reader:
        if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (1, 2, t.sample_rate):
            raise HTTPException(409, "主音轨格式不符合交付规范")
        pcm = reader.readframes(reader.getnframes())
    if len(pcm) // 2 != t.sample_count:
        raise HTTPException(409, "主音轨样本数与时间轨不一致")
    tracks = {s:bytearray(len(pcm)) for s in ("A","B")}
    subtitles = []
    for index, unit in enumerate(t.units):
        offset = t.unit_offsets[unit.id]; stop = offset + unit.sample_count
        turn = turns[unit.turn_id]
        tracks[turn.speaker][offset*2:stop*2] = pcm[offset*2:stop*2]
        text = "\n".join(line.display_text for line in turn.lines)
        subtitles.append(f"{index+1}\n{srt_time(offset/t.sample_rate)} --> {srt_time(stop/t.sample_rate)}\n{text}\n")
    for speaker, data in tracks.items():
        with wave.open(str(folder/f"{speaker}.wav"),"wb") as writer:
            writer.setparams((1,2,t.sample_rate,0,"NONE","not compressed")); writer.writeframes(data)
    (folder/"dialogue.srt").write_text("\n".join(subtitles),encoding="utf-8-sig")
    (folder/"manifest.json").write_text(json.dumps({**manifest,"subtitleTiming":"turn audio intervals", "dependencyHash":video["paramsSnapshot"].get("dependencyHash")},ensure_ascii=False,indent=2),encoding="utf-8")
    partial = folder/f"{uuid.uuid4().hex}.partial.zip"
    with zipfile.ZipFile(partial,"w",compression=zipfile.ZIP_STORED) as archive:
        archive.write(video["path"],"video.mp4")
        archive.write(master["path"],"master.wav")
        for name in ("A.wav","B.wav","dialogue.srt","manifest.json"):
            archive.write(folder/name,name)
    partial.replace(target)
    return FileResponse(target, media_type="application/zip", filename=f"{p.id}-{manifest['version']}.zip")
