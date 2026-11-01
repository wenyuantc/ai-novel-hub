"""实例运行设置的唯一白名单与安全传输契约。"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

BoolSettingKey = Literal["registration_enabled", "auto_analyze_enabled", "mcp_enabled"]
IntSettingKey = Literal["task_max_concurrency", "task_max_per_user", "open_api_rate_limit_per_minute"]
SystemSettingKey = BoolSettingKey | IntSettingKey
SystemSettingSource = Literal["admin", "deployment", "default"]
SystemSettingValue = StrictBool | StrictInt
PositiveSettingInt = Annotated[StrictInt, Field(ge=1)]
# API Key计数列为SQL Integer；阈值也必须能由PostgreSQL/SQLite实际绑定和计数。
MAX_OPEN_API_RATE_LIMIT_PER_MINUTE = 2_147_483_647
RateLimitSettingInt = Annotated[StrictInt, Field(ge=1, le=MAX_OPEN_API_RATE_LIMIT_PER_MINUTE)]
SYSTEM_SETTING_KEYS: tuple[SystemSettingKey, ...] = (
    "registration_enabled", "task_max_concurrency", "task_max_per_user", "auto_analyze_enabled",
    "mcp_enabled", "open_api_rate_limit_per_minute",
)


class SystemSettingsChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    registration_enabled: StrictBool | None = None
    task_max_concurrency: PositiveSettingInt | None = None
    task_max_per_user: PositiveSettingInt | None = None
    auto_analyze_enabled: StrictBool | None = None
    mcp_enabled: StrictBool | None = None
    open_api_rate_limit_per_minute: RateLimitSettingInt | None = None


class SystemSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    revision: Annotated[StrictInt, Field(ge=0)]
    changes: SystemSettingsChanges

    @model_validator(mode="after")
    def nonempty(self):
        if not self.changes.model_fields_set:
            raise ValueError("请至少选择一项设置")
        return self


class _SettingItem(BaseModel):
    source: SystemSettingSource
    deployment_source: Literal["deployment", "default"]
    overridden: bool
    requires_restart: Literal[False] = False


class SystemSettingBoolItem(_SettingItem):
    key: BoolSettingKey
    value: StrictBool
    deployment_value: StrictBool
    default_value: StrictBool


class SystemSettingIntItem(_SettingItem):
    key: IntSettingKey
    value: PositiveSettingInt
    deployment_value: PositiveSettingInt
    default_value: PositiveSettingInt


class SystemSettingsOut(BaseModel):
    revision: int
    updated_at: datetime | None
    items: list[SystemSettingBoolItem | SystemSettingIntItem]

    @field_validator("updated_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SystemSettingAuditChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: SystemSettingKey
    before_override: SystemSettingValue | None
    after_override: SystemSettingValue | None
    before_effective: SystemSettingValue
    after_effective: SystemSettingValue
    before_source: SystemSettingSource
    after_source: SystemSettingSource

    @model_validator(mode="after")
    def match_key_type(self):
        integer = self.key in ("task_max_concurrency", "task_max_per_user", "open_api_rate_limit_per_minute")
        for value in (self.before_override, self.after_override, self.before_effective, self.after_effective):
            if value is not None and (type(value) is not (int if integer else bool) or (integer and value < 1)):
                raise ValueError("设置变更类型不匹配")
        return self
