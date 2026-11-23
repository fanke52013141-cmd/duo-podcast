"""渲染业务编排（06 §3.2 render_svc）：输出版本登记（01 §11.4）。

真实渲染完成或用户采用候选后登记 OUT- 版本号；mock 渲染不改写正式输出指针。
"""

from __future__ import annotations

import re
from typing import Any

from ..domain.project import Project
from ..storage.project_store import ProjectStore


def apply_output_version(store: ProjectStore, project: Project, revision_id: str,
                         result: dict[str, Any] | None = None) -> str:
    # Allocate from every known version, not only the currently selected one.
    # Selecting an older output must never make the next render reuse its id.
    pattern = re.compile(rf"^OUT-{re.escape(revision_id)}-v(\d+)$")
    versions = [*project.output_history]
    if project.output_manifest:
        versions.append(project.output_manifest)
    if project.output_version:
        versions.append({"version": project.output_version})
    n = max((int(match.group(1)) for version in versions
             if isinstance(version, dict) and (match := pattern.match(str(version.get("version", ""))))),
            default=0) + 1
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
