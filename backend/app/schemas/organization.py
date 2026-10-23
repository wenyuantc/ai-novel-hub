"""组织与成员的请求 / 响应模型。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MemberStatus = Literal["active", "left", "expelled", "dead"]


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    org_type: str = Field(default="", max_length=50)
    purpose: str = ""
    location: str = Field(default="", max_length=200)
    power_level: int | None = Field(default=None, ge=0, le=100)
    parent_id: int | None = None
    description: str = ""
    sort: int | None = None


class OrganizationUpdate(BaseModel):
    """parent_id / power_level 显式传 null = 清空；省略 = 不变。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    org_type: str | None = Field(default=None, max_length=50)
    purpose: str | None = None
    location: str | None = Field(default=None, max_length=200)
    power_level: int | None = Field(default=None, ge=0, le=100)
    parent_id: int | None = None
    description: str | None = None
    sort: int | None = None


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    org_type: str
    purpose: str
    location: str
    power_level: int | None
    parent_id: int | None
    description: str
    sort: int
    member_count: int = 0


class MemberCreate(BaseModel):
    character_id: int
    position: str = Field(default="", max_length=50)
    status: MemberStatus = "active"
    note: str = ""


class MemberUpdate(BaseModel):
    position: str | None = Field(default=None, max_length=50)
    status: MemberStatus | None = None
    note: str | None = None


class MemberOut(BaseModel):
    id: int
    organization_id: int
    organization_name: str
    character_id: int
    character_name: str
    position: str
    status: str
    note: str


class OrganizationWithMembers(BaseModel):
    """AI 生成组织预览确认后一次保存：组织 + 勾选的成员（单事务）。"""

    organization: OrganizationCreate
    members: list[MemberCreate] = Field(default_factory=list, max_length=200)


class OrganizationWithMembersOut(BaseModel):
    organization: OrganizationOut
    members: list[MemberOut]
