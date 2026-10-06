"""Real reverse-angle planning on the authoritative audio sample clock."""
import math


def camera_pair(visual):
    groups = visual.get("cameraGroups", [])
    if len(groups) != 1:
        raise ValueError("过肩变体必须包含一组明确的 A/B 机位")
    cameras = groups[0].get("cameraAssets", [])
    pair = {c.get("subjectSpeaker"): c for c in cameras}
    if len(cameras) != 2 or set(pair) != {"A", "B"}:
        raise ValueError("请先准备 A 和 B 两个过肩机位")
    for speaker, camera in pair.items():
        if camera.get("mode") != "overShoulder" or camera.get("foregroundSpeaker") != ("B" if speaker == "A" else "A"):
            raise ValueError("过肩机位主体和前景角色不匹配")
        region = camera.get("subjectRegion", {})
        values = [region.get(k) for k in ("x", "y", "width", "height")]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError("主体区域必须为有效归一化坐标")
        x, y, width, height = values
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            raise ValueError("主体区域超出画面")
        if not camera.get("masterImage", {}).get("artifactId"):
            raise ValueError("机位缺少已登记的图片资产")
    if pair["A"]["masterImage"]["artifactId"] == pair["B"]["masterImage"]["artifactId"]:
        raise ValueError("正反打必须使用不同机位图片")
    return pair


def shot_plan(project, visual):
    pair = camera_pair(visual)
    timeline = project["audioTimeline"]
    revision = next(r for r in project["scriptRevisions"] if r["id"] == timeline["revisionId"])
    speakers = {t["id"]: t["speaker"] for t in revision["turns"]}
    units = sorted(timeline["units"], key=lambda u: timeline["unitOffsets"][u["id"]])
    if not units:
        raise ValueError("配音没有有效话轮")
    plan = []
    for n, unit in enumerate(units):
        speaker = speakers[unit["turnId"]]
        start = timeline["unitOffsets"][unit["id"]] if n else 0
        end = timeline["unitOffsets"][units[n + 1]["id"]] if n + 1 < len(units) else timeline["sampleCount"]
        if end <= start:
            raise ValueError("过肩镜头话轮时间重叠或顺序无效")
        if timeline["unitOffsets"][unit["id"]] + unit["sampleCount"] > end:
            raise ValueError("过肩正反打暂不支持重叠发言")
        if plan and plan[-1]["speaker"] == speaker:
            plan[-1]["endSample"] = end
        else:
            plan.append({"startSample": start, "endSample": end, "speaker": speaker,
                         "cameraAssetId": pair[speaker]["id"]})
    return plan


def shot_chunks(shot, rate):
    """Balance long turns so a five-second boundary never leaves a tiny shot."""
    start, end = shot["startSample"], shot["endSample"]
    count = math.ceil((end - start) / (5 * rate))
    boundaries = [start + round((end - start) * n / count) for n in range(count + 1)]
    return list(zip(boundaries, boundaries[1:]))
