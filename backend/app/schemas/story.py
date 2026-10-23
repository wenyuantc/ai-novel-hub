"""世界观 / 角色 / 大纲 的请求与响应模型。"""

from pydantic import BaseModel, ConfigDict, Field


class WorldSettingCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = ""
    sort: int | None = None


class WorldSettingUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = None
    sort: int | None = None


class WorldSettingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    content: str
    sort: int


class CharacterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role_type: str = Field(default="", max_length=50)
    profile: str = ""
    relations_text: str = ""
    sort: int | None = None


class CharacterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    role_type: str | None = Field(default=None, max_length=50)
    profile: str | None = None
    relations_text: str | None = None
    sort: int | None = None


class CharacterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    role_type: str
    profile: str
    relations_text: str
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
