from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator


def _check_password_bytes(v: str) -> str:
    # bcrypt 只接受不超过 72 字节的密码
    if len(v.encode("utf-8")) > 72:
        raise ValueError("密码过长（不超过 72 字节）")
    return v


# 注册、管理员创建用户、重置密码、自助改密共用的密码规则
Password = Annotated[str, Field(min_length=6, max_length=72), AfterValidator(_check_password_bytes)]


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: Password

    @field_validator("username")
    @classmethod
    def _strip_username(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("用户名至少 3 个字符")
        if any(ch.isspace() for ch in v):
            raise ValueError("用户名不能包含空白字符")
        return v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    is_admin: bool
    created_at: datetime


class AuthConfigOut(BaseModel):
    registration_enabled: bool


class ChangePasswordRequest(BaseModel):
    # 原密码只做长度上限，规则不符时也应走「原密码不正确」而不是 422
    old_password: str = Field(min_length=1, max_length=200)
    new_password: Password
