"""结构化日志（02 §13.3 日志规范的最小落位：时间 / 级别 / 模块 / 消息 / 上下文）。"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "ctx") and record.ctx:
            payload["ctx"] = record.ctx
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _json_default(obj):
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True)
    return repr(obj)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("duocast")
    root.setLevel(level)
    root.handlers = [handler]


def log(module: str, message: str, **ctx) -> None:
    record = logging.getLogger(f"duocast.{module}")
    record.info(message, extra={"ctx": ctx or None})
