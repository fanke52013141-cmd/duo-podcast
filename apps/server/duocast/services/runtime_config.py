"""Public runtime preferences; credentials reference environment variables."""
import os
import re
from dataclasses import replace
from urllib.parse import urlsplit

FIELDS = {"text_provider", "image_provider", "tts_provider", "video_provider", "text_model", "comfyui_url", "video_url", "text_base_url", "image_base_url", "credential_env", "budget_yuan"}


def validate_config(data):
    if not isinstance(data, dict) or set(data) - FIELDS:
        raise ValueError("包含不支持的配置字段；密钥请通过环境变量引用")
    for key, value in data.items():
        if key != "budget_yuan" and (not isinstance(value, str) or not value.strip() or len(value) > 2048):
            raise ValueError("配置值必须为非空字符串且不超过 2048 字")
        if key.endswith("_url"):
            url = urlsplit(str(value))
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("服务地址须为无凭据、无查询参数的 HTTP(S) 地址")
        if key == "credential_env" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("凭据引用必须为环境变量名称")
        if key == "budget_yuan" and (type(value) not in (int, float) or not 0 <= value <= 100000):
            raise ValueError("预算须为 0–100000 元")
        allowed = {"text_provider": {"mock", "toapis"}, "image_provider": {"mock", "toapis"}, "tts_provider": {"mock", "comfyui"}, "video_provider": {"mock", "comfyui"}}
        if key in allowed and value not in allowed[key]:
            raise ValueError("不支持的提供方")
    return data


def preflight_config(data):
    """Local structural checks only: never contact a provider or expose a secret."""
    errors, warnings = [], []
    try:
        validate_config(data)
    except ValueError as exc:
        return {"valid": False, "errors": [str(exc)], "warnings": []}
    for channel, address in (("text", "text_base_url"), ("image", "image_base_url"), ("tts", "comfyui_url"), ("video", "video_url")):
        if data.get(channel + "_provider", "mock") == "mock":
            continue
        if not data.get(address):
            errors.append(f"{channel} 真实服务缺少地址")
        elif channel in {"text", "image"} and urlsplit(data[address]).path.rstrip("/"):
            errors.append(f"{channel} 请填写服务根地址，不附加 /v1 等路径")
    if data.get("text_provider") == "toapis" and not data.get("text_model", "").strip():
        errors.append("真实文本服务缺少模型名称")
    if any(data.get(k) == "toapis" for k in ("text_provider", "image_provider")):
        name = data.get("credential_env", "DUOCAST_TOAPIS_KEY")
        if not os.environ.get(name):
            warnings.append("当前服务进程未读取到指定凭据环境变量；设置后须重启，再检测连接")
    if data.get("video_provider", "mock") != "mock" and data.get("tts_provider", "mock") == "mock":
        warnings.append("视频使用真实服务而语音仍为演示；真实成片前请配置并确认真实语音")
    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "notice": "仅校验本机配置，不验证模型存在、网络连通或推理质量"}


def effective(settings, machine):
    if os.environ.get("DUOCAST_DEMO_ONLY") == "1":
        return replace(settings, text_provider="mock", image_provider="mock", tts_provider="mock", video_provider="mock"), {}
    config = machine._data().get("runtimeConfig", {})
    overrides = {key: value for key, value in config.items() if key in settings.__dataclass_fields__}
    if "credential_env" in config:
        overrides["toapis_key"] = os.environ.get(config["credential_env"], "")
    return replace(settings, **overrides), config
