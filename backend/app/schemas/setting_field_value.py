"""类型化字段的公共值；引用只公开领域身份，不公开内部句柄。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

FieldType = Literal["text", "line", "dropdown", "reference"]
EntityKind = Literal["character", "world", "organization", "career"]
SettingEntryType = Literal[
    "character", "world.place", "world.faction", "world.item", "world.rule", "world.history", "world.other",
    "organization", "career",
]
PositiveDbInt = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]


class EntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntityKind
    id: PositiveDbInt


class DropdownSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    option_id: PositiveDbInt


class TextFieldValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_type: Literal["text"] = "text"
    value: StrictStr


class LineFieldValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_type: Literal["line"] = "line"
    value: Annotated[
        str, Field(strict=True, max_length=500, pattern=r"^[^\r\n\v\f\x1c-\x1e\x85\u2028\u2029]*$"),
    ]


class DropdownFieldValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_type: Literal["dropdown"] = "dropdown"
    value: DropdownSelection | None


class ReferenceFieldValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_type: Literal["reference"] = "reference"
    value: EntityRef | None


TypedFieldValue = Annotated[
    TextFieldValue | LineFieldValue | DropdownFieldValue | ReferenceFieldValue, Field(discriminator="field_type"),
]
