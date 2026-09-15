"""project.json 唯一写入者（06 §7.1 / R5）：原子替换 + expectedRevision 冲突 + 防抖落盘。

services 不得直接写文件；所有元数据变更经 apply()。内存态即时可读，落盘按防抖合并。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..domain.project import Project

logger = logging.getLogger("duocast.storage.project_store")

CONFLICT = "PROJECT_REVISION_CONFLICT"


def _now_iso() -> str:
    """UTC ISO8601（秒精度）。仅 ProjectStore 打戳，保证时间权威单点。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProjectRevisionConflict(Exception):
    pass


class InvalidProjectInput(ValueError):
    pass


class ProjectAlreadyExists(Exception):
    pass


class ProjectStore:
    def __init__(self, root: Path, debounce_ms: int = 500) -> None:
        # resolve 固化真实路径：防符号链接别名导致同一工程被不同 ID 访问（v3.1 存储安全）。
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.debounce_ms = debounce_ms
        self._projects: dict[str, Project] = {}
        self._debounce_task: Optional[asyncio.Task] = None
        self._dirty: set[str] = set()

    # ---- 路径 ----
    def _path(self, project_id: str) -> Path:
        if (
            not isinstance(project_id, str)
            or not 1 <= len(project_id) <= 255
            or re.fullmatch(r"[A-Za-z0-9_-]+", project_id) is None
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", project_id, re.IGNORECASE)
        ):
            raise InvalidProjectInput("invalid project id")
        candidate = self.root / project_id / "project.json"
        try:
            target = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise InvalidProjectInput("invalid project path") from exc
        # 同时拒绝越界和根内链接别名，避免不同 ID 读写同一个工程。
        if not target.is_relative_to(self.root) or target != candidate:
            raise InvalidProjectInput("invalid project path")
        return target

    # ---- 读 ----
    def load(self, project_id: str) -> Optional[Project]:
        p = self._path(project_id)
        if project_id in self._projects:
            return self._projects[project_id]
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        proj = Project.model_validate(data)
        if proj.id != project_id:
            raise InvalidProjectInput("project id does not match storage directory")
        self._projects[project_id] = proj
        return proj

    def list_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        candidates = set(self._projects) | {d.name for d in self.root.iterdir()}
        ids = []
        for project_id in candidates:
            try:
                path = self._path(project_id)
            except InvalidProjectInput:
                continue
            if project_id in self._projects or path.is_file():
                ids.append(project_id)
        return sorted(ids)

    # ---- 写（唯一入口） ----
    def apply(self, project_id: str, patch: dict[str, Any], expected_revision: int) -> Project:
        current = self.load(project_id)
        if not isinstance(patch, dict):
            raise InvalidProjectInput("patch must be an object")
        if "id" in patch and patch["id"] != project_id:
            raise InvalidProjectInput("project id cannot be changed")
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidProjectInput("expectedRevision must be a non-negative integer")
        if current is None:
            raise KeyError(f"project not found: {project_id}")
        if current.revision != expected_revision:
            raise ProjectRevisionConflict(
                f"expectedRevision={expected_revision} != current={current.revision}"
            )
        updated = current.model_copy(deep=True)
        data = updated.model_dump(mode="json")
        data.update(patch)
        updated = Project.model_validate(data)
        updated.revision = expected_revision + 1
        updated.updated_at = _now_iso()
        self._projects[project_id] = updated
        self._dirty.add(project_id)
        self._schedule_flush()
        return updated

    def create(self, project_id: str, title: str = "未命名节目") -> Project:
        path = self._path(project_id)
        if path.parent.exists() or any(
            self.root / existing_id == path.parent for existing_id in self._projects
        ):
            raise ProjectAlreadyExists(f"project already exists: {project_id}")
        proj = Project(id=project_id, title=title, revision=0, updated_at=_now_iso())
        self._projects[project_id] = proj
        self._dirty.add(project_id)
        self._schedule_flush()
        return proj

    # ---- 防抖落盘 ----
    def _schedule_flush(self) -> None:
        if self._debounce_task is not None and not self._debounce_task.done():
            return  # 已有定时器在等，合并本次修改
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 同步上下文（FastAPI 线程池端点）：无事件循环可调度，即时落盘保证不丢数据
            self.flush()
            return
        self._debounce_task = loop.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(self.debounce_ms / 1000)
        self.flush()

    def flush(self) -> None:
        for project_id in list(self._dirty):
            proj = self._projects.get(project_id)
            if proj is None:
                continue
            target = self._path(project_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            # 原子替换（02 §6.3）：tmp → fsync → os.replace
            payload = json.dumps(proj.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
            # 独占创建随机临时文件，不跟随预先放置的 project.json.tmp 链接。
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=target.parent,
                    prefix=".project-", suffix=".json.tmp", delete=False,
                ) as fh:
                    tmp = Path(fh.name)
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            self._dirty.discard(project_id)
            logger.info("project persisted", extra={"ctx": {"id": project_id, "revision": proj.revision}})
