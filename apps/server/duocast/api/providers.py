"""能力门控接口（01 §12.1）：GET /api/providers/capabilities。"""

from __future__ import annotations

from fastapi import APIRouter, Request
import httpx
from datetime import datetime, timezone

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
    checked_at = datetime.now(timezone.utc).isoformat()
    configured = getattr(request.app.state, "public_config", {}).get(canonical + "_provider")
    if caps.get("simulated") and configured and configured != "mock":
        return {"ok": False, "provider": canonical, "simulated": True, "checkedAt": checked_at,
                "message": "已请求真实提供方，但当前缺少有效凭据而回落演示；请设置凭据环境变量并重启，不能用于真实生成"}
    if not caps.get("simulated"):
        cfg = request.app.state.runtime_settings
        public = request.app.state.public_config
        try:
            if canonical in {"text", "image"}:
                if not cfg.toapis_key:
                    raise ValueError("凭据环境变量未设置，请配置后重启服务")
                base = public[f"{canonical}_base_url"].rstrip("/")
                async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                    response = await client.get(base + "/v1/models", headers={"Authorization": "Bearer " + cfg.toapis_key})
                    response.raise_for_status()
                    models = response.json().get("data", [])
                    if canonical == "text" and cfg.text_model not in {m.get("id") for m in models}:
                        raise ValueError("凭据可连接，但所选模型不在模型清单中")
                message = "凭据与模型清单检测通过；未发起付费生成，生成能力仍需样片验证"
            else:
                url = cfg.comfyui_url if canonical == "tts" else cfg.video_url
                async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                    response = await client.get(url.rstrip("/") + "/object_info")
                    response.raise_for_status()
                    nodes = response.json()
                    if canonical == "tts" and "BSAI_IndexTTS2.5Synthesis" not in nodes:
                        raise ValueError("引擎在线，但缺 IndexTTS 合成节点")
                    if canonical == "video" and "WanVideoSampler" not in nodes:
                        raise ValueError("引擎在线，但缺 WanVideoSampler 节点")
                message = "引擎及节点检测通过；权重加载和媒体效果请通过样片验证"
            result = {"ok": True, "provider": canonical, "simulated": False, "message": message, "checkedAt": checked_at}
        except (httpx.HTTPError, ValueError, KeyError):
            # Never echo transport exceptions: headers and credential-bearing URLs may be present.
            result = {"ok": False, "provider": canonical, "simulated": False, "message": "检测失败：请检查服务地址、凭据环境变量、所选模型及引擎节点；配置更改后需重启", "checkedAt": checked_at}
        request.app.state.provider_checks[canonical] = result
        return result
    return {"ok": True, "provider": canonical, "capabilities": caps,
            "simulated": bool(caps.get("simulated")),
            "checkedAt": checked_at,
            "message": "模拟适配器自检通过，尚未测试真实服务" if caps.get("simulated") else "提供方已装配"}
