"""渲染业务编排（06 §3.2 render_svc）：输出版本登记（01 §11.4）。

注意（11 报告 P2-4）：apply_output_version 当前**无调用方**——mock 渲染不改写正式
输出指针（09 边界 #6：模拟渲染不登记不存在的媒体）。真实渲染适配器（关口 B）接线时
由 _job_runner 在验证真实成片落盘后调用本函数登记 OUT- 版本号。
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
