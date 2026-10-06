"""Episode-wide camera decisions and role-aware model instructions."""
from copy import deepcopy


def camera_plan(project, mode="speaker", minimum=2.0):
    timeline = project["audioTimeline"]
    total = timeline["sampleCount"] / timeline["sampleRate"]
    if mode == "twoShot":
        return [{"start": 0.0, "end": total, "camera": "twoShot", "reason": "locked"}]
    revision = next(r for r in project["scriptRevisions"] if r["id"] == timeline["revisionId"])
    turns = {t["id"]: t for t in revision["turns"]}
    starts = [0.0]
    for unit in timeline["units"][1:]:
        at = timeline["unitOffsets"][unit["id"]] / timeline["sampleRate"]
        if at - starts[-1] >= minimum and total - at >= minimum:
            starts.append(at)
    shots = []
    for index, (start, end) in enumerate(zip(starts, starts[1:] + [total])):
        unit = next((u for u in reversed(timeline["units"])
                     if timeline["unitOffsets"][u["id"]] / timeline["sampleRate"] <= start + 0.0001), None)
        turn = turns.get(unit["turnId"], {}) if unit else {}
        closing = turn.get("intent") == "summarize" or index == len(starts) - 1
        camera = "twoShot" if index == 0 or closing else turn.get("speaker", "twoShot")
        override = project.get("shotCameraOverrides", {}).get(turn.get("id"))
        if override:
            camera = override
        if shots and shots[-1]["camera"] == camera:
            shots[-1]["end"] = end
        else:
            shots.append({"start": start, "end": end, "camera": camera,
                          "reason": "manual" if override else "opening" if index == 0 else "closing" if closing else "speaker"})
    return shots


def crop_region(region):
    """Static asset-relative portrait: stable without pretending to track faces."""
    x, y, width, height = (float(region[k]) for k in ("x", "y", "width", "height"))
    return {"x": x, "y": y, "width": width, "height": height * 0.55}


def adaptive_prompt(performance, regions, start, end, warmup=0.32):
    def host(speaker):
        region = regions[speaker]
        center = region["x"] + region["width"] / 2
        return "the host on the " + ("left" if center < 0.5 else "right")
    parts = [p for p in performance if p["end"] > start and p["start"] < end]
    opening = ""
    if parts:
        first = parts[0]
        opening = (f"At the beginning, {host(first['speaker'])} speaks; {host(first['listener'])} "
                   "looks toward the speaker and listens attentively with relaxed closed lips. ")
    events = []
    for index, part in enumerate(parts):
        a, b = max(start, part["start"])-start+warmup, min(end, part["end"])-start+warmup
        reaction = "makes one small acknowledging nod" if index % 2 == 0 else "gently adjusts posture"
        events.append(f"From {a:.2f} to {b:.2f} seconds, {host(part['speaker'])} speaks with natural lip motion "
                      f"and restrained hand gestures; {host(part['listener'])} keeps lips relaxed and closed, "
                      f"looks toward the speaker, blinks naturally and {reaction}.")
    return opening + ("A continuous natural live-action podcast conversation. Reactions are subtle and occasional; "
                      "quiet attentive moments are natural. The hosts react independently, without synchronized "
                      "or repetitive nodding. Preserve both identities, clothing, hands, microphones and studio. ") + " ".join(events)


def clipped_camera_plan(plan, start, end):
    return [{**deepcopy(shot), "start": max(start, shot["start"]), "end": min(end, shot["end"])}
            for shot in plan if shot["end"] > start and shot["start"] < end]
