"""渲染业务编排（06 §3.2 render_svc）：输出版本登记（01 §11.4）。

mock 阶段：输出版本号占位（OUT-Rn-vN），无真实成片文件；关口 B 后替换为真实验证与下载。
"""

from __future__ import annotations

from typing import Any

from ..domain.project import Project
from ..storage.project_store import ProjectStore


def apply_output_version(store: ProjectStore, project: Project, revision_id: str,
                         result: dict[str, Any] | None = None) -> str:
    n = 1
    if project.output_version:
        try:
            n = int(project.output_version.rsplit("-v", 1)[-1]) + 1
        except ValueError:
            n = 1
    output = f"OUT-{revision_id}-v{n}"
    store.apply(project.id, {
        "output_version": output,
        "selected_output_version": output,
    }, expected_revision=project.revision)
    return output
