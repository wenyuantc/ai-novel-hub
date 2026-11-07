from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.progression import NonnegativeDbInt
from app.schemas.setting_ai_visibility import MAX_MENTION_EXCLUSIONS, AiMode, BuiltinField, ExclusionPhrase
from app.schemas.setting_field_value import PositiveDbInt
from app.schemas.story import Aliases, WorldCategory

SeriesKind = Literal["character", "world"]
SeriesImpactAction = Literal["promote", "leave", "unuse", "move", "delete"]
SeriesName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SeriesCreate(_Strict):
    name: SeriesName


class SeriesRevision(_Strict):
    expected_revision: NonnegativeDbInt


class SeriesRename(SeriesRevision, SeriesCreate):
    pass


class SeriesBookMutation(SeriesRevision):
    project_id: PositiveDbInt


class SeriesLocalRef(_Strict):
    kind: SeriesKind
    id: PositiveDbInt


class _Patch(_Strict):
    @model_validator(mode="after")
    def no_explicit_null(self) -> Self:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("请省略未修改的字段，不能显式传 null")
        return self


class SeriesCharacterData(_Strict):
    name: str = Field(min_length=1, max_length=100)
    aliases: Aliases = Field(default_factory=list)
    profile: str = ""
    appearance: str = ""
    personality: str = ""
    background: str = ""
    gender: str = Field(default="", max_length=20)


class SeriesWorldData(_Strict):
    title: str = Field(min_length=1, max_length=200)
    aliases: Aliases = Field(default_factory=list)
    category: WorldCategory = "other"
    content: str = ""


class SeriesDataPatch(_Patch):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: Aliases | None = None
    profile: str | None = None
    appearance: str | None = None
    personality: str | None = None
    background: str | None = None
    gender: str | None = Field(default=None, max_length=20)
    category: WorldCategory | None = None
    content: str | None = None


class SeriesAiPatch(_Patch):
    ai_mode: AiMode | None = None
    mention_enabled: bool | None = Field(default=None, strict=True)
    mention_case_sensitive: bool | None = Field(default=None, strict=True)
    mention_exclusions: list[ExclusionPhrase] | None = Field(default=None, max_length=MAX_MENTION_EXCLUSIONS)
    ai_hidden_builtin_fields: list[BuiltinField] | None = Field(default=None, max_length=10)


class SeriesAiOut(_Strict):
    ai_mode: AiMode
    mention_enabled: bool
    mention_case_sensitive: bool
    mention_exclusions: list[str]
    ai_hidden_builtin_fields: list[str]


class SeriesEntryCreate(SeriesRevision):
    kind: SeriesKind
    data: SeriesDataPatch
    ai_settings: SeriesAiPatch | None = None


class SeriesEntryPatch(SeriesRevision, _Patch):
    data: SeriesDataPatch | None = None
    ai_settings: SeriesAiPatch | None = None


class SeriesPromote(SeriesBookMutation):
    ref: SeriesLocalRef
    source_stamp: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    ai_settings: SeriesAiPatch | None = None


class SeriesOut(BaseModel):
    id: int
    name: str
    revision: int
    book_count: int
    entry_count: int


class SeriesBookOut(BaseModel):
    id: int
    title: str
    recycled: bool


class SeriesDetail(SeriesOut):
    books: list[SeriesBookOut]


class SeriesUseOut(BaseModel):
    project_id: int
    ref: SeriesLocalRef


class _SeriesEntryOut(BaseModel):
    id: int
    ai_settings: SeriesAiOut
    uses: list[SeriesUseOut]


class SeriesCharacterOut(_SeriesEntryOut):
    kind: Literal["character"] = "character"
    data: SeriesCharacterData


class SeriesWorldOut(_SeriesEntryOut):
    kind: Literal["world"] = "world"
    data: SeriesWorldData


SeriesEntryOut = Annotated[SeriesCharacterOut | SeriesWorldOut, Field(discriminator="kind")]


class SeriesEntryPage(BaseModel):
    items: list[SeriesEntryOut]
    total: int
    revision: int


class SeriesEntryMutation(BaseModel):
    revision: int
    item: SeriesEntryOut | None = None
    ref: SeriesLocalRef | None = None


class SeriesImpactBlocker(BaseModel):
    project_id: int
    title: str
    recycled: bool
    reasons: list[str]


class SeriesImpactOut(BaseModel):
    revision: int
    blockers: list[SeriesImpactBlocker]
    affected_books: list[SeriesBookOut]
    source_stamp: str | None = None
    requires_ai_settings: bool = False
