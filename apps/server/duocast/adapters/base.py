"""提供方适配契约（06 §8.1）：Protocol 定义 + CapabilityRegistry。

adapters 不 import services / domain 业务概念；只做请求 / 错误 / 能力翻译。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TextProvider(Protocol):
    capabilities: dict[str, Any]

    async def generate(self, req: dict[str, Any]) -> dict[str, Any]: ...

    async def rewrite(self, req: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class ImageProvider(Protocol):
    capabilities: dict[str, Any]

    async def generate(self, req: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class TTSProvider(Protocol):
    capabilities: dict[str, Any]

    async def synthesize(self, req: dict[str, Any]) -> dict[str, Any]: ...

    async def audition(self, req: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class VideoProvider(Protocol):
    capabilities: dict[str, Any]

    async def submit(self, req: dict[str, Any]) -> str: ...


@dataclass
class CapabilityRegistry:
    """能力与就绪状态的唯一数据源（01 §1.6.4：UI 能力门控）。区分连通性与真实能力。"""

    text: dict[str, Any] = field(default_factory=dict)
    image: dict[str, Any] = field(default_factory=dict)
    tts: dict[str, Any] = field(default_factory=dict)
    video: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        def status(caps):
            simulated = bool(caps.get("simulated", False))
            return {"ready": bool(caps), "productionReady": bool(caps) and not simulated,
                    "mode": "mock" if simulated else "live", "capabilities": caps}
        return {
            "textApi": status(self.text),
            "imageApi": status(self.image),
            "tts": status(self.tts),
            "video": status(self.video),
        }
