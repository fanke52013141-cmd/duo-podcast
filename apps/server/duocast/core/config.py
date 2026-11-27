"""机器级运行配置（06 §2 core/config.py）。端口 / 工程根 / 队列上限 / 防抖间隔。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]  # apps/server/duocast/core/config.py -> 仓库根


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    # TrustedHost 额外放行的主机名（逗号分隔）。通配监听地址必须显式配置实际访问主机名/IP。
    trusted_hosts: str = ""
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
    # TTS 提供方：mock | comfyui | minimax | volcengine。
    # 云语音密钥只从环境变量读取；machine.json 仅保存非敏感模型/音色配置。
    tts_provider: str = "mock"
    minimax_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com"
    minimax_model: str = "speech-2.8-hd"
    minimax_voice_a: str = "male-qn-qingse"
    minimax_voice_b: str = ""
    volcengine_key: str = ""
    volcengine_base_url: str = "https://openspeech.bytedance.com"
    volcengine_model: str = "seed-audio-1.0"
    volcengine_voice_a: str = ""
    volcengine_voice_b: str = ""
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_input: Path = field(
        default_factory=lambda: Path(r"D:\软件\Wan2.2-ReMix-SVI2-V3\Wan2.2-ReMix-SVI2-V3\ComfyUI\input"))
    tts_ref_a: str = r"D:\Program Files (x86)\PPT_presentation_video\data\model_voice_references\76abe9e06a9d4670b40b0d966faa652c.mp3"
    tts_ref_b: str = ""
    # 关口 A（链路实测 2026-09-19）：图片提供方切换 mock | toapis（需 DUOCAST_TOAPIS_KEY）
    image_provider: str = "mock"
    toapis_key: str = ""
    image_size: str = "4:5"
    image_quality: str = "low"
    image_resolution: str = "1k"
    video_provider: str = "mock"
    # Vidu 官方 CLI 通道；Token 和可选二进制路径仅从环境变量读取。
    vidu_key: str = ""
    vidu_base_url: str = "https://service.vidu.cn"
    vidu_model: str = "3.4"
    vidu_binary: str = ""
    text_provider: str = "mock"
    text_model: str = "deepseek-flash"
    video_url: str = "http://127.0.0.1:8191"
    video_input: Path = field(default_factory=lambda: PROJECT_ROOT / "runtime" / "video" / "input")
    # 全局 API 门禁。为空时仅允许回环监听；设置后外部 API 客户端必须携带
    # Authorization: Bearer <token>，同源浏览器 UI 由 TrustedHost/Origin 放行。
    # Ingest 始终执行更严格的端点级 Bearer 校验，不使用同源豁免。
    api_token: str = ""
    # 44 号 · 标准模式加速调优参数（默认值=现网行为，零变化）。
    # 回家在 5080 上按 44 号文档梯级 A/B；参数纳入缓存指纹，切配置不误用旧片段。
    video_attention: str = "sdpa"          # sdpa | sageattn | sageattn_3 | flash_attn_2 | flash_attn_3 | comfy
    video_blocks_to_swap: int = 38         # 0–48；38 为现网极限省显存值，5080 可试 24–28
    video_prefetch_blocks: int = 0         # 0–40；异步预取，1 通常能补回大部分交换速度损失
    video_text_cfg: float = 2.0            # 现网 listening-v4-cfg2 策略；蒸馏 LoRA 官方建议 1.0（A/B）
    video_audio_cfg: float = 1.0           # 口型引导；官方建议蒸馏 LoRA 配 2.0（与 text_cfg 置换 A/B）
    video_compile: bool = False            # torch.compile（blocks-only，节点 177）；Blackwell 需 triton-windows
    video_lora: str = ""                   # 空=模板默认 lightx2v 4 步蒸馏；可换 LoRA 做 A/B
    video_lora_strength: float = 0.8

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError:
            return default
        return value if value > 0 else default

    @staticmethod
    def _env_nonneg_int(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError:
            return default
        return value if value >= 0 else default

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, str(default)))
        except ValueError:
            return default

    @classmethod
    def from_env(cls) -> "Settings":
        # 队列上限可经环境变量覆盖（12 报告 C-3：5080 单机需按实机调参，无需改代码）
        return cls(
            host=os.environ.get("DUOCAST_HOST", "127.0.0.1"),
            trusted_hosts=os.environ.get("DUOCAST_TRUSTED_HOSTS", ""),
            port=int(os.environ.get("DUOCAST_PORT", "8100")),
            storage_root=Path(os.environ.get("DUOCAST_STORAGE", str(PROJECT_ROOT / "apps" / "storage"))),
            queue_cap_gpu=cls._env_int("DUOCAST_QUEUE_GPU", 1),
            queue_cap_api=cls._env_int("DUOCAST_QUEUE_API", 16),
            queue_cap_cpu=cls._env_int("DUOCAST_QUEUE_CPU", 4),
            project_debounce_ms=cls._env_int("DUOCAST_PROJECT_DEBOUNCE_MS", 500),
            tts_provider=os.environ.get("DUOCAST_TTS_PROVIDER", "mock"),
            minimax_key=os.environ.get("DUOCAST_MINIMAX_API_KEY", ""),
            minimax_base_url=os.environ.get("DUOCAST_MINIMAX_BASE_URL", "https://api.minimaxi.com"),
            minimax_model=os.environ.get("DUOCAST_MINIMAX_MODEL", "speech-2.8-hd"),
            minimax_voice_a=os.environ.get("DUOCAST_MINIMAX_VOICE_A", "male-qn-qingse"),
            minimax_voice_b=os.environ.get("DUOCAST_MINIMAX_VOICE_B", ""),
            volcengine_key=os.environ.get("DUOCAST_VOLCENGINE_API_KEY", ""),
            volcengine_base_url=os.environ.get("DUOCAST_VOLCENGINE_BASE_URL", "https://openspeech.bytedance.com"),
            volcengine_model=os.environ.get("DUOCAST_VOLCENGINE_MODEL", "seed-audio-1.0"),
            volcengine_voice_a=os.environ.get("DUOCAST_VOLCENGINE_VOICE_A", ""),
            volcengine_voice_b=os.environ.get("DUOCAST_VOLCENGINE_VOICE_B", ""),
            comfyui_url=os.environ.get("DUOCAST_COMFYUI_URL", "http://127.0.0.1:8188"),
            comfyui_input=Path(os.environ.get(
                "DUOCAST_COMFYUI_INPUT",
                r"D:\软件\Wan2.2-ReMix-SVI2-V3\Wan2.2-ReMix-SVI2-V3\ComfyUI\input")),
            tts_ref_a=os.environ.get(
                "DUOCAST_TTS_REF_A",
                r"D:\Program Files (x86)\PPT_presentation_video\data\model_voice_references\76abe9e06a9d4670b40b0d966faa652c.mp3"),
            tts_ref_b=os.environ.get("DUOCAST_TTS_REF_B", ""),
            image_provider=os.environ.get("DUOCAST_IMAGE_PROVIDER", "mock"),
            toapis_key=os.environ.get("DUOCAST_TOAPIS_KEY", ""),
            image_size=os.environ.get("DUOCAST_IMAGE_SIZE", "4:5"),
            image_quality=os.environ.get("DUOCAST_IMAGE_QUALITY", "low"),
            image_resolution=os.environ.get("DUOCAST_IMAGE_RESOLUTION", "1k"),
            video_provider=os.environ.get("DUOCAST_VIDEO_PROVIDER", "mock"),
            vidu_key=os.environ.get("DUOCAST_VIDU_TOKEN", ""),
            vidu_base_url=os.environ.get("DUOCAST_VIDU_BASE_URL", "https://service.vidu.cn"),
            vidu_model=os.environ.get("DUOCAST_VIDU_MODEL", "3.4"),
            vidu_binary=os.environ.get("DUOCAST_VIDU_PATH", ""),
            text_provider=os.environ.get("DUOCAST_TEXT_PROVIDER", "mock"),
            text_model=os.environ.get("DUOCAST_TEXT_MODEL", "deepseek-flash"),
            video_url=os.environ.get("DUOCAST_VIDEO_URL", "http://127.0.0.1:8191"),
            video_input=Path(os.environ.get("DUOCAST_VIDEO_INPUT", str(PROJECT_ROOT / "runtime" / "video" / "input"))),
            api_token=os.environ.get("DUOCAST_API_TOKEN", "").strip(),
            video_attention=os.environ.get("DUOCAST_VIDEO_ATTENTION", "sdpa"),
            video_blocks_to_swap=cls._env_nonneg_int("DUOCAST_VIDEO_BLOCKS_TO_SWAP", 38),
            video_prefetch_blocks=cls._env_nonneg_int("DUOCAST_VIDEO_PREFETCH_BLOCKS", 0),
            video_text_cfg=cls._env_float("DUOCAST_VIDEO_TEXT_CFG", 2.0),
            video_audio_cfg=cls._env_float("DUOCAST_VIDEO_AUDIO_CFG", 1.0),
            video_compile=os.environ.get("DUOCAST_VIDEO_COMPILE", "0") == "1",
            video_lora=os.environ.get("DUOCAST_VIDEO_LORA", ""),
            video_lora_strength=cls._env_float("DUOCAST_VIDEO_LORA_STRENGTH", 0.8),
        )


settings = Settings.from_env()
