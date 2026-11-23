"""Public runtime preferences; credentials reference environment variables."""
import ipaddress
import os
import re
import socket
from dataclasses import replace
from urllib.parse import urlsplit

FIELDS = {"text_provider", "image_provider", "tts_provider", "video_provider", "text_model", "comfyui_url", "video_url", "text_base_url", "image_base_url", "credential_env", "budget_yuan"}
REMOTE_PROVIDER_HOSTS = frozenset({"toapis.com", "toapis.cn"})
REMOTE_PROVIDER_URL_FIELDS = frozenset({"text_base_url", "image_base_url"})
LOCAL_PROVIDER_URL_FIELDS = frozenset({"comfyui_url", "video_url"})


def _literal_address(hostname: str):
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def _resolved_addresses(hostname: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("服务地址解析失败") from exc
    addresses = []
    for record in records:
        value = record[4][0]
        if "%" in value:
            raise ValueError("服务地址不得包含网络作用域后缀")
        try:
            addresses.append(ipaddress.ip_address(value))
        except ValueError as exc:
            raise ValueError("服务地址解析结果无效") from exc
    if not addresses:
        raise ValueError("服务地址解析结果为空")
    return addresses


def _public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _loopback_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def validate_provider_url(value: str, field: str, *, resolve: bool = False) -> str:
    """Validate a configured provider origin.

    ``resolve=False`` is used by config preflight and never performs DNS. Adapters
    call with ``resolve=True`` immediately before network I/O so hostnames that
    resolve to private/link-local addresses are rejected as well.
    """
    if field not in REMOTE_PROVIDER_URL_FIELDS | LOCAL_PROVIDER_URL_FIELDS:
        raise ValueError("未知的 provider 地址字段")
    if not isinstance(value, str) or not value.strip() or len(value) > 2048 or any(ch.isspace() for ch in value):
        raise ValueError("服务地址必须为非空 URL")
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("服务地址格式无效") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        raise ValueError("服务地址须为无凭据、无查询参数的 HTTP(S) 地址")

    if field in REMOTE_PROVIDER_URL_FIELDS:
        if parsed.scheme != "https" or hostname not in REMOTE_PROVIDER_HOSTS or port not in {None, 443}:
            raise ValueError("远程 provider 仅允许 HTTPS 且主机名必须在白名单内")
        literal = _literal_address(hostname)
        if literal is not None or resolve:
            addresses = [literal] if literal is not None else _resolved_addresses(hostname, port or 443)
            if any(not _public_address(address) for address in addresses):
                raise ValueError("远程 provider 不得解析到私网、回环或链路本地地址")
    else:
        literal = _literal_address(hostname)
        if hostname != "localhost" and (literal is None or not _loopback_address(literal)):
            raise ValueError("本地引擎地址仅允许 localhost 或回环 IP")
        if resolve:
            addresses = _resolved_addresses(hostname, port or (443 if parsed.scheme == "https" else 80))
            if any(not _loopback_address(address) for address in addresses):
                raise ValueError("本地引擎地址不得解析到非回环地址")
    return value


def validate_public_download_url(value: str, *, resolve: bool = True) -> str:
    """Allow signed public HTTPS result URLs while blocking SSRF and redirects."""
    if not isinstance(value, str) or not value.strip() or len(value) > 8192 or any(ch.isspace() for ch in value):
        raise ValueError("下载地址格式无效")
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("下载地址格式无效") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
        or parsed.netloc.endswith(":")
    ):
        raise ValueError("下载地址仅允许无凭据的 HTTPS 地址")
    literal = _literal_address(hostname)
    if literal is not None or resolve:
        addresses = [literal] if literal is not None else _resolved_addresses(hostname, port or 443)
        if any(not _public_address(address) for address in addresses):
            raise ValueError("下载地址不得解析到私网、回环或链路本地地址")
    return value


def validate_config(data):
    if not isinstance(data, dict) or set(data) - FIELDS:
        raise ValueError("包含不支持的配置字段；密钥请通过环境变量引用")
    for key, value in data.items():
        if key != "budget_yuan" and (not isinstance(value, str) or not value.strip() or len(value) > 2048):
            raise ValueError("配置值必须为非空字符串且不超过 2048 字")
        if key in REMOTE_PROVIDER_URL_FIELDS | LOCAL_PROVIDER_URL_FIELDS:
            validate_provider_url(value, key)
        if key == "credential_env" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("凭据引用必须为环境变量名称")
        if key == "budget_yuan" and (type(value) not in (int, float) or not 0 <= value <= 100000):
            raise ValueError("预算须为 0–100000 元")
        allowed = {"text_provider": {"mock", "toapis"}, "image_provider": {"mock", "toapis"}, "tts_provider": {"mock", "comfyui"}, "video_provider": {"mock", "comfyui"}}
        if key in allowed and value not in allowed[key]:
            raise ValueError("不支持的提供方")
    return data


def validate_effective_provider_urls(settings, config: dict) -> None:
    values = {
        "text_base_url": config.get("text_base_url", "https://toapis.com"),
        "image_base_url": config.get("image_base_url", "https://toapis.com"),
        "comfyui_url": config.get("comfyui_url", settings.comfyui_url),
        "video_url": config.get("video_url", settings.video_url),
    }
    for field, value in values.items():
        validate_provider_url(value, field)


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
    if config:
        validate_config(config)
    overrides = {key: value for key, value in config.items() if key in settings.__dataclass_fields__}
    if "credential_env" in config:
        overrides["toapis_key"] = os.environ.get(config["credential_env"], "")
    cfg = replace(settings, **overrides)
    validate_effective_provider_urls(cfg, config)
    return cfg, config
