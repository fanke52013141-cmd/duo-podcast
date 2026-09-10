"""机器级运行配置（06 §2 core/config.py）。端口 / 工程根 / 队列上限 / 防抖间隔。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]  # apps/server/duocast/core/config.py -> 仓库根


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    # 8100 为默认端口：本机 8000 可能被其他本地服务占用（如 PPT_presentation_video server.py），
    # 端口可经 DUOCAST_PORT 覆盖（06 §2 core/config.py）
    port: int = 8100
    # 工程数据根（06 §2 结构中的 storage/，位于 apps/ 下，与旧落点一致）
    storage_root: Path = field(default_factory=lambda: PROJECT_ROOT / "apps" / "storage")
    # 队列上限（02 §7：三队列 GPU / API / CPU）
    queue_cap_gpu: int = 1
    queue_cap_api: int = 16
    queue_cap_cpu: int = 4
    # 编辑类 PATCH 防抖落盘间隔（毫秒，01 §12 预期 revision 冲突保护之上）
    project_debounce_ms: int = 2000
    # 事件日志保留条数（超出后最旧事件轮转，重连需走快照路径）
    event_log_cap: int = 20_000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("DUOCAST_HOST", "127.0.0.1"),
            port=int(os.environ.get("DUOCAST_PORT", "8100")),
            storage_root=Path(os.environ.get("DUOCAST_STORAGE", str(PROJECT_ROOT / "apps" / "storage"))),
        )


settings = Settings.from_env()
