"""站内作品与系列邀请的严格输入、版本和安全响应。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.setting_field_value import PositiveDbInt

Role = Literal["viewer", "editor"]
Scope = Literal["project", "series"]


class ProjectInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    role: Role
    allow_owner_ai: bool = Field(default=False, strict=True)

    @field_validator("username")
    @classmethod
    def username_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("用户名不能为空")
        return value


class SeriesInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)

    @field_validator("username")
    @classmethod
    def username_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("用户名不能为空")
        return value


class ProjectMemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: PositiveDbInt
    role: Role | None = None
    allow_owner_ai: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def has_change(self):
        fields = self.model_fields_set.intersection({"role", "allow_owner_ai"})
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("请提供角色或模型授权")
        return self


class InvitationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: PositiveDbInt


class MemberOut(BaseModel):
    user_id: int
    username: str
    role: Role
    status: Literal["pending", "accepted", "declined", "revoked"]
    allow_owner_ai: bool | None = None
    invitation_id: str
    revision: int
    responded_at: datetime | None
    created_at: datetime
    updated_at: datetime


class InvitationOut(BaseModel):
    scope: Scope
    resource_id: int
    title: str
    owner: str
    role: Role
    allow_owner_ai: bool | None = None
    invitation_id: str
    revision: int
