"""仅创建和回收本次验收拥有的 Docker 资源，绝不读取项目 .env。"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# 官方仓库及 TLS 文档：https://docs.rustfs.com/en/installation/container/docker
# https://docs.rustfs.com/en/integration/tls-configured；2026-11-02 查询官方 manifest。
# MinIO 官方镜像已不允许匿名拉取，不能依赖会失败的历史 tag 或第三方重打包。
S3_IMAGE = "rustfs/rustfs@sha256:ed9210d6411d77ffceb70d94b964a3cb7d225983368bc25bebe01e995293dcc2"
PG_IMAGE = "postgres@sha256:deb28ee471183cb0c303113dac9b2433c51eeeeb678def7af9a9ec5820367815"
HELPERS = Path(__file__).resolve().parent


class AcceptanceFailure(RuntimeError):
    """只包含固定检查名称；不透传 Docker、HTTP、数据库或供应商原始异常。"""


def require(condition: object, check: str) -> None:
    if not condition:
        raise AcceptanceFailure(check)


def command(*args: str, data: bytes | None = None, within: int = 90, check: str = "docker_command") -> bytes:
    try:
        result = subprocess.run(args, input=data, capture_output=True, timeout=within, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise AcceptanceFailure(check) from None
    if result.returncode:
        raise AcceptanceFailure(check)
    return result.stdout


def private_write(path: Path, data: str | bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(data.encode() if isinstance(data, str) else data)


def make_ca(directory: Path) -> None:
    """测试 CA 只留在本次 0700 临时目录，不进入证据文件或镜像。"""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Isolated backup acceptance CA")])
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .sign(ca_key, hashes.SHA256())
    )
    private_write(directory / "ca.pem", ca.public_bytes(serialization.Encoding.PEM))
    for host, prefix in (("s3.acceptance.test", "rustfs"), ("smtp.acceptance.test", "smtp")):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), False).sign(ca_key, hashes.SHA256())
        )
        private_write(directory / f"{prefix}_cert.pem", cert.public_bytes(serialization.Encoding.PEM))
        private_write(
            directory / f"{prefix}_key.pem",
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ),
        )


@dataclass
class DockerLab:
    image: str
    token: str = field(default_factory=lambda: secrets.token_hex(6))
    containers: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    secrets_to_check: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="ainovel-backup-acceptance-")
        self.root = Path(self._temporary.name)
        self.root.chmod(0o700)
        self.network = f"backup-acceptance-{self.token}"
        self._network_created = False
        self.pg_user = "backup_acceptance"
        self.pg_password = secrets.token_urlsafe(30)
        self.s3_access = "acceptance-" + secrets.token_hex(10)
        self.s3_secret = secrets.token_urlsafe(35)
        self.smtp_username = "smtp-" + secrets.token_hex(10)
        self.smtp_password = secrets.token_urlsafe(35)
        self.admin_password = secrets.token_urlsafe(30)
        self.backup_password = secrets.token_urlsafe(36)
        self.provider_secret = "sk-acceptance-" + secrets.token_urlsafe(32)
        self.fernet_key = Fernet.generate_key().decode()
        self.jwt_secret = secrets.token_urlsafe(40)
        self.secrets_to_check.extend([
            self.pg_password, self.s3_access, self.s3_secret, self.admin_password,
            self.backup_password, self.provider_secret, self.fernet_key, self.jwt_secret,
            self.smtp_username, self.smtp_password,
        ])

    def __enter__(self) -> DockerLab:
        try:
            return self._enter()
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def _enter(self) -> DockerLab:
        command("docker", "version", "--format", "{{.Server.Version}}", check="docker_available")
        self.facts["app_image_id"] = command(
            "docker", "image", "inspect", self.image, "--format", "{{.Id}}", check="application_image_exists",
        ).decode().strip()
        # 主会话可能同时重建同一个tag；本次所有容器必须使用同一不可变image ID。
        self._app_image = self.facts["app_image_id"]
        self.facts["postgres_image"] = PG_IMAGE
        self.facts["s3_image"] = S3_IMAGE
        command(
            # Docker Desktop 的 internal bridge 不提供宿主机 publish 映射。
            # 专属 bridge + 仅127.0.0.1发布应用端口；PG/S3/SMTP均不发布，且从不加入已有网络。
            "docker", "network", "create", "--label", f"ainovel.backup.acceptance={self.token}",
            self.network, check="isolated_network_created",
        )
        self._network_created = True
        tls = self.root / "tls"
        tls.mkdir(mode=0o700)
        make_ca(tls)
        return self

    def __exit__(self, _kind, _error, _traceback) -> None:
        cleanup_errors = []
        for name in reversed(self.containers):
            try:
                # 一次性 maintenance 容器带 --rm，已退出就不再调用 rm。
                existing = subprocess.run(
                    ["docker", "inspect", name, "--format", "{{index .Config.Labels \"ainovel.backup.acceptance\"}}"],
                    capture_output=True, timeout=15, check=False,
                )
                if existing.returncode:
                    continue
                require(existing.stdout.decode().strip() == self.token, "cleanup_ownership_label")
                command("docker", "rm", "--force", "--volumes", name, within=45, check="container_cleanup")
            except AcceptanceFailure:
                cleanup_errors.append("container_cleanup")
        for volume in reversed(self.volumes):
            try:
                command("docker", "volume", "rm", volume, check="volume_cleanup")
            except AcceptanceFailure:
                cleanup_errors.append("volume_cleanup")
        if self._network_created:
            try:
                command("docker", "network", "rm", self.network, check="network_cleanup")
            except AcceptanceFailure:
                cleanup_errors.append("network_cleanup")
        self._temporary.cleanup()
        self.facts["resource_cleanup"] = "failed" if cleanup_errors else "passed"
        if cleanup_errors and _error is None:
            raise AcceptanceFailure("owned_resources_removed")

    def envfile(self, name: str, values: dict[str, str]) -> Path:
        path = self.root / f"{name}.env"
        require(all("\n" not in value and "\r" not in value for value in values.values()), "env_line_safety")
        private_write(path, "".join(f"{key}={value}\n" for key, value in values.items()))
        return path

    def volume(self, suffix: str) -> str:
        name = f"{self.network}-{suffix}"
        command("docker", "volume", "create", "--label", f"ainovel.backup.acceptance={self.token}", name)
        self.volumes.append(name)
        return name

    def run(self, suffix: str, image: str, *args: str, options: tuple[str, ...] = ()) -> str:
        name = f"{self.network}-{suffix}"
        if image == self.image:
            image = self._app_image
        # CLI 超时也可能已创建容器，提前登记随机专属名称，退出仍能回收。
        self.containers.append(name)
        command(
            "docker", "run", "--detach", "--name", name, "--network", self.network,
            "--label", f"ainovel.backup.acceptance={self.token}", *options, image, *args,
            within=240, check=f"start_{suffix}",
        )
        return name

    def exec(self, container: str, *args: str, data: bytes | None = None, check: str = "container_exec") -> bytes:
        require(container in self.containers, "exec_owned_container")
        return command("docker", "exec", "--interactive", container, *args, data=data, within=120, check=check)

    def probe(self, container: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.exec(
            container, "python", "/acceptance/probe.py", action,
            data=json.dumps(payload or {}).encode(), check=f"probe_{action}",
        )
        try:
            decoded = json.loads(result)
            if decoded.get("probe_failed"):
                raise AcceptanceFailure(f"probe_{action}_{decoded['error_type']}_line_{decoded['probe_line']}")
            return decoded
        except (ValueError, TypeError):
            raise AcceptanceFailure(f"probe_{action}_json") from None

    def maintenance(self, engine: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """应用停机时用独立工具容器修改合成夹具，不需要运行真实业务进程。"""
        name = f"{self.network}-maintenance-{secrets.token_hex(4)}"
        self.containers.append(name)
        result = command(
            "docker", "run", "--rm", "--interactive", "--name", name, "--network", self.network,
            "--label", f"ainovel.backup.acceptance={self.token}", "--env-file", str(self.root / f"{engine}.env"),
            "--mount", f"type=bind,src={self.root},dst=/acceptance-data",
            "--mount", f"type=bind,src={HELPERS},dst=/acceptance,readonly",
            "--entrypoint", "python", self._app_image, "/acceptance/probe.py", action,
            data=json.dumps(payload or {}).encode(), within=120, check=f"maintenance_{action}",
        )
        return json.loads(result)

    def start_postgres(self) -> str:
        env = self.envfile("postgres", {
            "POSTGRES_USER": self.pg_user, "POSTGRES_PASSWORD": self.pg_password, "POSTGRES_DB": "source",
        })
        self.pg = self.run("pg", PG_IMAGE, options=(
            "--network-alias", "pg.acceptance.test", "--env-file", str(env),
            "--mount", f"type=volume,src={self.volume('pg')},dst=/var/lib/postgresql/data",
        ))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                self.exec(self.pg, "pg_isready", "--username", self.pg_user, "--dbname", "source")
                return self.pg
            except AcceptanceFailure:
                time.sleep(0.5)
        raise AcceptanceFailure("postgres_ready")

    def start_external_services(self) -> None:
        env = self.envfile("s3", {
            "RUSTFS_ACCESS_KEY": self.s3_access, "RUSTFS_SECRET_KEY": self.s3_secret,
            "RUSTFS_ADDRESS": ":9000", "RUSTFS_CONSOLE_ENABLE": "false",
            "RUSTFS_TLS_PATH": "/certs", "RUSTFS_OBS_LOGGER_LEVEL": "error",
        })
        self.s3 = self.run("s3", S3_IMAGE, "/data", options=(
            "--network-alias", "s3.acceptance.test", "--user", "0:0", "--env-file", str(env),
            "--mount", f"type=bind,src={self.root / 'tls'},dst=/certs,readonly",
            "--mount", f"type=volume,src={self.volume('s3')},dst=/data",
        ))
        smtp_env = self.envfile("smtp", {"SMTP_USERNAME": self.smtp_username, "SMTP_PASSWORD": self.smtp_password})
        self.smtp = self.run("smtp", self.image, "python", "/acceptance/smtp_capture.py", options=(
            "--network-alias", "smtp.acceptance.test", "--entrypoint", "",
            "--env-file", str(smtp_env),
            "--mount", f"type=bind,src={HELPERS},dst=/acceptance,readonly",
            "--mount", f"type=bind,src={self.root},dst=/acceptance-data",
        ))

    def start_app(self, engine: str) -> tuple[str, str]:
        directory = self.root / engine
        directory.mkdir(mode=0o700)
        (directory / "backups").mkdir(mode=0o700)
        url = (
            f"postgresql+asyncpg://{self.pg_user}:{self.pg_password}@pg.acceptance.test:5432/source"
            if engine == "postgresql" else "sqlite+aiosqlite:////acceptance-data/sqlite/source.sqlite3"
        )
        env = self.envfile(engine, self.application_env(url, f"/acceptance-data/{engine}/backups"))
        container = self.run(engine, self.image, options=(
            "--publish", "127.0.0.1::8000", "--env-file", str(env),
            "--mount", f"type=bind,src={self.root},dst=/acceptance-data",
            "--mount", f"type=bind,src={HELPERS},dst=/acceptance,readonly",
        ))
        return container, self.app_url(container)

    def application_env(self, url: str, backup_dir: str) -> dict[str, str]:
        return {
            "DATABASE_URL": url, "JWT_SECRET": self.jwt_secret, "ENCRYPTION_KEY": self.fernet_key,
            "ADMIN_USERNAME": "acceptance_admin", "ADMIN_PASSWORD": self.admin_password,
            "COOKIE_SECURE": "false", "REGISTRATION_ENABLED": "true", "AUTO_ANALYZE_ENABLED": "false",
            "BACKUP_DIR": backup_dir, "BACKUP_TIMEOUT_SECONDS": "90",
            "ALLOW_PRIVATE_BACKUP_ENDPOINTS": "true", "BACKUP_CA_BUNDLE": "/acceptance-data/tls/ca.pem",
        }

    def start_restored(self, engine: str, run_id: int, database_url: str) -> tuple[str, str]:
        suffix = f"restored-{engine}-{run_id}"
        directory = self.root / suffix
        directory.mkdir(mode=0o700)
        (directory / "backups").mkdir(mode=0o700)
        env = self.envfile(suffix, self.application_env(database_url, f"/acceptance-data/{suffix}/backups"))
        container = self.run(suffix, self.image, options=(
            "--publish", "127.0.0.1::8000", "--env-file", str(env),
            "--mount", f"type=bind,src={self.root},dst=/acceptance-data",
            "--mount", f"type=bind,src={HELPERS},dst=/acceptance,readonly",
        ))
        return container, self.app_url(container)

    def app_url(self, container: str) -> str:
        require(container in self.containers, "port_owned_container")
        until = time.monotonic() + 10
        while time.monotonic() < until:
            ports = json.loads(command("docker", "inspect", container, "--format", "{{json .NetworkSettings.Ports}}"))
            published = ports.get("8000/tcp")
            if published and published[0].get("HostPort"):
                return f"http://127.0.0.1:{published[0]['HostPort']}"
            time.sleep(0.1)
        raise AcceptanceFailure("loopback_application_port_published")

    def assert_logs_safe(self) -> None:
        for container in self.containers:
            result = subprocess.run(["docker", "logs", container], capture_output=True, timeout=30, check=False)
            if result.returncode and "maintenance" in container:
                continue
            require(result.returncode == 0, "read_owned_logs")
            logs = (result.stdout + result.stderr).decode(errors="replace")
            require(not any(value in logs for value in self.secrets_to_check), "no_secret_in_container_logs")
