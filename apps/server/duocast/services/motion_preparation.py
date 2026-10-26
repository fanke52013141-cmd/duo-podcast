"""Real, resumable, audio-independent I2V candidates; never adopts a pack clip."""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

import httpx
from pydantic import Field

from ..domain.motion_pack import MotionModel, MotionPack, Role, MotionState, content_hash
from ..storage.motion_packs import InvalidMotionPack, MotionPackConflict
from .listening_plan import reference_workflow
from .motion_quality import inspect_motion_clip

VERSION = "motion-preparation-i2v-v1"
KIND = "motion.prepare"


class PreparationRequest(MotionModel):
    expected_revision: int = Field(strict=True, ge=1)
    client_token: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    camera_id: str = Field(min_length=1, max_length=128)
    role: Role
    state: MotionState = "listening"
    seed: int = Field(default=1, strict=True, ge=0, le=2**32 - 1)
    duration_sec: float = Field(default=4, ge=2, le=6)
    prompt: str = Field(min_length=1, max_length=6000)


def build_workflow(pack, request, image_name, prefix):
    template = json.loads((Path(__file__).parents[1] / "adapters/multitalk_workflow.json").read_text(encoding="utf-8"))
    segment = {"start": 0, "end": request.duration_sec, "referenceSeed": request.seed,
               "listenerSide": "left", "reaction": "attentive", "reactionAtSec": 1,
               "amplitude": "subtle"}
    workflow = reference_workflow(template, image_name, segment, prefix)
    camera = next(c for c in pack.cameras if c.id == request.camera_id)
    character = pack.characters[request.role]
    prompt = (f"Fixed camera continuous object animation. Moving subject is role {request.role}, "
              f"{character.name or character.character_id}, type {character.object_type}. "
              f"Action state: {request.state}. {character.motion_notes}. {request.prompt}. "
              "Animate actual structural movement at the designed joints, preserve rigid shape and planted base. "
              "Keep subject's both eyes, eyebrows, nose and mouth visible in every frame, small in-plane roll only; "
              "no turn-away, downward pitch, facial occlusion, camera motion, cuts or morphing. "
              "Preserve exact reference identities, mechanical construction and background. "
              "Lips stay relaxed and closed; audio and lip sync are added separately. ")
    if character.object_type != "human":
        prompt += "No invented human body, limbs or clothing. "
    if camera.mode == "overShoulder":
        prompt += "Preserve exact over-shoulder reverse angle; foreground object's face remains hidden. "
    else:
        prompt += "Keep both subjects in frame; the other subject quietly listens. "
    workflow["135"]["inputs"]["positive_prompt"] = prompt
    if any(n["class_type"] in {"MultiTalkModelLoader", "MultiTalkWav2VecEmbeds"} for n in workflow.values()):
        raise RuntimeError("Audio-independent I2V workflow is unavailable")
    return workflow


