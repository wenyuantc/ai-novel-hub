"""应用部署配置：环境变量 / .env 为基线，业务设置白名单可由后台持久覆盖。

关键密钥（JWT_SECRET、ENCRYPTION_KEY）缺失或非法时实例化即抛错，实现启动 fail-fast，
避免 API Key 以明文或弱密钥落库。
"""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas.auth import Credentials
from app.schemas.system_settings import MAX_OPEN_API_RATE_LIMIT_PER_MINUTE

_DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


class Settings(BaseSettings):
    # hide_input_in_errors：校验失败时不在错误信息里回显密钥 / 管理员密码原文
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = "postgresql+asyncpg://ainovel:ainovel@localhost:5432/ainovel"
    jwt_secret: str = Field(min_length=16)
    encryption_key: str
    # 业务设置白名单由 SystemSettingsManager 在启动和管理保存后发布有效值。
    registration_enabled: bool = True
    oauth_github_client_id: str = ""
    oauth_github_client_secret: str = Field(default="", repr=False)
    oauth_github_redirect_uri: str = ""
    oauth_linuxdo_client_id: str = ""
    oauth_linuxdo_client_secret: str = Field(default="", repr=False)
    oauth_linuxdo_redirect_uri: str = ""
    cookie_secure: bool = False
    access_token_expire_minutes: int = Field(default=60 * 24 * 7, gt=0)
    # 开放API可运行时覆盖的每Key/UTC分钟上限；不影响网页登录，不重置当前分钟计数。
    open_api_rate_limit_per_minute: int = Field(default=120, gt=0, le=MAX_OPEN_API_RATE_LIMIT_PER_MINUTE)
    # 逗号分隔，例如 http://localhost:5173,http://127.0.0.1:5173；为空表示不启用 CORS
    cors_origins: str = ""
    # 章节生成上下文的字符预算
    context_char_budget: int = Field(default=12000, ge=1000)
    # 单次 AI 请求超时（秒）
    ai_request_timeout: float = Field(default=300.0, gt=0)
    video_job_timeout_seconds: int = Field(default=1800, gt=0, le=86400)
    # 是否允许 AI Base URL 指向本地 / 内网地址（接入 Ollama 等本地模型时开启）；开放注册的公网部署请保持关闭
    allow_private_ai_endpoints: bool = False
    # 任务进程内部通道；默认用私有 UDS，跨实例显式配置私网主机及允许的网络。
    task_peer_enabled: bool = True
    task_peer_socket_dir: str | None = None
    task_peer_bind_host: str | None = None
    task_peer_advertise_host: str | None = None
    task_peer_port: int = Field(default=0, ge=0, le=65535)
    task_peer_networks: str = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1/128,fc00::/7"
    # 后台任务运行时上限：全局/每用户，其余排队；降低不取消已运行任务。
    task_max_concurrency: int = Field(default=4, ge=1)
    task_max_per_user: int = Field(default=2, ge=1)
    # 停止受理新任务，已排队与运行的任务照常完成。
    task_admission_closed: bool = False
    # 实例级自动分析有效开关：关闭后不触发后续自动分析，手动/已排队分析不受影响。
    auto_analyze_enabled: bool = True
    # 实例级MCP有效开关：关闭后插件接口403，智能体禁止后续调用，已发调用允许结束。
    mcp_enabled: bool = True
    mcp_stdio_catalog: str = ""
    media_dir: str = Field(default="./media", min_length=1)
    public_base_url: str = ""
    # 整库备份的部署边界。S3、SMTP 和计划使用独立的 BackupSettings。
    backup_dir: str = Field(default="./backups", min_length=1)
    backup_pg_dump_path: str = Field(default="pg_dump", min_length=1)
    backup_gpg_path: str = Field(default="gpg", min_length=1)
    backup_gpgconf_path: str = Field(default="gpgconf", min_length=1)
    backup_timeout_seconds: float = Field(default=3600, gt=0, le=86400)
    allow_private_backup_endpoints: bool = False
    backup_ca_bundle: str | None = None
    # 生产模式下挂载的前端构建产物目录；不存在则跳过
    frontend_dist_dir: str = str(_DEFAULT_FRONTEND_DIST)
    # 首个管理员引导（可选，须同时设置）：启动时账号不存在则创建，已存在则确保为启用中的管理员，不覆盖密码
    admin_username: str | None = None
    admin_password: str | None = None

    @field_validator("encryption_key")
    @classmethod
    def _validate_fernet_key(cls, v: str) -> str:
        try:
            Fernet(v.encode())
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                "ENCRYPTION_KEY 不是合法的 Fernet 密钥，请使用 "
                '`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成'
            ) from exc
        return v

    @field_validator("public_base_url")
    @classmethod
    def _validate_public_base_url(cls, value: str) -> str:
        if not value:
            return ""
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment or any(c.isspace() for c in value)):
            raise ValueError("PUBLIC_BASE_URL 需要有效的 HTTP(S) 站点地址")
        return value.rstrip("/")

    @field_validator("backup_ca_bundle", mode="before")
    @classmethod
    def _empty_backup_ca(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("backup_dir", "backup_pg_dump_path", "backup_gpg_path", "backup_gpgconf_path", "backup_ca_bundle")
    @classmethod
    def _validate_backup_deployment_path(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(char in value for char in ("\r", "\n", "\x00"))):
            raise ValueError("备份部署路径不能为空或包含控制字符")
        return value

    @field_validator("admin_username", "admin_password", mode="before")
    @classmethod
    def _empty_as_none(cls, v: object) -> object:
        # compose 中未设置的变量会以空字符串传入
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _validate_admin_bootstrap(self) -> "Settings":
        if (self.admin_username is None) != (self.admin_password is None):
            raise ValueError("ADMIN_USERNAME 与 ADMIN_PASSWORD 必须同时设置（或同时留空）")
        if self.admin_username is not None:
            try:
                creds = Credentials(username=self.admin_username, password=self.admin_password)
            except ValidationError as exc:
                reasons = "；".join(str(e.get("msg", "")).removeprefix("Value error, ") for e in exc.errors())
                raise ValueError(f"ADMIN_USERNAME / ADMIN_PASSWORD 不符合注册规则：{reasons}") from None
            self.admin_username = creds.username
        return self

    @model_validator(mode="after")
    def _validate_oauth(self) -> "Settings":
        for name in ("github", "linuxdo"):
            prefix = f"oauth_{name}_"
            values = [getattr(self, prefix + key) for key in ("client_id", "client_secret", "redirect_uri")]
            if not any(values):
                continue
            if not all(value and value.strip() == value for value in values):
                raise ValueError(f"{name} 登录需要完整的 Client ID、Secret 和回调地址")
            url = urlsplit(values[2])
            local_http = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
            if (not url.hostname or not (url.scheme == "https" or local_http) or url.username or url.password
                    or url.query or url.fragment or url.path != f"/api/v1/auth/oauth/{name}/callback"):
                raise ValueError(f"{name} 登录回调地址无效")
        return self

    @property
    def admin_credentials(self) -> Credentials | None:
        if self.admin_username is None or self.admin_password is None:
            return None
        return Credentials(username=self.admin_username, password=self.admin_password)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
