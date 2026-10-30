"""平台 API Key 的管理契约；默认响应不包含密文或摘要。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, PositiveInt, StringConstraints, field_validator, model_validator

KeyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ApiKeyStatus = Literal["active", "expired", "revoked"]


def default_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=90)


def _future_expiry(value: datetime | None) -> datetime | None:
    if value is not None:
        value = value.astimezone(UTC)
        if value <= datetime.now(UTC):
            raise ValueError("到期时间必须晚于当前时间")
    return value


class ApiKeyCreate(BaseModel):
    name: KeyName
    all_projects: bool = False
    project_ids: list[PositiveInt] = Field(default_factory=list)
    # 省略为 90 天；显式 null 表示不过期。
    expires_at: AwareDatetime | None = Field(default_factory=default_expiry)

    _expiry = field_validator("expires_at")(_future_expiry)


class ApiKeyUpdate(BaseModel):
    name: KeyName | None = None
    all_projects: bool | None = None
    project_ids: list[PositiveInt] | None = None
    expires_at: AwareDatetime | None = None

    _expiry = field_validator("expires_at")(_future_expiry)

    @model_validator(mode="after")
    def _non_nullable_fields(self) -> Self:
        for field in ("name", "all_projects", "project_ids"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} 不能为 null")
        return self


class ApiKeyOut(BaseModel):
    id: int
    name: str
    key_masked: str
    all_projects: bool
    project_ids: list[int]
    status: ApiKeyStatus
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_validator("expires_at", "revoked_at", "last_used_at", "created_at", "updated_at", mode="before")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ApiKeyCreateOut(ApiKeyOut):
    key: str


class ApiKeyRevealOut(BaseModel):
    key: str
