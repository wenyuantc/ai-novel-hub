from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PenNameCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class PenNameUpdate(PenNameCreate):
    expected_revision: int = Field(ge=1, strict=True)


class PenNameOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    revision: int
    created_at: datetime
    updated_at: datetime


class PenNameBrief(BaseModel):
    id: int | None
    name: str


class ProjectPenNameUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pen_name_id: int | None = Field(ge=1, strict=True)


class ProjectPenNameOut(BaseModel):
    project_id: int
    pen_name: PenNameBrief | None
