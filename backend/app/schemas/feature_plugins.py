from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class PluginStateOut(BaseModel):
    id: str
    name: str
    description: str
    version: str
    enabled: bool
    revision: int
    updated_at: datetime | None


class PluginListOut(BaseModel):
    items: list[PluginStateOut]


class PluginUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    revision: int = Field(strict=True, ge=0)
