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
    # 编辑类 PATCH 防抖落盘间隔（毫秒，01 §12 预期 revision 冲突保护之上）。
    # 500ms（11 报告 P2-9）：窗口内 kill -9 会丢已确认给客户端的编辑，窗口越短暴露面越小。
    project_debounce_ms: int = 500
    # 事件日志保留条数（超出后最旧事件轮转，重连需走快照路径）
    event_log_cap: int = 20_000
    # 关口 A（链路实测 2026-09-19）：TTS 提供方切换 mock | comfyui
    tts_provider: str = "mock"
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_input: Path = field(
        default_factory=lambda: Path(r"D:\软件\Wan2.2-ReMix-SVI2-V3\Wan2.2-ReMix-SVI2-V3\ComfyUI\input"))
    tts_ref_a: str = r"D:\Program Files (x86)\PPT_presentation_video\data\model_voice_references\76abe9e06a9d4670b40b0d966faa652c.mp3"
    tts_ref_b: str = ""

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError:
            return default
        return value if value > 0 else default

    @classmethod
    def from_env(cls) -> "Settings":
        # 队列上限可经环境变量覆盖（12 报告 C-3：5080 单机需按实机调参，无需改代码）
        return cls(
            host=os.environ.get("DUOCAST_HOST", "127.0.0.1"),
            port=int(os.environ.get("DUOCAST_PORT", "8100")),
            storage_root=Path(os.environ.get("DUOCAST_STORAGE", str(PROJECT_ROOT / "apps" / "storage"))),
            queue_cap_gpu=cls._env_int("DUOCAST_QUEUE_GPU", 1),
            queue_cap_api=cls._env_int("DUOCAST_QUEUE_API", 16),
            queue_cap_cpu=cls._env_int("DUOCAST_QUEUE_CPU", 4),
            project_debounce_ms=cls._env_int("DUOCAST_PROJECT_DEBOUNCE_MS", 500),
            tts_provider=os.environ.get("DUOCAST_TTS_PROVIDER", "mock"),
            comfyui_url=os.environ.get("DUOCAST_COMFYUI_URL", "http://127.0.0.1:8188"),
            comfyui_input=Path(os.environ.get(
                "DUOCAST_COMFYUI_INPUT",
                r"D:\软件\Wan2.2-ReMix-SVI2-V3\Wan2.2-ReMix-SVI2-V3\ComfyUI\input")),
            tts_ref_a=os.environ.get(
                "DUOCAST_TTS_REF_A",
                r"D:\Program Files (x86)\PPT_presentation_video\data\model_voice_references\76abe9e06a9d4670b40b0d966faa652c.mp3"),
            tts_ref_b=os.environ.get("DUOCAST_TTS_REF_B", ""),
        )


settings = Settings.from_env()
