"""AI 接口地址校验：防止用户借自填 Base URL 让服务端访问内网（SSRF）。

未开启 ALLOW_PRIVATE_AI_ENDPOINTS 时，Base URL 解析出的任一地址若为回环、内网、链路本地
（含云元数据 169.254.169.254）、保留或组播地址即拒绝；同时禁止 SDK 跟随重定向，
避免公网地址 302 跳回内网。

残留风险：校验与实际连接之间存在 DNS 重绑定窗口，需更强隔离时请在网络层限制出站。
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from app.ai.providers.base import AIProviderError

PRIVATE_BLOCKED_HINT = "如需接入本地或内网模型（如 Ollama），请在部署配置中设置 ALLOW_PRIVATE_AI_ENDPOINTS=true"


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


async def _resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def ensure_endpoint_allowed(base_url: str | None) -> None:
    """未填 Base URL 时使用各厂商官方地址，无需校验。"""
    if not base_url:
        return
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AIProviderError("Base URL 格式不正确，应以 http:// 或 https:// 开头")
    host = parts.hostname
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise AIProviderError("Base URL 端口不正确") from exc

    try:
        addresses = [host] if _is_ip_literal(host) else await _resolve(host, port)
    except OSError as exc:
        raise AIProviderError(f"无法解析 Base URL 的域名：{host}") from exc

    for addr in addresses:
        # 去掉 IPv6 zone id（如 fe80::1%eth0）
        if is_blocked_ip(ipaddress.ip_address(addr.split("%", 1)[0])):
            raise AIProviderError(f"Base URL 指向本地或内网地址（{host}），当前部署禁止访问。{PRIVATE_BLOCKED_HINT}")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
