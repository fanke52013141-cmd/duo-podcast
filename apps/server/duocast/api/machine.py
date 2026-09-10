"""机器设置接口（06 §7.3）：GET/PATCH /api/machine。

providerProfiles 只存凭据引用（credentialRef），不落密钥明文；VRAM 分配供
queueCaps 之外的人工预算门控（01 §1.6.4 UI 门控）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..core.config import settings
from ..storage.machine import MachineSettings

router = APIRouter(prefix="/api/machine", tags=["machine"])


def _machine(request: Request) -> MachineSettings:
    return request.app.state.machine


@router.get("")
def get_machine(request: Request) -> dict:
    m = _machine(request)
    data = m._data()  # noqa: SLF001（读文件快照，非写入）
    return {
        "providerProfiles": data.get("providerProfiles", {}),
        "vramAllocation": data.get("vramAllocation", {}),
        "queueCaps": {
            "gpu": settings.queue_cap_gpu,
            "api": settings.queue_cap_api,
            "cpu": settings.queue_cap_cpu,
        },
        "capabilities": request.app.state.capabilities.snapshot(),
    }


@router.patch("")
def patch_machine(payload: dict, request: Request) -> dict:
    m = _machine(request)
    data = m._data()
    if isinstance(payload.get("providerProfiles"), dict):
        data["providerProfiles"] = payload["providerProfiles"]
    if isinstance(payload.get("vramAllocation"), dict):
        data["vramAllocation"] = payload["vramAllocation"]
    m.save(data)
    return {
        "providerProfiles": data.get("providerProfiles", {}),
        "vramAllocation": data.get("vramAllocation", {}),
    }
