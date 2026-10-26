"""Read-only delivery readiness. Historical exports retain their frozen inputs."""
from pathlib import Path
from .render_inputs import dependency_hash


def delivery_check(project, assets, pronunciation=None):
    from .render_inputs import audio_inputs_match
    issues = []
    def add(code, message, target, level="error"):
        issues.append({"code": code, "message": message, "target": target, "level": level})
    def intact(asset_id, label, target):
        entry = assets.by_id(asset_id) if asset_id else None
        if not entry or not Path(entry["path"]).is_file():
            add("missing_" + label, label + "文件缺失，请重新生成或恢复备份", target)
            return None
        if not entry.get("fileHash") or entry["fileHash"] != assets._hash_file(entry["path"]):
            add("corrupt_" + label, label + "完整性校验失败", target)
            return None
        return entry
    if not project.current_draft_revision:
        add("no_script", "尚无当前脚本", "script")
    for kind, target, label in (("script", "script", "脚本"), ("voice", "voice", "声音")):
        if not any(a.kind == kind and a.input_revision_id == project.current_draft_revision and a.decision == "accepted" and not a.revoked_at for a in project.approvals):
            add("unconfirmed_" + kind, label + "尚未确认当前版本", target)
    if not audio_inputs_match(project, pronunciation):
        add("stale_audio", "声音缺失或与当前脚本/声线/读音不一致", "voice")
    if project.audio_timeline:
        intact(project.audio_timeline.master_audio_asset_id, "母轨", "voice")
        for unit in project.audio_timeline.units:
            intact(unit.adopted_audio_asset_id, "声音单元 " + unit.id, "voice")
    variant = next((v for v in project.visual_variants if v.id == project.selected_visual_variant_id), None)
    if variant is None and not project.selected_visual_variant_id and project.visual_variants:
        variant = project.visual_variants[-1]
    if variant is None:
        add("no_visual", "尚未选定有效画面", "visual")
    else:
        def visual_refs(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k == "artifactId" and v:
                        yield v
                    else:
                        yield from visual_refs(v)
            elif isinstance(value, list):
                for v in value:
                    yield from visual_refs(v)
        refs = set(visual_refs(variant.model_dump(by_alias=True)))
        if not refs:
            add("no_visual_media", "当前画面没有可校验的媒体引用", "visual")
        for ref in sorted(refs):
            intact(ref, "画面 " + ref, "visual")
    sample = next((a for a in reversed(project.approvals) if a.kind == "sample" and a.input_revision_id == project.current_draft_revision and a.decision == "accepted" and not a.revoked_at), None)
    if not sample:
        add("no_sample", "样片尚未确认或已失效", "visual")
    else:
        from .render_inputs import visual_dependency_hash
        if sample.spec.get("dependencyHash") not in {visual_dependency_hash(project), dependency_hash(project)}:
            add("stale_sample", "样片使用的脚本或画面已改变", "visual")
        spec = sample.spec.get("renderSpec", {})
        if any(spec.get(k) != v for k, v in project.render_spec.items()):
            add("sample_spec", "样片与当前生成规格不一致，请重新检查样片", "visual")
        if sample.spec.get("simulated"):
            add("demo_sample", "样片为演示结果，不能证明真实画质", "visual", "warning")
    manifest = project.output_manifest
    if not manifest or not manifest.get("artifactIds"):
        add("no_output", "尚无已采用的正式成片", "render")
    else:
        entry = intact(manifest["artifactIds"][0], "成片", "render")
        if entry:
            frozen = entry.get("paramsSnapshot", {})
            if frozen.get("dependencyHash") != dependency_hash(project):
                add("stale_output", "已采用成片与当前输入不同；可导出历史版本，交付当前版本前请重新生成并采用", "render")
    return {"ready": not any(i["level"] == "error" for i in issues), "issues": issues,
            "outputVersion": project.output_version, "notice": "检查文件与版本一致性，不替代真实观看和试听；历史交付仍使用冻结快照"}
