from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=72)

    @field_validator("username")
    @classmethod
    def _strip_username(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("用户名至少 3 个字符")
        if any(ch.isspace() for ch in v):
            raise ValueError("用户名不能包含空白字符")
        return v

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, v: str) -> str:
        # bcrypt 只接受不超过 72 字节的密码
        if len(v.encode("utf-8")) > 72:
            raise ValueError("密码过长（不超过 72 字节）")
        return v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    created_at: datetime


class AuthConfigOut(BaseModel):
    registration_enabled: bool
