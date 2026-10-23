from datetime import datetime

from pydantic import BaseModel

from app.schemas.auth import Credentials, Password


class AdminUserOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    is_active: bool
    created_at: datetime
    project_count: int


class AdminUserList(BaseModel):
    items: list[AdminUserOut]
    total: int


class AdminUserCreate(Credentials):
    is_admin: bool = False


class AdminUserUpdate(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: Password
