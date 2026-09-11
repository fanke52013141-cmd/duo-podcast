"""能力门控接口（01 §12.1）：GET /api/providers/capabilities。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..adapters.base import CapabilityRegistry

router = APIRouter(prefix="/api/providers", tags=["providers"])


def _registry(request: Request) -> CapabilityRegistry:
    return request.app.state.capabilities


@router.get("/capabilities")
def capabilities(request: Request) -> dict:
    return _registry(request).snapshot()


@router.post("/{provider_id}/test")
async def test_provider(provider_id: str, request: Request) -> dict:
    registry: CapabilityRegistry = _registry(request)
    provider_map = {"text": registry.text, "image": registry.image, "tts": registry.tts, "video": registry.video}
    caps = provider_map.get(provider_id)
    if caps is None:
        return {"ok": False, "error": "unknown provider"}
    return {"ok": True, "provider": provider_id, "capabilities": caps,
            "simulated": bool(caps.get("simulated")),
            "message": "模拟适配器自检通过，尚未测试真实服务" if caps.get("simulated") else "提供方已装配"}
