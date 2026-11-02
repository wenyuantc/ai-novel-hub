"""备份S3/SMTP专用出站防护；保留DNS校验到连接间的窗口，部署须限制出站。"""

import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.services.backups.process import BackupAdapterError


def _resolve(host: str, port: int) -> list[str]:
    return list({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def _allowed_address(address: str, allow_private: bool) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or (ip.is_reserved and not ip.is_loopback):
        return False
    # 额外封禁部分云商特殊元数据地址，即便部署显式允许私网也不能放行。
    if str(ip) in ("100.100.100.200", "192.0.0.192"):
        return False
    return ip.is_global or (allow_private and (ip.is_private or ip.is_loopback))


@dataclass(frozen=True)
class NetworkPolicy:
    allow_private: bool = False
    ca_bundle: str | None = None

    def validate_host(self, host: str, port: int) -> None:
        if not host or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in host) or "%" in host:
            raise BackupAdapterError("network_blocked")
        if not 1 <= port <= 65535:
            raise BackupAdapterError("network_blocked")
        try:
            try:
                ipaddress.ip_address(host)
                addresses = [host]
            except ValueError:
                addresses = _resolve(host, port)
            if not addresses or not all(_allowed_address(value, self.allow_private) for value in addresses):
                raise BackupAdapterError("network_blocked")
        except BackupAdapterError:
            raise
        except (OSError, ValueError):
            raise BackupAdapterError("network_unavailable") from None

    def _origin(self, url: str) -> tuple[str, str, int]:
        try:
            if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url) or "\\" in url:
                raise ValueError
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.fragment:
                raise ValueError
            port = parts.port if parts.port is not None else 443
            if not 1 <= port <= 65535:
                raise ValueError
            return parts.scheme, parts.hostname.lower(), port
        except ValueError:
            raise BackupAdapterError("network_blocked") from None

    def validate_endpoint(self, endpoint: str) -> str:
        scheme, host, port = self._origin(endpoint)
        parts = urlsplit(endpoint)
        if parts.query or parts.path not in ("", "/"):
            raise BackupAdapterError("network_blocked")
        self.validate_host(host, port)
        formatted_host = f"[{host}]" if ":" in host else host
        return f"{scheme}://{formatted_host}:{port}"

    def validate_request(self, url: str, origin: str) -> None:
        scheme, host, port = self._origin(url)
        if self._origin(origin) != (scheme, host, port):
            raise BackupAdapterError("network_blocked")
        self.validate_host(host, port)

    def ssl_context(self) -> ssl.SSLContext:
        try:
            return ssl.create_default_context(cafile=self.ca_bundle)
        except (OSError, ssl.SSLError):
            raise BackupAdapterError("network_blocked") from None
