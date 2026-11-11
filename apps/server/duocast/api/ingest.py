"""批 2（42 号方案）：统一对外 Ingest API。

外部工具（脚本工具 / 外部 TTS / 绘图 / 剪辑软件）把产物推进来的唯一入口：
- 统一信封：schemaVersion / kind / clientToken / expectedRevision / provenance / payload
- 统一语义：产物一律成为候选，等人显式采用（响应带 status=pending-adoption 与 adoptEndpoint）
- 门禁：DUOCAST_API_TOKEN 为空时仅本机默认放行；设置后必须 Bearer 匹配
- 契约自描述：GET /api/ingest/schema?kind=... 返回 JSON Schema

实现上复用各阶段导入端点的核心逻辑，不另造平行通道。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

SUPPORTED_KINDS = ("script", "audio", "image", "video")
SCHEMA_VERSION = "1.0"


async def require_ingest_token(request: Request) -> None:
    token = getattr(request.app.state.runtime_settings, "api_token", "") or ""
    if not token:
        return  # 本地单用户默认：服务只绑 127.0.0.1，不额外设卡
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {token}":
        raise HTTPException(401, "Ingest 需要有效的 Bearer token（DUOCAST_API_TOKEN）")


def _validate_envelope(meta: dict, kind: str) -> dict:
    if not isinstance(meta, dict):
        raise HTTPException(422, "UPLOAD_SCHEMA: 信封必须是 JSON 对象")
    version = str(meta.get("schemaVersion") or SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise HTTPException(422, f"UPLOAD_SCHEMA: 不支持的 schemaVersion {version}（当前 {SCHEMA_VERSION}）")
    declared = meta.get("kind")
    if declared is not None and declared != kind:
        raise HTTPException(422, f"UPLOAD_SCHEMA: 信封 kind={declared} 与端点 {kind} 不符")
    return meta


def _project_id(request: Request, envelope: dict) -> str:
    pid = request.query_params.get("projectId") or envelope.get("projectId")
    if not isinstance(pid, str) or not pid.strip():
        raise HTTPException(422, "UPLOAD_SCHEMA: 缺少 projectId（查询参数或信封字段）")
    return pid


async def _multipart_envelope(request: Request, kind: str) -> dict:
    form = await request.form()
    raw = form.get("meta")
    if not raw:
        return {}
    try:
        meta = json.loads(str(raw))
    except ValueError:
        raise HTTPException(422, "UPLOAD_SCHEMA: meta 不是合法 JSON")
    return _validate_envelope(meta, kind)


@router.post("/script")
async def ingest_script(request: Request, projectId: str | None = Query(None)):
    await require_ingest_token(request)
    from .script import import_script
    try:
        envelope = await request.json()
    except ValueError:
        raise HTTPException(422, "UPLOAD_SCHEMA: 请求体必须是 JSON")
    _validate_envelope(envelope, "script")
    pid = projectId or envelope.get("projectId")
    if not isinstance(pid, str) or not pid.strip():
        raise HTTPException(422, "UPLOAD_SCHEMA: 缺少 projectId")
    return await import_script(pid, envelope, request)


@router.post("/audio")
async def ingest_audio(request: Request, projectId: str | None = Query(None), unitId: str | None = Query(None)):
    await require_ingest_token(request)
    from .voice import import_unit_audio
    envelope = await _multipart_envelope(request, "audio")
    pid = _project_id(request, envelope)
    uid = unitId or (envelope.get("payload") or {}).get("unitId")
    if not isinstance(uid, str) or not uid.strip():
        raise HTTPException(422, "UPLOAD_SCHEMA: 缺少 unitId（查询参数或 payload.unitId）")
    return await import_unit_audio(pid, uid, request)


@router.post("/image")
async def ingest_image(request: Request, projectId: str | None = Query(None)):
    await require_ingest_token(request)
    from .assets import import_variant
    envelope = await _multipart_envelope(request, "image")
    pid = _project_id(request, envelope)
    return await import_variant(pid, request)


@router.post("/video")
async def ingest_video(request: Request, projectId: str | None = Query(None)):
    await require_ingest_token(request)
    from .render import import_output
    envelope = await _multipart_envelope(request, "video")
    pid = _project_id(request, envelope)
    return await import_output(pid, request)


_ENVELOPE_SCHEMA = {
    "type": "object",
    "required": ["schemaVersion", "kind", "projectId"],
    "properties": {
        "schemaVersion": {"const": SCHEMA_VERSION},
        "kind": {"enum": list(SUPPORTED_KINDS)},
        "projectId": {"type": "string"},
        "clientToken": {"type": "string", "description": "幂等键：重复提交返回同一结果"},
        "expectedRevision": {"type": "integer", "description": "乐观锁：与工程当前 revision 不符返回 409"},
        "provenance": {
            "type": "object",
            "properties": {
                "source": {"const": "external-upload"},
                "tool": {"type": "string"},
                "note": {"type": "string"},
            },
        },
        "payload": {"type": "object"},
    },
}

_PAYLOAD_SCHEMAS = {
    "script": {
        "type": "object", "required": ["turns"],
        "properties": {
            "label": {"type": "string", "maxLength": 40},
            "parentRevisionId": {"type": "string"},
            "contentBrief": {"type": "object"},
            "turns": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object", "required": ["speaker", "lines"],
                    "properties": {
                        "id": {"type": "string"},
                        "speaker": {"enum": ["A", "B"]},
                        "intent": {"enum": ["explain", "probe", "example", "challenge", "summarize", "other"]},
                        "tone": {"type": "string"},
                        "speedRatio": {"type": "number", "minimum": 0.5, "maximum": 1.5},
                        "lines": {
                            "type": "array", "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "displayText": {"type": "string"},
                                    "spokenText": {"type": "string"},
                                },
                            },
                        },
                        "sourceAnchors": {"type": "array", "items": {"type": "object", "properties": {"ref": {"type": "string"}, "quote": {"type": "string"}}}},
                    },
                },
            },
        },
    },
    "audio": {
        "type": "object", "required": ["unitId"],
        "description": "multipart：file=音频（≤50MB，自动转 48kHz PCM16 单声道）；meta=信封。产物注入为该单元候选，走 /voice/units/{id}/adopt 采纳。",
        "properties": {"unitId": {"type": "string"}},
    },
    "image": {
        "type": "object",
        "description": "multipart：file=PNG/JPEG（≤10MB）；meta=信封。产物成为视觉变体（provider=upload）。",
        "properties": {
            "mode": {"enum": ["twoShot", "overShoulder"]},
            "aspect": {"enum": ["landscape", "portrait"]},
            "personRegions": {
                "type": "object", "required": ["A", "B"],
                "additionalProperties": {
                    "type": "object", "required": ["x", "y", "width", "height"],
                    "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "width": {"type": "number"}, "height": {"type": "number"}},
                },
            },
            "subjectSpeaker": {"enum": ["A", "B"]},
            "foregroundSpeaker": {"enum": ["A", "B"]},
            "cameraGroupId": {"type": "string"},
            "cameraAssetId": {"type": "string"},
        },
    },
    "video": {
        "type": "object",
        "description": "multipart：file=视频（≤2GB）；meta=信封。时长须与权威时间轨一致（±0.2s）；产物成为候选成片，走 /render/adopt 采纳。",
        "properties": {
            "audioMode": {"enum": ["keep", "master"], "default": "keep",
                          "description": "master=仅取画面，音轨重混本工程母轨"},
        },
    },
}


@router.get("/schema")
async def ingest_schema(kind: str | None = Query(None)):
    if kind is None:
        return {"schemaVersion": SCHEMA_VERSION, "kinds": list(SUPPORTED_KINDS),
                "envelope": _ENVELOPE_SCHEMA,
                "semantics": "所有上传产物一律成为候选（pending-adoption），由人显式采用；绝不静默覆盖。"}
    if kind not in SUPPORTED_KINDS:
        raise HTTPException(422, f"未知 kind：{kind}")
    return {"schemaVersion": SCHEMA_VERSION, "kind": kind,
            "envelope": _ENVELOPE_SCHEMA, "payload": _PAYLOAD_SCHEMAS[kind]}