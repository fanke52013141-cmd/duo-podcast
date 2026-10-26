"""渲染业务编排（06 §3.2 render_svc）：输出版本登记（01 §11.4）。

真实渲染完成或用户采用候选后登记 OUT- 版本号；mock 渲染不改写正式输出指针。
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
    manifest = {"version": output, "artifactIds": (result or {}).get("artifactIds", []), "meta": (result or {}).get("meta", {})}
    history = list(project.output_history)
    if project.output_manifest and not any(v["version"] == project.output_manifest["version"] for v in history):
        history.append(project.output_manifest)
    history.append(manifest)
    store.apply(project.id, {
        "output_version": output,
        "selected_output_version": output,
        "output_manifest": manifest,
        "output_history": history,
    }, expected_revision=project.revision)
    return output
