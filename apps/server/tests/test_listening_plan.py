import copy
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from duocast.services.listening_plan import listening_plan, listening_prompt, reference_workflow
from duocast.services.render_inputs import validate_render_options
from test_render_dependencies import project


def episode():
    return {"audioTimeline": {"sampleRate": 48000, "sampleCount": 480000, "revisionId": "R",
        "unitOffsets": {"U1": 0, "U2": 240000}, "units": [
            {"id": "U1", "turnId": "T1", "sampleCount": 240000},
            {"id": "U2", "turnId": "T2", "sampleCount": 240000}]},
        "scriptRevisions": [{"id": "R", "turns": [
            {"id": "T1", "speaker": "A", "intent": "explain", "lines": [{"spokenText": "为什么这样？"}]},
            {"id": "T2", "speaker": "B", "intent": "summarize", "lines": [{"spokenText": "归纳一下结果。"}]}]}],
        "visualVariants": [{"id": "V", "personRegions": {
            "A": {"x": 0, "width": 0.5}, "B": {"x": 0.5, "width": 0.5}}}]}


def test_entire_episode_assigns_each_silent_role_and_intent():
    plan = listening_plan(episode(), "V")
    assert plan["referenceCount"] == 2
    assert [(s["listener"], s["reaction"]) for s in plan["segments"]] == [("B", "curious"), ("A", "reflect")]
    assert plan["segments"][0]["listenerSide"] == "right"
    assert plan["requiresVisualReview"]


def test_handoffs_and_speech_overlap_are_never_overlaid():
    data = episode(); data["audioTimeline"]["unitOffsets"]["U2"] = 200000
    plan = listening_plan(data, "V")
    assert not plan["segments"][0]["eligible"]
    assert "角色交接" in plan["segments"][0]["reason"]


def test_variations_are_reproducible_and_independent_between_segments():
    first = listening_plan(episode(), "V", 1)
    assert first == listening_plan(episode(), "V", 1)
    assert first != listening_plan(episode(), "V", 2)
    assert len({s["referenceSeed"] for s in first["segments"]}) == 2
    for segment in first["segments"]:
        assert 0 < segment["reactionAtSec"] < segment["end"]-segment["start"]


@pytest.mark.parametrize("text,intent,expected", [
    ("对，确实如此。", "explain", "acknowledge"), ("普通解释。", "explain", "attentive"),
    ("Let us ask?", "explain", "curious"), ("继续解释", "probe", "curious"),
    ("一句总结", "summarize", "reflect")])
def test_text_and_intent_change_reaction(text, intent, expected):
    data=episode(); turn=data["scriptRevisions"][0]["turns"][0]
    turn["lines"][0]["spokenText"]=text; turn["intent"]=intent
    assert listening_plan(data, "V")["segments"][0]["reaction"] == expected


def test_unsafe_layout_and_short_tail_preserve_speaking_video():
    data=episode(); data["visualVariants"][0]["personRegions"]["A"]["width"]=0.7
    assert listening_plan(data, "V")["referenceCount"] == 0
    data=episode();data["audioTimeline"]["sampleCount"]=250000
    assert not listening_plan(data, "V")["segments"][-1]["eligible"]


def test_prompt_has_closed_lips_timed_event_without_loop():
    segment=listening_plan(episode(), "V")["segments"][0]
    prompt=listening_prompt(segment)
    assert "host on the right" in prompt and "closed throughout" in prompt
    assert "No repeated nodding" in prompt and "no mandatory return" in prompt
    assert f"{segment['reactionAtSec']+0.32:.2f} seconds" in prompt


def test_long_turn_does_not_repeat_nod_or_question_reaction_every_five_seconds():
    data=episode(); timeline=data["audioTimeline"]
    timeline.update({"sampleCount":720000,"units":[{"id":"U1","turnId":"T1","sampleCount":720000}],"unitOffsets":{"U1":0}})
    assert [s["reaction"] for s in listening_plan(data,"V")["segments"]] == ["curious","attentive","attentive"]


def test_i2v_workflow_isolated_from_speaking_audio_and_template():
    template=json.loads((Path(__file__).parents[1]/"duocast/adapters/multitalk_workflow.json").read_text(encoding="utf-8"))
    original=copy.deepcopy(template); segment=listening_plan(episode(), "V")["segments"][0]
    workflow=reference_workflow(template, "own-anchor.png", segment, "completion/own")
    assert template == original
    assert workflow["192"]["class_type"] == "WanVideoImageToVideoEncode"
    assert "multitalk_model" not in workflow["122"]["inputs"]
    assert "multitalk_embeds" not in workflow["199"]["inputs"]
    assert "audio" not in workflow["229"]["inputs"]
    assert workflow["199"]["inputs"]["seed"] == segment["referenceSeed"]
    assert "198" not in workflow and "241" not in workflow


