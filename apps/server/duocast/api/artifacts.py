"""资产接口（01 §12.1）：GET /api/artifacts（不可变产物登记清单，06 §7.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..storage.artifacts import ArtifactStore

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


@router.get("")
def list_artifacts(request: Request) -> dict:
    store: ArtifactStore = request.app.state.artifacts
    return {"artifacts": store._manifest()["artifacts"]}  # noqa: SLF001（读清单，非写入）
