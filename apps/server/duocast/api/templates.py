"""模板库接口（01 §1.5）：GET/POST /api/templates、DELETE /api/templates/{id}。

模板只带可复用配置（画幅 / A·B 声线引用），不带内容；创建工程可携带
templateId，由服务端在创建时套用配置（原子，避免前端二次 PATCH）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..storage.templates import TemplateStore

router = APIRouter(prefix="/api/templates", tags=["templates"])


def _store(request: Request) -> TemplateStore:
    return request.app.state.template_store


@router.get("")
async def list_templates(request: Request) -> list[dict]:
    return _store(request).list()


@router.post("")
async def create_template(payload: dict, request: Request) -> dict:
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(422, "模板名称不能为空")
    config = payload.get("config") or {}
    project_id = payload.get("projectId")
    if project_id:
        # 从工程保存模板：配置由服务端从工程提取（画幅 + A/B 声线引用），前端只给名称与简介。
        project = request.app.state.project_store.load(str(project_id))
        if project is None:
            raise HTTPException(404, "project not found")
        config = {
            "aspect": project.aspect,
            "voiceBindings": [{"characterId": b.character_id, "providerProfileId": b.provider_profile_id,
                               "modelId": b.model_id} for b in project.voice_bindings],
        }
    return _store(request).create(name=name, desc=payload.get("desc", ""),
                                  config=config, tags=payload.get("tags"))


@router.delete("/{template_id}")
async def delete_template(template_id: str, request: Request) -> dict:
    try:
        removed = _store(request).delete(template_id)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    if removed is None:
        raise HTTPException(404, "template not found")
    return {"deleted": template_id}
