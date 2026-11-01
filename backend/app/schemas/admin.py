from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.auth import Credentials, Password
from app.schemas.project import ProjectStatus
from app.schemas.rbac import RoleBrief


class AdminProjectOut(BaseModel):
    id: int
    title: str
    genre: str
    status: ProjectStatus
    owner_user_id: int
    owner_username: str
    chapter_count: int
    total_words: int
    created_at: datetime
    updated_at: datetime


class AdminProjectList(BaseModel):
    items: list[AdminProjectOut]
    total: int


class AdminUserOut(BaseModel):
    is_super_admin: bool
    is_management_account: bool
    roles: list[RoleBrief]
    authorization_revision: int
    id: int
    username: str
    is_admin: bool
    is_active: bool
    created_at: datetime
    project_count: int


class AdminUserList(BaseModel):
    items: list[AdminUserOut]
    total: int


class AdminResourceUsage(BaseModel):
    project_count: int = Field(ge=0)
    chapter_count: int = Field(ge=0)
    total_words: int = Field(ge=0)


class AdminUserUsage(BaseModel):
    active: AdminResourceUsage
    recycled: AdminResourceUsage
    deleting: AdminResourceUsage


class AdminUserDetail(AdminUserOut):
    last_login_at: datetime | None
    usage: AdminUserUsage


class AdminUserCreate(Credentials):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class AdminUserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: Password
