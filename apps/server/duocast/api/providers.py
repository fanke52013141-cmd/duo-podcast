"""能力门控接口（01 §12.1）：GET /api/providers/capabilities。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..adapters.base import CapabilityRegistry

router = APIRouter(prefix="/api/providers", tags=["providers"])


def _registry(request: Request) -> CapabilityRegistry:
    return request.app.state.capabilities


def _canonical_provider_id(provider_id: str) -> str:
    """前端历史 ID（textApi / imageApi）映射为后端能力键（text / image）。"""
    aliases = {"textApi": "text", "imageApi": "image"}
    canonical = aliases.get(provider_id, provider_id)
    if canonical not in ("text", "image", "tts", "video"):
        raise KeyError(f"unknown provider: {provider_id}")
    return canonical


_PROVIDER_TO_CAPS = {
    "text": lambda r: r.text, "image": lambda r: r.image,
    "tts": lambda r: r.tts, "video": lambda r: r.video,
}


@router.get("/capabilities")
def capabilities(request: Request) -> dict:
    return _registry(request).snapshot()


@router.post("/{provider_id}/test")
async def test_provider(provider_id: str, request: Request) -> dict:
    registry: CapabilityRegistry = _registry(request)
    try:
        canonical = _canonical_provider_id(provider_id)
    except KeyError as exc:
        return {"ok": False, "error": str(exc)}
    caps = _PROVIDER_TO_CAPS[canonical](registry)
    return {"ok": True, "provider": canonical, "capabilities": caps,
            "simulated": bool(caps.get("simulated")),
            "message": "模拟适配器自检通过，尚未测试真实服务" if caps.get("simulated") else "提供方已装配"}
