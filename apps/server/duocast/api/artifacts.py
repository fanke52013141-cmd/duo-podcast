"""资产接口（01 §12.1）：GET /api/artifacts（清单）、GET /api/artifacts/{id}/file（下载）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..storage.artifacts import ArtifactStore

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


@router.get("")
def list_artifacts(request: Request) -> dict:
    store: ArtifactStore = request.app.state.artifacts
    return {"artifacts": store._manifest()["artifacts"]}  # noqa: SLF001（读清单，非写入）


@router.get("/{artifact_id}/file")
def get_artifact_file(artifact_id: str, request: Request) -> FileResponse:
    store: ArtifactStore = request.app.state.artifacts
    entry = store.by_id(artifact_id)
    if entry is None:
        raise HTTPException(404, "artifact not found")
    # 路径白名单：只允许 manifest 登记过的文件，杜绝任意路径读取
    path = entry.get("path", "")
    file = Path(path)
    if not path or not file.is_file():
        raise HTTPException(404, "artifact file missing")
    return FileResponse(str(file))
