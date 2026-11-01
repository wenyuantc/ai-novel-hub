"""后台角色与配额的严格传输边界。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from app.core.permissions import PERMISSION_CODES, validate_permissions

Revision = Annotated[StrictInt, Field(ge=0)]
PositiveId = Annotated[StrictInt, Field(gt=0)]


class RoleBrief(BaseModel):
    id: int
    name: str
    system_key: str | None
    is_builtin: bool


class AuthorizationOut(BaseModel):
    is_super_admin: bool
    roles: list[RoleBrief]
    effective_permissions: list[str]
    authorization_revision: int


class PermissionOut(BaseModel):
    code: str
    label: str
    group: str
    dangerous: bool
    requires: list[str]


class PermissionList(BaseModel):
    items: list[PermissionOut]


class RoleOut(RoleBrief):
    description: str
    permissions: list[str]
    revision: int
    member_count: int
    created_at: datetime
    updated_at: datetime
    authorization_revision: int


class RoleList(BaseModel):
    items: list[RoleOut]
    total: int
    authorization_revision: int


class RoleFields(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    @field_validator("permissions", check_fields=False)
    @classmethod
    def permission_values(cls, value):
        return validate_permissions(value) if value is not None else value

    @field_validator("name", check_fields=False)
    @classmethod
    def nonempty_name(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("角色名称不能为空")
        return value


class RoleCreate(RoleFields):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    permissions: list[StrictStr] | None = Field(default=None, max_length=len(PERMISSION_CODES))
    source_role_id: PositiveId | None = None
    source_revision: Revision | None = None

    @model_validator(mode="after")
    def complete_source(self):
        if "permissions" in self.model_fields_set and self.permissions is None:
            raise ValueError("权限列表不能为null")
        if (self.source_role_id is None) != (self.source_revision is None):
            raise ValueError("复制角色必须同时提供来源和版本")
        return self


class RoleUpdate(RoleFields):
    authorization_revision: Revision
    revision: Revision
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    permissions: list[StrictStr] | None = Field(default=None, max_length=len(PERMISSION_CODES))

    @model_validator(mode="after")
    def nonempty(self):
        fields = self.model_fields_set - {"authorization_revision", "revision"}
        if not fields or any(getattr(self, key) is None for key in fields):
            raise ValueError("请提供非空角色修改，省略表示不变")
        return self


class UserRolesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    authorization_revision: Revision
    role_ids: list[PositiveId]

    @field_validator("role_ids")
    @classmethod
    def distinct(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("角色不能重复")
        return value


class InheritedQuota(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["inherit"]


class UnlimitedQuota(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["unlimited"]


class LimitedQuota(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["limited"]
    value: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)]


QuotaConfig = Annotated[InheritedQuota | UnlimitedQuota | LimitedQuota, Field(discriminator="mode")]


class QuotaChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_count: QuotaConfig | None = None
    task_concurrency: QuotaConfig | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("请提供至少一项非空配额配置")
        return self


class UserQuotaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    authorization_revision: Revision
    revision: Revision
    changes: QuotaChanges


class QuotaItem(BaseModel):
    config: QuotaConfig
    effective_limit: int | None
    source: Literal["user", "system", "default"]
    used: int = Field(ge=0)


class UserQuotaOut(BaseModel):
    user_id: int
    username: str
    is_management_account: bool
    revision: int
    authorization_revision: int
    global_task_limit: int
    project_count: QuotaItem
    task_concurrency: QuotaItem
