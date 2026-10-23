"""世界观 / 角色 / 大纲 的请求与响应模型。"""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

WorldCategory = Literal["place", "faction", "item", "rule", "history", "other"]
LifeStatus = Literal["alive", "dead", "missing", "other"]

MAX_ALIASES = 10
MAX_ALIAS_LEN = 50


def clean_aliases(values: list[str]) -> list[str]:
    """去首尾空白、去空、去重（保序）；单个别名 ≤50 字，最多 10 个。"""
    out: list[str] = []
    for v in values:
        v = v.strip()
        if not v or v in out:
            continue
        if len(v) > MAX_ALIAS_LEN:
            raise ValueError(f"单个别名不能超过 {MAX_ALIAS_LEN} 字")
        out.append(v)
    if len(out) > MAX_ALIASES:
        raise ValueError(f"别名最多 {MAX_ALIASES} 个")
    return out


Aliases = Annotated[list[str], AfterValidator(clean_aliases)]


class WorldSettingCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = ""
    category: WorldCategory = "other"
    sort: int | None = None


class WorldSettingUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = None
    category: WorldCategory | None = None
    sort: int | None = None


class WorldSettingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    content: str
    category: str
    sort: int


class CharacterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role_type: str = Field(default="", max_length=50)
    profile: str = ""
    # 旧关系备注：界面只读，接口保留写入以兼容旧客户端
    relations_text: str = ""
    aliases: Aliases = Field(default_factory=list)
    age: str = Field(default="", max_length=50)
    gender: str = Field(default="", max_length=20)
    appearance: str = ""
    personality: str = ""
    background: str = ""
    current_state: str = ""
    life_status: LifeStatus = "alive"
    # 未传 = 按定位推断（主角默认开启）
    always_in_context: bool | None = None
    sort: int | None = None


class CharacterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    role_type: str | None = Field(default=None, max_length=50)
    profile: str | None = None
    relations_text: str | None = None
    aliases: Aliases | None = None
    age: str | None = Field(default=None, max_length=50)
    gender: str | None = Field(default=None, max_length=20)
    appearance: str | None = None
    personality: str | None = None
    background: str | None = None
    current_state: str | None = None
    life_status: LifeStatus | None = None
    always_in_context: bool | None = None
    sort: int | None = None


class CharacterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
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
    life_status: str
    always_in_context: bool
    sort: int


class OutlineCreate(BaseModel):
    volume_no: int = Field(default=1, ge=1)
    volume_title: str = Field(default="", max_length=200)
    chapter_no: int = Field(default=1, ge=1)
    title: str = Field(min_length=1, max_length=200)
    summary: str = ""
    sort: int | None = None


class OutlineUpdate(BaseModel):
    volume_no: int | None = Field(default=None, ge=1)
    volume_title: str | None = Field(default=None, max_length=200)
    chapter_no: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str | None = None
    sort: int | None = None


class OutlineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    volume_no: int
    volume_title: str
    chapter_no: int
    title: str
    summary: str
    sort: int


class ChapterCastUpdate(BaseModel):
    character_ids: list[int] = Field(default_factory=list, max_length=200)


class ChapterCastOut(BaseModel):
    character_ids: list[int]