class MotionPreparationService:
    def __init__(self, store, artifacts, manager, provider=None, *, transport=None,
                 poll_interval=3.0, timeout_sec=3600):
        self.store, self.artifacts, self.manager, self.provider = store, artifacts, manager, provider
        self.transport = transport
        self.poll_interval, self.timeout_sec = poll_interval, timeout_sec
        self._locks = {}

    def submit(self, pack_id: str, payload: PreparationRequest | dict):
        request = PreparationRequest.model_validate(payload.model_dump() if isinstance(payload, PreparationRequest) else payload)
        # Replay the existing frozen job even if its original pack revision has advanced.
        for job in self.manager.list(owner_type="motionPack", owner_id=pack_id):
            if job.kind == KIND and job.client_token == request.client_token:
                if job.input_snapshot.get("request") != request.model_dump(mode="json", by_alias=True):
                    raise MotionPackConflict("IDEMPOTENCY_TOKEN_INPUT_MISMATCH")
                return job
        if self.provider is None or self.provider.capabilities.get("simulated", True):
            raise RuntimeError("真实 I2V 引擎未配置；动作准备不支持模拟完成")
        pack = self.store.load(pack_id)
        if pack is None:
            raise KeyError(pack_id)
        if pack.revision != request.expected_revision:
            raise MotionPackConflict("MOTION_PACK_REVISION_CONFLICT")
        camera = next((c for c in pack.cameras if c.id == request.camera_id), None)
        if camera is None or camera.master_image is None:
            raise InvalidMotionPack("机位或机位参考图缺失")
        if camera.mode == "overShoulder" and camera.subject_speaker != request.role:
            raise InvalidMotionPack("过肩动作角色必须是该机位可见主体")
        for role in ("A", "B"):
            if role not in pack.characters or pack.characters[role].reference_image is None:
                raise InvalidMotionPack("A/B角色参考图缺失")
        self._verify_inputs(pack, camera)
        image = self.artifacts.by_id(camera.master_image.artifact_id)
        key = content_hash({"pack": pack.model_dump(mode="json"), "request": request.model_dump(mode="json"), "version": VERSION})
        image_name = "motion_" + key + Path(image["path"]).suffix.lower()
        workflow = build_workflow(pack, request, image_name, "motion_candidates/" + key)
        snapshot = {"request": request.model_dump(mode="json", by_alias=True),
                    "packSnapshot": pack.model_dump(mode="json", by_alias=True),
                    "workflow": workflow, "imageName": image_name, "dependencyHash": key,
                    "resumableMotionPreparation": True, "workflowVersion": VERSION}
        return self.manager.submit(KIND, "", "gpu", request.client_token, snapshot,
                                   owner_type="motionPack", owner_id=pack_id)

    def _verify_inputs(self, pack, camera):
        self.store._verify_ref(camera.master_image, "image")
        for role in ("A", "B"):
            self.store._verify_ref(pack.characters[role].reference_image, "image")

    def _remember(self, job, record):
        job.completed_calls["motionPreparation"] = record
        self.manager._persist(job)

    def resume(self, pack_id: str, job_id: str):
        job = self.manager.get(job_id)
        if job is None or job.owner_type != "motionPack" or job.owner_id != pack_id or job.kind != KIND:
            raise KeyError(job_id)
        return self.manager.resume_external(job_id)

    async def run(self, job):
        async with self._locks.setdefault(job.id, asyncio.Lock()):
            try:
                return await self._run(job)
            finally:
                (self.artifacts.root / "motion_candidates" / job.id / "download.partial.mp4").unlink(missing_ok=True)

    async def _run(self, job):
        if job.kind != KIND or job.owner_type != "motionPack":
            raise ValueError("Not a motion preparation job")
        if self.provider is None or self.provider.capabilities.get("simulated", True):
            raise RuntimeError("真实 I2V 引擎未配置")
        snapshot = job.input_snapshot
        pack = MotionPack.model_validate(snapshot["packSnapshot"])
        if pack.id != job.owner_id:
            raise InvalidMotionPack("Motion job owner mismatch")
        request = PreparationRequest.model_validate(snapshot["request"])
        camera = next(c for c in pack.cameras if c.id == request.camera_id)
        self._verify_inputs(pack, camera)
        record = job.completed_calls.get("motionPreparation", {})
        if record.get("result"):
            entry = self.artifacts.by_id(record["result"]["artifactIds"][0])
            if not entry or entry["fileHash"] != self.artifacts._hash_file(entry["path"]):
                raise InvalidMotionPack("Prepared candidate changed or disappeared")
            return record["result"]
        workflow = snapshot["workflow"]
        digest = content_hash(workflow)
        if record and record.get("workflowHash") != digest:
            raise InvalidMotionPack("Persisted I2V workflow changed")
        prompt_id = record.get("promptId")
        if record.get("submissionIntent") and not prompt_id:
            raise RuntimeError("I2V_SUBMISSION_UNKNOWN: submission may have reached engine; refusing to resample")
        source = Path(self.artifacts.by_id(camera.master_image.artifact_id)["path"])
        self.provider.input_dir.mkdir(parents=True, exist_ok=True)
        image = self.provider.input_dir / snapshot["imageName"]
        shutil.copyfile(source, image)
        async with httpx.AsyncClient(base_url=self.provider.base_url, timeout=60, trust_env=False,
                                     transport=self.transport) as client:
            if not prompt_id:
                self.manager.checkpoint(job)
                response = await client.get("/object_info")
                response.raise_for_status()
                available = response.json()
                missing = sorted({n["class_type"] for n in workflow.values()} - set(available))
                if missing:
                    raise RuntimeError("I2V_WORKFLOW_UNAVAILABLE: " + ", ".join(missing))
                record = {"workflowHash": digest, "submissionIntent": True}
                self._remember(job, record)
                response = await client.post("/prompt", json={"prompt": workflow, "client_id": job.id})
                response.raise_for_status()
                submitted = response.json()
                if submitted.get("node_errors") or not isinstance(submitted.get("prompt_id"), str) or not submitted["prompt_id"]:
                    raise RuntimeError("I2V_WORKFLOW_REJECTED: " + str(submitted.get("node_errors", "missing prompt id")))
                prompt_id = submitted["prompt_id"]
                record["promptId"] = prompt_id
                job.provider_task_id = prompt_id
                self._remember(job, record)
            deadline = time.monotonic() + self.timeout_sec
            while time.monotonic() < deadline:
                self.manager.checkpoint(job)
                response = await client.get("/history/" + prompt_id)
                response.raise_for_status()
                history = response.json().get(prompt_id)
                if history:
                    if history.get("status", {}).get("status_str") != "success":
                        record["externalTerminalFailure"] = True
                        self._remember(job, record)
                        raise RuntimeError("I2V_ENGINE_FAILED: " + str(history.get("status")))
                    return await self._collect(client, job, history, record, request)
                await asyncio.sleep(self.poll_interval)
        raise TimeoutError("I2V_PENDING: original external task retained; resume without resampling")

    async def _collect(self, client, job, history, record, request):
        artifact_id = "VID-MOTION-" + job.id
        entry = self.artifacts.by_id(artifact_id)
        # Registration can precede a crash before the final job checkpoint.
        # Recover the immutable artifact rather than overwrite its path.
        if entry:
            if entry["fileHash"] != self.artifacts._hash_file(entry["path"]):
                raise InvalidMotionPack("Immutable candidate artifact differs")
            provenance = entry["paramsSnapshot"]
            result = self._result(entry, request, provenance)
            record["result"] = result
            self._remember(job, record)
            return result
        outputs = history.get("outputs", {}).get("229", {}).get("gifs", [])
        video = next((v for v in outputs if isinstance(v.get("filename"), str) and v["filename"].lower().endswith(".mp4")), None)
        if not video or any(sep in video["filename"] for sep in ("/", "\\")) or video.get("type") != "output":
            raise RuntimeError("I2V_OUTPUT_MISSING: no real video output")
        subfolder = video.get("subfolder", "")
        if not isinstance(subfolder, str) or ".." in subfolder.replace("\\", "/").split("/") or Path(subfolder).is_absolute() or ":" in subfolder:
            raise RuntimeError("Invalid engine output location")
        folder = self.artifacts.root / "motion_candidates" / job.id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "candidate.mp4"
        partial = folder / "download.partial.mp4"
        async with client.stream("GET", "/view", params={"filename": video["filename"], "subfolder": subfolder, "type": "output"}) as response:
            response.raise_for_status()
            size = 0
            with partial.open("wb") as stream:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 256 * 1024 * 1024:
                        raise RuntimeError("I2V output exceeds 256 MiB limit")
                    stream.write(chunk)
        # Actual strict decoder evidence is mandatory; structural quality is still
        # recorded as candidate evidence and is never converted to human approval.
        report = await asyncio.to_thread(inspect_motion_clip, partial, max_duration_sec=10)
        native = report.get("native", {})
        if (not native or not report.get("evidence", {}).get("completeSourceFramesDecoded")
                or any(c not in {"STATIC_SOURCE", "NO_STRUCTURAL_MOTION"} for c in report["hardFailures"])
                or native["durationSec"] < request.duration_sec):
            raise RuntimeError("I2V_OUTPUT_INVALID: real complete video decode required")
        partial.replace(target)
        provenance = {"generator": "ComfyUI/WanVideoImageToVideoEncode", "workflowVersion": VERSION,
                      "workflowHash": record["workflowHash"], "promptId": record["promptId"],
                      "packId": job.owner_id, "packVersion": job.input_snapshot["packSnapshot"]["version"],
                      "request": request.model_dump(mode="json", by_alias=True), "native": native,
                      "qualityReport": report, "requiresHumanReview": True, "adopted": False}
        entry = self.artifacts.register(artifact_id, "video", str(target), job.input_snapshot["dependencyHash"], provenance, VERSION)
        result = self._result(entry, request, provenance)
        record["result"] = result
        self._remember(job, record)
        return result

    @staticmethod
    def _result(entry, request, provenance):
        return {"artifactIds": [entry["artifactId"]], "candidate": {"media": {"artifactId": entry["artifactId"], "fileHash": entry["fileHash"]},
                  "cameraId": request.camera_id, "role": request.role, "state": request.state,
                  "native": provenance["native"], "provenance": provenance}, "adopted": False}
