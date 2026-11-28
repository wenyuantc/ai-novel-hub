"""手工录入与编辑素材的字段边界。PATCH 只写入请求实际携带的字段。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.plugins.drama.episode_schemas import LongText as Body
from app.plugins.drama.episode_schemas import Revision
from app.plugins.drama.episode_schemas import Title as Name

Label = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
LinkId = Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)] | None


class AssetPatch(BaseModel):
    revision: Revision

    @model_validator(mode="after")
    def validate_changes(self):
        changes = self.model_fields_set - {"revision"}
        if not changes:
            raise ValueError("至少填写一个修改项")
        if any(getattr(self, k) is None for k in changes - {"novel_character_id", "novel_world_setting_id"}):
            raise ValueError("字段不能为空值")
        return self


class AssetInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CharacterFields(AssetInput):
    role: Body = ""
    description: Body = ""
    appearance: Body = ""
    novel_character_id: LinkId = None


class CharacterCreate(CharacterFields):
    name: Name


class CharacterPatch(CharacterFields, AssetPatch):
    name: Name | None = None


class LookFields(AssetInput):
    styling: Body = ""
    image_prompt: Body = ""
    is_default: bool = False


class LookCreate(LookFields):
    name: Name


class LookPatch(LookFields, AssetPatch):
    name: Name | None = None


class SceneFields(AssetInput):
    time_of_day: Label = ""
    description: Body = ""
    lighting: Body = ""
    image_prompt: Body = ""
    novel_world_setting_id: LinkId = None


class SceneCreate(SceneFields):
    location: Name


class ScenePatch(SceneFields, AssetPatch):
    location: Name | None = None


class PropFields(AssetInput):
    category: Body = ""
    description: Body = ""
    image_prompt: Body = ""


class PropCreate(PropFields):
    name: Name


class PropPatch(PropFields, AssetPatch):
    name: Name | None = None


class LookOut(BaseModel):
    id: int
    character_id: int
    revision: int
    name: str
    styling: str
    image_prompt: str
    is_default: bool
    sort: int


class CharacterOut(BaseModel):
    id: int
    series_id: int
    revision: int
    name: str
    role: str
    description: str
    appearance: str
    sort: int
    novel_character_id: int | None
    novel_character_name: str | None
    looks: list[LookOut]


class SceneOut(BaseModel):
    id: int
    series_id: int
    revision: int
    location: str
    time_of_day: str
    description: str
    lighting: str
    image_prompt: str
    novel_world_setting_id: int | None
    novel_world_setting_title: str | None


class PropOut(BaseModel):
    id: int
    series_id: int
    revision: int
    name: str
    category: str
    description: str
    image_prompt: str


class ImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_type: Literal["look"]
    owner_id: int
    source: Literal["generated", "uploaded", "novel"]
    status: Literal["pending", "ready", "failed"]
    media_object_id: str | None
    prompt: str
    reference_image_ids: list[int]
    error_message: str
    is_selected: bool
    task_id: int | None
    created_at: datetime
    updated_at: datetime


class LibraryLookOut(LookOut):
    selected_image: ImageOut | None
    image_count: int
    pending_image_count: int
    latest_image_error: str | None


class LibraryCharacterOut(CharacterOut):
    looks: list[LibraryLookOut]


class AssetsOut(BaseModel):
    characters: list[LibraryCharacterOut]
    scenes: list[SceneOut]
    props: list[PropOut]
