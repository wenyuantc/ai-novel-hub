from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProjectStatus = Literal["planning", "writing", "completed"]


# ---------------- 向导进度 ----------------


class WizardProgress(BaseModel):
    done: list[str] = Field(default_factory=list, max_length=20)
    skipped: list[str] = Field(default_factory=list, max_length=20)
    current: str | None = Field(default=None, max_length=32)


class WizardProgressUpdate(BaseModel):
    """给出的字段整体替换，未给的保持不变；current 显式 null = 清空。"""

    done: list[str] | None = Field(default=None, max_length=20)
    skipped: list[str] | None = Field(default=None, max_length=20)
    current: str | None = Field(default=None, max_length=32)


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    genre: str = Field(default="", max_length=100)
    premise: str = Field(default="", max_length=5000)
    style: str = Field(default="", max_length=5000)


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    genre: str | None = Field(default=None, max_length=100)
    premise: str | None = Field(default=None, max_length=5000)
    style: str | None = Field(default=None, max_length=5000)
    status: ProjectStatus | None = None
    auto_analyze: bool | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    genre: str
    premise: str
    style: str
    status: ProjectStatus
    auto_analyze: bool = True
    wizard_progress: WizardProgress = Field(default_factory=WizardProgress)
    created_at: datetime
    updated_at: datetime
    chapter_count: int = 0
    total_words: int = 0
