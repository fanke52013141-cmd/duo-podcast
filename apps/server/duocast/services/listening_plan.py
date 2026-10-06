"""Deterministic, content-aware listening plans frozen into render jobs."""
import hashlib
import math

VERSION = "dialogue-listening-v1"
LABELS = {"acknowledge": "轻微认同", "curious": "关注疑问", "reflect": "理解总结", "attentive": "安静倾听"}


def listening_plan(project, visual_id, seed=1):
    timeline = project["audioTimeline"]
    total = timeline["sampleCount"] / timeline["sampleRate"]
    revision = next(r for r in project["scriptRevisions"] if r["id"] == timeline["revisionId"])
    turns = {t["id"]: t for t in revision["turns"]}
    visual = next(v for v in project["visualVariants"] if v["id"] == visual_id)
    regions = visual["personRegions"]
    centers = {s: regions[s]["x"] + regions[s]["width"] / 2 for s in ("A", "B")}
    # Restrict compositing to non-overlapping left/right regions. Crossing gestures
    # still require visual review; a rectangle cannot establish occlusion safety.
    left, right = sorted(regions.values(), key=lambda r: r["x"])
    gap_left, gap_right = left["x"] + left["width"], right["x"]
    safe_layout = gap_left <= gap_right and 0 < gap_left <= gap_right < 1
    midpoint = (gap_left + gap_right) / 2
    band = [max(gap_left-0.02, midpoint-0.02), min(gap_right+0.02, midpoint+0.02)]
    segments = []
    reacted_turns = set()
    for index in range(math.ceil(total / 5)):
        start, end = index*5, min(total, (index+1)*5)
        units = [u for u in timeline["units"] if timeline["unitOffsets"][u["id"]]/timeline["sampleRate"] < end
                 and (timeline["unitOffsets"][u["id"]]+u["sampleCount"])/timeline["sampleRate"] > start]
        active = {turns[u["turnId"]]["speaker"] for u in units}
        reason = "角色交接，保留原表演" if len(active) > 1 else "无有效发言，保留原表演"
        eligible = len(active) == 1 and safe_layout and end-start >= 1.5
        segment = {"start": start, "end": end, "eligible": eligible, "reason": reason,
                   "turnIds": [u["turnId"] for u in units]}
        if eligible:
            speaker = next(iter(active)); listener = "B" if speaker == "A" else "A"
            texts = "".join(line.get("spokenText") or line.get("displayText", "")
                            for u in units for line in turns[u["turnId"]].get("lines", []))
            intents = {turns[u["turnId"]].get("intent") for u in units}
            reaction = "curious" if "?" in texts or "？" in texts or "probe" in intents else (
                "reflect" if "summarize" in intents else "acknowledge" if texts.startswith(("对", "是的", "没错", "确实")) else "attentive")
            turn_ids = {u["turnId"] for u in units}
            if turn_ids & reacted_turns:
                reaction = "attentive"
            elif reaction != "attentive":
                reacted_turns.update(turn_ids)
            token = hashlib.sha256(f"{VERSION}|{seed}|{listener}|{start}|{texts}".encode()).digest()
            at = round((end-start) * (0.40 + token[0]/255*0.18), 2)
            segment.update({"speaker": speaker, "listener": listener, "reaction": reaction,
                "label": LABELS[reaction], "reason": "按台词意图安排一次轻微反应，其他时间自然倾听",
                "reactionAtSec": at, "amplitude": "subtle" if token[1]%2 else "very_subtle",
                "referenceSeed": int.from_bytes(token[2:6], "big"), "blendBand": band,
                "listenerSide": "left" if centers[listener] < centers[speaker] else "right"})
        elif len(active) == 1:
            segment["reason"] = "角色区域不适合分层或片段过短，保留原表演"
        segments.append(segment)
    return {"version": VERSION, "seed": seed, "segments": segments,
            "referenceCount": sum(s["eligible"] for s in segments),
            "requiresVisualReview": True}


def listening_prompt(segment):
    host = "the host on the " + segment["listenerSide"]
    reactions = {"acknowledge": "one very small acknowledging nod", "curious": "a slight eyebrow lift and small attentive lean",
                 "reflect": "one slow small nod of understanding", "attentive": "a subtle shoulder relaxation"}
    at = segment["reactionAtSec"] + 0.32
    return (f"A continuous live action podcast in a fixed wide camera. {host} quietly looks toward the other host. "
            f"Keep the listener's lips relaxed and closed throughout. Around {at:.2f} seconds, the listener makes "
            f"{reactions[segment['reaction']]}, with {segment['amplitude'].replace('_',' ')} amplitude, then settles comfortably. "
            "Natural occasional blinking and breathing. Quiet pauses between reactions. No repeated nodding, "
            "no rhythmic movement, no mandatory return to the front camera. The other host stays seated. "
            "Preserve both identities, clothes, hands, table, microphones and studio. No crossed arms between hosts.")


def reference_workflow(template, anchor_name, segment, prefix):
    from copy import deepcopy
    workflow = deepcopy(template)
    workflow["122"]["inputs"].pop("multitalk_model", None)
    workflow["133"]["inputs"]["image"] = anchor_name
    workflow["135"]["inputs"]["positive_prompt"] = listening_prompt(segment)
    workflow["192"] = {"class_type": "WanVideoImageToVideoEncode", "inputs": {
        "vae": ["129", 0], "width": ["213", 3], "height": ["213", 4],
        "num_frames": 4*math.ceil(((segment["end"]-segment["start"])+0.32)*25/4)+1,
        "noise_aug_strength": 0.0, "start_latent_strength": 1.0, "end_latent_strength": 1.0,
        "force_offload": True, "start_image": ["213", 0], "clip_embeds": ["193", 0],
        "fun_or_fl2v_model": False, "tiled_vae": True}}
    workflow["199"]["inputs"].pop("multitalk_embeds", None)
    workflow["199"]["inputs"].update({"seed": segment["referenceSeed"], "cfg": 2.0})
    workflow["229"]["inputs"].pop("audio", None)
    workflow["229"]["inputs"]["filename_prefix"] = prefix
    needed = set()
    def visit(key):
        if key in needed: return
        needed.add(key)
        for value in workflow[key]["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and value[0] in workflow:
                visit(value[0])
    visit("229")
    return {key: value for key, value in workflow.items() if key in needed}
