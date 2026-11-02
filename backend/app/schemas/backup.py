"""整库备份唯一传输契约：秘密仅写入，不进入读取、repr 或审计。"""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

BackupDestination = Literal["local", "s3"]
BackupSource = Literal["manual", "scheduled"]
BackupRunStatus = Literal["queued", "running", "succeeded", "partial", "failed", "interrupted"]
BackupArtifactStatus = Literal["pending", "ready", "failed", "unknown", "deleting", "deleted", "missing"]
BackupNotificationStatus = Literal["not_configured", "pending", "sending", "sent", "failed", "unknown"]
BackupCleanupStatus = Literal["not_required", "pending", "succeeded", "failed", "unknown"]
BackupSettingKey = Literal[
    "schedule_enabled",
    "schedule_time",
    "local_enabled",
    "s3_enabled",
    "local_keep",
    "s3_keep",
    "encryption_enabled",
    "backup_password",
    "s3_endpoint",
    "s3_region",
    "s3_bucket",
    "s3_prefix",
    "s3_access_key",
    "s3_secret_key",
    "smtp_host",
    "smtp_port",
    "smtp_security",
    "smtp_username",
    "smtp_password",
    "smtp_from_address",
    "smtp_recipients",
]
SECRET_KEYS = frozenset({"backup_password", "s3_access_key", "s3_secret_key", "smtp_username", "smtp_password"})
KeepCount = Annotated[StrictInt, Field(ge=1, le=1000)]


class BackupConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    schedule_enabled: StrictBool = False
    schedule_time: str = "03:00"
    local_enabled: StrictBool = True
    s3_enabled: StrictBool = False
    local_keep: KeepCount = 7
    s3_keep: KeepCount = 7
    encryption_enabled: StrictBool = True
    s3_endpoint: str = Field(default="", max_length=2048)
    s3_region: str = Field(default="us-east-1", max_length=100)
    s3_bucket: str = Field(default="", max_length=63)
    s3_prefix: str = Field(default="ainovel", max_length=512)
    smtp_host: str = Field(default="", max_length=253)
    smtp_port: Annotated[StrictInt, Field(ge=1, le=65535)] = 465
    smtp_security: Literal["ssl", "starttls"] = "ssl"
    smtp_from_address: EmailStr | Literal[""] = ""
    smtp_recipients: list[EmailStr] = Field(default_factory=list, max_length=10)

    @field_validator("schedule_time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
            raise ValueError("时间必须为 HH:mm")
        return value

    @field_validator("s3_endpoint")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        if not value:
            return value
        try:
            parts = urlsplit(value)
            port = parts.port
            valid = (
                parts.scheme == "https"
                and parts.hostname
                and not parts.username
                and not parts.password
                and not parts.query
                and not parts.fragment
                and parts.path in ("", "/")
                and not any(ord(c) <= 32 or ord(c) == 127 for c in value)
                and "\\" not in value
                and (port is None or 1 <= port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("S3 端点必须为不含路径、身份或查询参数的 HTTPS 地址")
        return value.rstrip("/")

    @field_validator("s3_bucket")
    @classmethod
    def valid_bucket(cls, value: str) -> str:
        if value and (
            re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value) is None
            or ".." in value
            or ".-" in value
            or "-." in value
            or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value)
        ):
            raise ValueError("S3 bucket 名称无效")
        return value

    @field_validator("s3_prefix")
    @classmethod
    def valid_prefix(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or any(p in (".", "..") for p in value.split("/"))
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("S3 前缀无效")
        return value.rstrip("/")

    @field_validator("smtp_host", "s3_region")
    @classmethod
    def protocol_text(cls, value: str) -> str:
        if any(ord(c) <= 32 or ord(c) == 127 for c in value) or any(c in value for c in "/\\@?#"):
            raise ValueError("连接参数无效")
        return value


class BackupSettingsChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    schedule_enabled: StrictBool | None = None
    schedule_time: str | None = None
    local_enabled: StrictBool | None = None
    s3_enabled: StrictBool | None = None
    local_keep: KeepCount | None = None
    s3_keep: KeepCount | None = None
    encryption_enabled: StrictBool | None = None
    s3_endpoint: str | None = None
    s3_region: str | None = None
    s3_bucket: str | None = None
    s3_prefix: str | None = None
    s3_access_key: SecretStr | None = Field(default=None, repr=False)
    s3_secret_key: SecretStr | None = Field(default=None, repr=False)
    backup_password: SecretStr | None = Field(default=None, repr=False)
    smtp_host: str | None = None
    smtp_port: Annotated[StrictInt, Field(ge=1, le=65535)] | None = None
    smtp_security: Literal["ssl", "starttls"] | None = None
    smtp_username: SecretStr | None = Field(default=None, repr=False)
    smtp_password: SecretStr | None = Field(default=None, repr=False)
    smtp_from_address: EmailStr | Literal[""] | None = None
    smtp_recipients: list[EmailStr] | None = Field(default=None, max_length=10)

    @field_validator("backup_password", "s3_access_key", "s3_secret_key", "smtp_username", "smtp_password")
    @classmethod
    def secret_safe(cls, value: SecretStr | None, info) -> SecretStr | None:
        if value is not None:
            text = value.get_secret_value()
            minimum = 16 if info.field_name == "backup_password" else 1
            if not minimum <= len(text) <= 1024 or any(c in text for c in "\r\n\0"):
                raise ValueError("凭据长度或字符无效")
        return value

    @model_validator(mode="after")
    def nonempty_and_nonnull(self):
        if not self.model_fields_set:
            raise ValueError("请至少选择一项设置")
        ordinary = {key: getattr(self, key) for key in self.model_fields_set if key not in SECRET_KEYS}
        if any(value is None for value in ordinary.values()):
            raise ValueError("仅秘密字段允许显式清除")
        # 与完整持久配置共用校验，不让PATCH绕过端点/时间规则。
        BackupConfiguration.model_validate(ordinary)
        return self


class BackupSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    revision: Annotated[StrictInt, Field(ge=0)]
    changes: BackupSettingsChanges


class _UTCModel(BaseModel):
    @field_validator("*", mode="after")
    @classmethod
    def utc_timestamp(cls, value):
        if isinstance(value, datetime):
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value


class BackupSettingsOut(BackupConfiguration, _UTCModel):
    revision: int
    updated_at: datetime | None
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    backup_password_configured: bool
    s3_access_key_configured: bool
    s3_secret_key_configured: bool
    smtp_username_configured: bool
    smtp_password_configured: bool


class BackupManualRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    request_id: UUID
    settings_revision: Annotated[StrictInt, Field(ge=0)]


class BackupArtifactOut(_UTCModel):
    id: int
    destination: BackupDestination
    status: BackupArtifactStatus
    filename: str | None
    size_bytes: int | None
    checksum_sha256: str | None
    error_code: str | None
    error_message: str | None
    downloadable: bool
    deleted_at: datetime | None


class BackupRunOut(_UTCModel):
    id: int
    execution_key: UUID
    source: BackupSource
    actor_user_id: int | None
    actor_username: str | None
    settings_revision: int
    database_engine: Literal["postgresql", "sqlite"]
    schema_revision: str | None
    encrypted: bool
    file_format: str
    status: BackupRunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None
    notification_status: BackupNotificationStatus
    notification_error: str | None
    cleanup_status: BackupCleanupStatus
    cleanup_error: str | None
    artifacts: list[BackupArtifactOut]


class BackupRunList(BaseModel):
    items: list[BackupRunOut]
    total: int


class BackupRunFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: BackupRunStatus | None = None
    source: BackupSource | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class BackupOverview(_UTCModel):
    revision: int
    schedule_enabled: bool
    schedule_time: str
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    local_enabled: bool
    s3_enabled: bool
    local_keep: int
    s3_keep: int
    encryption_enabled: bool
    ready: bool
    readiness_errors: list[str]
    notification_ready: bool
    next_scheduled_at: datetime | None
    active_run: BackupRunOut | None
    last_success: BackupRunOut | None
    last_failure: BackupRunOut | None
    last_available: BackupRunOut | None


class BackupAuditChange(BaseModel):
    """有界字段名与状态；不存连接参数、秘密本身或秘密长度。"""

    model_config = ConfigDict(extra="forbid")
    fields: list[BackupSettingKey] = Field(default_factory=list, max_length=21)
    settings_revision: int = Field(ge=1)
    execution_key: UUID | None = None
    destination: BackupDestination | None = None
    encrypted: bool | None = None