@pytest.mark.parametrize("seed", [-1, True, 1.1, 10001, None])
def test_invalid_listening_variant_rejected(seed):
    with pytest.raises(HTTPException): validate_render_options(project(), {"listeningSeed": seed})


def test_multiple_segments_render_and_repeat_without_model_sampling(tmp_path):
    """Real ffmpeg compositing/export; only expensive I2V sampling is substituted."""
    import asyncio
    import shutil
    import subprocess
    import wave
    from duocast.storage.artifacts import ArtifactStore
    from duocast.adapters.comfyui_video import ComfyUIVideoProvider
    if not shutil.which("ffmpeg"): pytest.skip("ffmpeg unavailable")
    store=ArtifactStore(tmp_path/"artifacts")
    native=store.root/"video_motion"/"source";native.mkdir(parents=True)
    def video(path, color, duration):
        subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i",f"color=c={color}:s=320x180:r=25",
                        "-t",str(duration),"-c:v","libx264","-pix_fmt","yuv420p",str(path)],check=True)
    image=tmp_path/"image.png"
    subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i","color=c=black:s=320x180",
                    "-frames:v","1",str(image)],check=True)
    store.register("IMG","image",str(image),"image")
    audio=tmp_path/"audio.wav"
    with wave.open(str(audio),"wb") as w:
        w.setparams((1,2,48000,0,"NONE","not compressed"));w.writeframes(bytes(480000*2))
    store.register("AUD","audio",str(audio),"audio")
    data=episode();data.update({"id":"auto-test","aspect":"landscape","currentDraftRevision":"R"})
    data["audioTimeline"]["masterAudioAssetId"]="AUD"
    data["visualVariants"][0].update({"aspect":"landscape","mode":"twoShot","masterImage":{"artifactId":"IMG"}})
    for region in data["visualVariants"][0]["personRegions"].values():region.update({"y":0,"height":1})
    prior_hash=store._hash_file(str(image)); clips=[]
    for index,(start,end) in enumerate([(0,5),(5,10)]):
        key=round(start*48000)*100000000+round(end*48000)
        path=native/f"shot_{key}.mp4";video(path,"black",5)
        path.with_suffix(".complete.json").write_text(json.dumps({"referenceHash":prior_hash,"fileHash":store._hash_file(str(path))}))
        prior_hash=store._hash_file(str(path));clips.append(path)
    source_dir=tmp_path/"source";source_dir.mkdir()
    source=source_dir/"final.mp4";shutil.copyfile(clips[0],source)
    (source_dir/"motion_manifest.json").write_text(json.dumps({"motionDirectory":str(native)}))
    store.register("SOURCE","video",str(source),"source",{"projectSnapshot":data,"projectId":data["id"]})
    provider=ComfyUIVideoProvider(base_url="http://unused",input_dir=tmp_path/"input",artifact_store=store)
    calls=[]
    async def prepare(client):pass
    async def reference(client, project_data, visual, segment, previous, picture):
        calls.append(segment["listener"])
        path=tmp_path/f"reference-{segment['start']}.mp4";video(path,"white",5.32)
        return store.register(f"REF-{segment['start']}","video",str(path),str(segment["start"]),
            {"rangeStartSec":segment["start"],"rangeEndSec":segment["end"],"listenerSpeaker":segment["listener"],"blendBand":segment["blendBand"]})
    provider._prepare_engine=prepare;provider._automatic_reference=reference
    req={"projectSnapshot":data,"projectId":data["id"],"revisionId":"R","visualVariantId":"V", "resolution":"854x480",
         "fps":25,"cameraMode":"twoShot","generationProfile":"dialogue","listeningPlan":listening_plan(data,"V"),
         "reuseMotionArtifactId":"SOURCE","listeningSeed":1}
    first=asyncio.run(provider.render(req))
    assert calls == ["B","A"]
    assert first["meta"]["generationStats"]["composited"] == 2
    assert len(first["meta"]["automaticReferenceArtifactIds"]) == 2
    second=asyncio.run(provider.render(req))
    assert calls == ["B","A"]  # No new reference generation on identical inputs.
    assert second["meta"]["generationStats"]["cached"] == 2
    assert not second["meta"]["generationStats"]["engineUsed"]
    assert second["meta"]["automaticReferenceArtifactIds"] == first["meta"]["automaticReferenceArtifactIds"]
