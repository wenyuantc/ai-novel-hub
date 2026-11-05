"""组织与成员的请求 / 响应模型。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.progression import AnchorKind, Boundary, NonnegativeDbInt, PositiveDbInt

MemberStatus = Literal["active", "left", "expelled", "dead"]


def validate_member_anchor(anchor_kind: AnchorKind | None, chapter_id: int | None, chapter_no: int | None) -> None:
    if chapter_id is not None and chapter_no is not None:
        raise ValueError("不能同时指定章节 id 和章节号")
    if anchor_kind == "chapter" and chapter_id is None:
        raise ValueError("已有章节锚点必须指定章节 id")
    if anchor_kind == "planned" and chapter_no is None:
        raise ValueError("计划章节锚点必须指定章节号")


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


class _MemberIntent(BaseModel):
    edit_mode: Literal["baseline", "progression"] | None = None
    expected_revision: PositiveDbInt | None = None
    anchor_kind: AnchorKind | None = None
    chapter_id: PositiveDbInt | None = None
    chapter_no: PositiveDbInt | None = None
    boundary: Boundary = "start"

    @model_validator(mode="after")
    def validate_position(self) -> Self:
        validate_member_anchor(self.anchor_kind, self.chapter_id, self.chapter_no)
        has_position = self.chapter_id is not None or self.chapter_no is not None
        if self.edit_mode == "baseline" and has_position:
            raise ValueError("修正基础设定不能指定生效章节")
        if self.edit_mode == "progression" and not has_position:
            raise ValueError("记录剧情变化必须指定生效章节")
        if not has_position and self.boundary != "start":
            raise ValueError("指定章节后才能选择生效边界")
        return self


class MemberCreate(_MemberIntent):
    # 0 表示调用方确认主体时间线不存在；正数表示编辑已存在的时间线。
    expected_revision: NonnegativeDbInt | None = None
    character_id: PositiveDbInt
    position: str = Field(default="", max_length=50)
    status: MemberStatus = "active"
    note: str = ""


class MemberUpdate(_MemberIntent):
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
