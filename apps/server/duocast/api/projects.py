"""工程接口（01 §12.1）：GET/PATCH /api/projects/{id}，携带 expectedRevision 冲突保护。

阶段七态由 stage_svc 权威推导（前端不自算），在响应中覆盖 stageProgress 派生值（不落盘）。
PATCH 白名单与确认绑定校验为 11 报告 P1-4/P1-6/P2-5 的落点。
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from ..domain.project import Project
from ..services.stage_svc import derive_stage_states
from ..services.render_inputs import dependency_hash, validate_render_options, visual_dependency_hash
from .voice import _merged_pronunciation
from ..storage.project_store import InvalidProjectInput, ProjectAlreadyExists, ProjectRevisionConflict, ProjectStore

router = APIRouter(prefix="/api/projects", tags=["projects"])

# 客户端可修改的字段（11 报告 P2-5：拒绝注入 revision/id/audioTimeline 等内部字段）
ALLOWED_PATCH_FIELDS = {"title", "aspect", "scriptRevisions", "currentDraftRevision",
                        "approvals", "voiceBindings", "pronunciationDict", "selectedVisualVariantId", "cameraMode", "performanceMode", "shotCameraOverrides"}
# 三个默认确认点必须绑定当前草稿（11 报告 P1-6；stage_svc 的推导口径与此一致）
CONFIRM_KINDS = {"script", "voice", "sample"}
# 工程 id 白名单：字母/数字/下划线/连字符/空格/中文，不含路径分隔符与点号（P1-4 路径穿越）
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff][A-Za-z0-9 _\-\u4e00-\u9fff]{0,62}$")


def _init_store(request: Request) -> ProjectStore:
    return request.app.state.project_store


def _inject_states(request: Request, proj: Project) -> dict:
    jobs = request.app.state.job_manager.list(proj.id) if proj.id else []
    states = derive_stage_states(proj, jobs, _merged_pronunciation(request, proj))
    data = proj.model_dump(mode="json", by_alias=True)
    data["stageProgress"] = {k: v.value for k, v in states.items()}
    return data


@router.get("")
async def list_projects(request: Request) -> list[dict]:
    store: ProjectStore = _init_store(request)
    ids = store.list_ids()
    return [_inject_states(request, store.load(i)) for i in ids if store.load(i)]


@router.post("")
async def create_project(request: Request, payload: dict) -> dict:
    store: ProjectStore = _init_store(request)
    title = payload.get("title", "未命名节目")
    generation_mode = payload.get("generationMode", "standard")
    if generation_mode not in ("standard", "fast"):
        raise HTTPException(422, "生成模式必须为 standard 或 fast")
    fast_visual = None
    if generation_mode == "fast":
        from ..adapters.musetalk_video import template_variant
        try:
            fast_visual = template_variant(request.app.state.artifacts)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    template_id = payload.get("templateId")
    if isinstance(template_id, str) and template_id:
        tpl = request.app.state.template_store.get(template_id)
        if tpl is None:
            raise HTTPException(422, f"模板不存在: {template_id}")
        if generation_mode == "fast" and (tpl.get("config") or {}).get("aspect", "landscape") != "landscape":
            raise HTTPException(422, "快速模式暂只支持横屏双人演播室模板")
    # ID 白名单（P1-4 路径穿越）：显式 ID 先过正则，非法直接 422。
    project_id = payload.get("id")
    if project_id is not None:
        if not isinstance(project_id, str) or not PROJECT_ID_RE.match(project_id):
            raise HTTPException(
                422,
                f"非法工程 id: {project_id!r}（允许字母/数字/空格/连字符/下划线/中文，不含路径分隔符与点号）",
            )
    try:
        if project_id is not None:
            proj = store.create(project_id, title)
        else:
            # 未落盘工程、空目录及链接占位均占用编号，不能覆盖或复用。
            occupied = set(store.list_ids()) | {d.name for d in store.root.iterdir()}
            number = max(
                (int(i[2:]) for i in occupied if re.fullmatch(r"EP[0-9]+", i)),
                default=0,
            ) + 1
            while True:
                try:
                    proj = store.create(f"EP{number:03d}", title)
                    break
                except ProjectAlreadyExists:
                    number += 1
    except ProjectAlreadyExists as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors(
            include_url=False, include_context=False, include_input=False,
        )) from exc
    # 模板套用（01 §1.5：模板只带可复用配置，不带内容）：创建即套用，免前端二次 PATCH。
    template_id = payload.get("templateId")
    if isinstance(template_id, str) and template_id:
        tpl = request.app.state.template_store.get(template_id)
        if tpl is None:
            raise HTTPException(422, f"模板不存在: {template_id}")
        config = tpl.get("config") or {}
        patch: dict = {}
        if config.get("aspect") in ("landscape", "portrait"):
            patch["aspect"] = config["aspect"]
        bindings = config.get("voiceBindings")
        if isinstance(bindings, list) and bindings:
            patch["voiceBindings"] = bindings
        if patch:
            proj = store.apply(proj.id, patch, expected_revision=proj.revision)
    if fast_visual:
        proj = store.apply(proj.id, {"generation_mode": "fast", "visual_variants": [fast_visual],
            "selected_visual_variant_id": fast_visual["id"], "aspect": "landscape", "camera_mode": "twoShot"}, expected_revision=proj.revision)
    return _inject_states(request, proj)


@router.get("/{project_id}")
async def get_project(project_id: str, request: Request) -> dict:
    store: ProjectStore = _init_store(request)
    proj = store.load(project_id)
    if proj is None:
        raise HTTPException(404, "project not found")
    return _inject_states(request, proj)


@router.delete("/{project_id}")
async def delete_project(project_id: str, request: Request) -> dict:
    """软删除：工程目录移入 storage/trash/（保留可人工恢复），不从磁盘抹除内容。

    同时把该工程的全部任务记录搬进同一回收站目录（第五轮验证：否则工程 ID 复用后，
    新工程会看到被删工程的任务历史，阶段①出现「幽灵任务已生成」的假状态）。
    """
    store: ProjectStore = _init_store(request)
    try:
        # 先移工程、再移任务：用 delete 返回的实际回收站目录做任务落点，
        # 两者必须同处一个目录，恢复时才能整体搬回（否则只恢复半套数据）。
        trash_dir = store.delete(project_id)
        moved_jobs = request.app.state.job_manager.purge_project(project_id, trash_dir)
    except KeyError as exc:
        raise HTTPException(404, f"工程不存在: {project_id}") from exc
    except InvalidProjectInput as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"删除失败（目录被占用或权限不足）: {exc}") from exc
    return {"deleted": project_id, "jobsMoved": moved_jobs}


def _validate_confirm_bindings(store: ProjectStore, project_id: str, approvals: list) -> None:
    """新增的默认确认点必须绑定当前草稿；历史确认原样透传（只追加不改写，01 §13.2）。"""
    current = store.load(project_id)
    if current is None:
        return
    existing = {
        json.dumps(a.model_dump(mode="json", by_alias=True), sort_keys=True, ensure_ascii=False)
        for a in current.approvals
    }
    for entry in approvals:
        if not isinstance(entry, dict) or entry.get("kind") not in CONFIRM_KINDS:
            continue
        key = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        if key in existing:
            continue
        if entry.get("inputRevisionId") != current.current_draft_revision:
            raise HTTPException(
                422,
                f"确认记录必须绑定当前草稿 {current.current_draft_revision}，"
                f"收到的是 {entry.get('inputRevisionId')!r}（kind={entry.get('kind')}，可能已过期）",
            )


@router.patch("/{project_id}")
async def patch_project(project_id: str, payload: dict, request: Request) -> dict:
    store: ProjectStore = _init_store(request)
    expected = payload.get("expectedRevision")
    patch = {k: v for k, v in payload.items() if k != "expectedRevision"}
    unknown = set(patch) - ALLOWED_PATCH_FIELDS
    if unknown:
        raise HTTPException(422, f"字段不允许修改: {sorted(unknown)}")
    if "approvals" in patch and isinstance(patch["approvals"], list):
        _validate_confirm_bindings(store, project_id, patch["approvals"])
    saved_project = store.load(project_id)
    # 只和「有效确认」去重：已撤销的旧记录不应阻止用户重新确认（F2）。
    saved_approvals = {json.dumps(a.model_dump(mode="json", by_alias=True), sort_keys=True, ensure_ascii=False)
                       for a in saved_project.approvals if not a.revoked_at} if saved_project else set()
    for approval in patch.get("approvals", []):
        if not isinstance(approval, dict):
            continue
        if json.dumps(approval, sort_keys=True, ensure_ascii=False) in saved_approvals:
            continue
        spec = approval.get("spec", {})
        if not isinstance(spec, dict):
            continue
        if approval.get("kind") == "sample" and spec.get("sampleJobId"):
            job = request.app.state.job_manager.get(spec["sampleJobId"])
            current = store.load(project_id)
            result = (job.result if job is not None else None) or {}
            # 演示样片无真实产物（artifactIds 为空）：只要求任务成功且依赖一致；
            # 真实样片仍必须携带与任务产物一致的 sampleArtifactId（第五轮实测 A1：
            # 演示模式下前端传空 artifactId，"None not in []" 恒真导致接受永远 409）。
            demo_sample = bool(result.get("simulated")) and not result.get("artifactIds")
            artifact_ok = demo_sample or spec.get("sampleArtifactId") in result.get("artifactIds", [])
            if (job is None or job.project_id != project_id or job.status.value != "succeeded"
                    or not artifact_ok
                    or job.input_snapshot.get("dependencyHash") != dependency_hash(current)):
                raise HTTPException(
                    409,
                    "样片无法接受：任务不存在、未成功、产物不符，或生成样片后脚本/配音/构图又改动过。"
                    "请重新生成样片后再接受；若反复出现，请回到阶段②③检查是否还有未确认的改动。",
                )
            # 样片确认只绑定「画面依赖」：之后调间隔/换配音不应让已接受的样片作废（F4）。
            spec["dependencyHash"] = visual_dependency_hash(current)
            validate_render_options(current, {}, _merged_pronunciation(request, current), real_video=False)
    try:
        updated = store.apply(project_id, patch, expected_revision=expected)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ProjectRevisionConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors(
            include_url=False, include_context=False, include_input=False,
        )) from exc
    return _inject_states(request, updated)
