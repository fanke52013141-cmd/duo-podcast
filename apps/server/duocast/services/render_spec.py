"""Shared preview/final output options. Explicit requests override saved defaults."""
import shutil
from fastapi import HTTPException


def render_options(project, payload, root):
    merged = {**project.render_spec, **payload}
    timeline = project.audio_timeline
    duration = timeline.sample_count / timeline.sample_rate if timeline and timeline.sample_rate else 0
    reserve = max(256 * 1024**2, int(duration * 12 * 1024**2))
    if shutil.disk_usage(root).free < reserve:
        raise HTTPException(409, "磁盘可用空间不足，请在工程维护中检查占用；已有媒体保留")
    return merged


def representative_range(project):
    timeline = project.audio_timeline
    if not timeline or not timeline.units or timeline.sample_rate <= 0:
        return None
    revision = next((r for r in project.script_revisions if r.id == timeline.revision_id), None)
    if not revision:
        return None
    speakers = {t.id: t.speaker for t in revision.turns}
    units = timeline.units
    for index in range(len(units)-2):
        if [speakers.get(u.turn_id) for u in units[index:index+3]] in (["A", "B", "A"], ["B", "A", "B"]):
            start = timeline.unit_offsets[units[index].id] / timeline.sample_rate
            end = (timeline.unit_offsets[units[index+2].id] + units[index+2].sample_count) / timeline.sample_rate
            return {"start": start, "end": min(end, timeline.sample_count / timeline.sample_rate), "reason": "包含双方交替与角色交接"}
    return {"start": 0, "end": min(15, timeline.sample_count / timeline.sample_rate), "reason": "未找到三条交替话轮，取开头代表区间，请人工检查覆盖范围"}
