"""开放读取的字段白名单；与内部编辑/运行时响应隔离。"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.schemas.chapter import ChapterStatus
from app.schemas.organization import MemberStatus
from app.schemas.project import ProjectStatus
from app.schemas.relationship import RelationCategory, RelationStatus
from app.schemas.story import LifeStatus, WorldCategory


def as_utc(value: datetime) -> datetime:
    """SQLite 的无时区时间同现有数据约定视为 UTC，对外始终带时区。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UtcTime = Annotated[datetime, AfterValidator(as_utc)]


class OpenModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int


class OpenError(BaseModel):
    detail: str


class ValidationIssue(BaseModel):
    type: str
    loc: list[str | int]
    msg: str


class OpenValidationError(OpenError):
    errors: list[ValidationIssue]


class KeyInfo(BaseModel):
    id: int
    name: str
    permissions: list[Literal["read"]] = Field(default_factory=lambda: ["read"])
    all_projects: bool
    project_ids: list[int]
    expires_at: UtcTime | None


class Novel(OpenModel):
    id: int
    title: str
    genre: str
    premise: str
    synopsis: str
    theme: str
    tags: list[str]
    selling_points: str
    audience: str
    target_words: int | None
    status: ProjectStatus
    chapter_count: int = 0
    total_words: int = 0
    created_at: UtcTime
    updated_at: UtcTime = Field(description="小说元数据更新时间，不包含所有章节或设定的变更。")
    content_updated_at: UtcTime | None = Field(default=None, description="现存章节行的最近更新时间；无章节为 null。")
    cover_updated_at: UtcTime | None = None
    cover_url: str | None = Field(default=None, description="受 X-API-Key 保护的相对图片地址，不含密钥。")


class NovelEntity(OpenModel):
    id: int
    novel_id: int = Field(validation_alias="project_id")
    created_at: UtcTime
    updated_at: UtcTime


class ChapterBrief(NovelEntity):
    chapter_no: int
    title: str
    status: ChapterStatus
    word_count: int
    volume_no: int | None = None
    volume_title: str | None = None


class ChapterDetail(ChapterBrief):
    content: str = Field(description="当前保存正文；空章节返回空字符串。")


class Character(NovelEntity):
    name: str
    role_type: str
    profile: str
    relations_text: str
    aliases: list[str]
    age: str
    gender: str
    appearance: str
    personality: str
    background: str
    current_state: str
    life_status: LifeStatus
    sort: int


class WorldSetting(NovelEntity):
    title: str
    content: str
    category: WorldCategory
    sort: int


class Outline(NovelEntity):
    volume_no: int
    volume_title: str
    chapter_no: int
    title: str
    summary: str
    sort: int


class ChapterPlan(NovelEntity):
    outline_id: int
    seq: int
    title: str
    summary: str
    key_events: list[str]
    character_ids: list[int] = Field(description="已过滤当前小说中不存在的人物 ID。")
    emotion: str
    goal: str
    est_words: int
    chapter_id: int | None
    chapter_no: int | None
    has_content: bool


class Organization(NovelEntity):
    name: str
    org_type: str
    purpose: str
    location: str
    power_level: int | None
    parent_id: int | None
    description: str
    sort: int
    member_count: int = Field(default=0, description="所有成员状态的总数，包含离开、被逐和死亡成员。")


class OrganizationMember(NovelEntity):
    organization_id: int
    organization_name: str
    character_id: int
    character_name: str
    position: str
    status: MemberStatus
    note: str


class Relationship(NovelEntity):
    a_id: int
    b_id: int
    a_name: str
    b_name: str
    type_id: int
    category: RelationCategory
    role_a: str = Field(description="a 是 b 的身份；从 b 视角看 a 时使用此称呼。")
    role_b: str = Field(description="b 是 a 的身份；从 a 视角看 b 时使用此称呼。")
    intimacy: int
    status: RelationStatus
    description: str
    other_id: int | None = None
    other_name: str | None = None
    other_role: str | None = None
    self_role: str | None = None


class RelationshipType(OpenModel):
    id: int
    novel_id: int | None = Field(validation_alias="project_id", description="null 为全局预置类型。")
    category: RelationCategory
    role_a: str
    role_b: str
    sort: int
    builtin: bool
    created_at: UtcTime
    updated_at: UtcTime
