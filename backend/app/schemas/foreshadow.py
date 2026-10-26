"""伏笔的请求 / 响应模型。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ForeshadowStatus = Literal["pending", "planted", "resolved", "partial", "abandoned"]
ForeshadowSort = Literal["planned_resolve", "importance"]

MAX_CHARACTERS = 50


class ForeshadowCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = ""
    plant_hint: str = ""
    reveal_hint: str = ""
    status: ForeshadowStatus = "pending"
    planned_plant_chapter: int | None = Field(default=None, ge=1)
    actual_plant_chapter: int | None = Field(default=None, ge=1)
    planned_resolve_chapter: int | None = Field(default=None, ge=1)
    actual_resolve_chapter: int | None = Field(default=None, ge=1)
    importance: int = Field(default=3, ge=1, le=5)
    include_in_context: bool = True
    note: str = ""
    character_ids: list[int] = Field(default_factory=list, max_length=MAX_CHARACTERS)


class ForeshadowUpdate(BaseModel):
    """省略 = 不变；章节字段显式 null = 清空；character_ids 给出时整体替换。

    chapter_no / history_note 只写进本次变化的历史记录（不是伏笔字段）。
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = None
    plant_hint: str | None = None
    reveal_hint: str | None = None
    status: ForeshadowStatus | None = None
    planned_plant_chapter: int | None = Field(default=None, ge=1)
    actual_plant_chapter: int | None = Field(default=None, ge=1)
    planned_resolve_chapter: int | None = Field(default=None, ge=1)
    actual_resolve_chapter: int | None = Field(default=None, ge=1)
    importance: int | None = Field(default=None, ge=1, le=5)
    include_in_context: bool | None = None
    note: str | None = None
    character_ids: list[int] | None = Field(default=None, max_length=MAX_CHARACTERS)
    chapter_no: int | None = Field(default=None, ge=1)
    history_note: str = Field(default="", max_length=200)


class PlantRequest(BaseModel):
    """标记已埋：chapter_no 为实际埋设章；省略时沿用已有的实际埋设章，都没有 → 400。"""

    chapter_no: int | None = Field(default=None, ge=1)


class ResolveRequest(BaseModel):
    """标记回收：chapter_no 为实际回收章；partial=true 标为部分回收。"""

    chapter_no: int | None = Field(default=None, ge=1)
    partial: bool = False


class ForeshadowOut(BaseModel):
    id: int
    project_id: int
    title: str
    content: str
    plant_hint: str
    reveal_hint: str
    status: ForeshadowStatus
    planned_plant_chapter: int | None
    actual_plant_chapter: int | None
    planned_resolve_chapter: int | None
    actual_resolve_chapter: int | None
    importance: int
    include_in_context: bool
    note: str
    source: str
    # 与 character_names 一一对应（按角色排序）
    character_ids: list[int]
    character_names: list[str]
    # 派生：已埋 / 部分回收且没有计划回收章
    is_long_term: bool
    # 派生：已埋 / 部分回收且计划回收章早于参照章时超期的章数，否则 0
    overdue_by: int
    created_at: datetime
    updated_at: datetime


class ForeshadowHistoryOut(BaseModel):
    id: int
    foreshadow_id: int
    source: str
    chapter_no: int | None
    changes: dict[str, Any]
    note: str
    created_at: datetime


class ForeshadowStats(BaseModel):
    pending: int = 0
    planted: int = 0
    resolved: int = 0
    partial: int = 0
    abandoned: int = 0
    overdue: int = 0
    long_term: int = 0
    # 已有正文的最大章节号（没有时为 0），超期按它判断
    current_chapter: int = 0


class ForeshadowReminders(BaseModel):
    """某一章的伏笔提醒（与章节上下文【伏笔提醒】同源，只含注入上下文开启的伏笔）。overdue_by 以该章为参照。"""

    chapter_no: int
    to_plant: list[ForeshadowOut] = []
    must_resolve: list[ForeshadowOut] = []
    overdue: list[ForeshadowOut] = []
    upcoming: list[ForeshadowOut] = []
    long_term: list[ForeshadowOut] = []


class ForeshadowOverrides(BaseModel):
    """确认分析识别的新伏笔时可修改的字段（省略 = 用分析给出的值）。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = None
    plant_hint: str | None = None
    reveal_hint: str | None = None
    planned_resolve_chapter: int | None = Field(default=None, ge=1)
    importance: int | None = Field(default=None, ge=1, le=5)
    character_ids: list[int] | None = Field(default=None, max_length=MAX_CHARACTERS)


class ConfirmChangeRequest(BaseModel):
    """确认回写条目的可选请求体；overrides 只对 foreshadow_new 生效，其他类型忽略。"""

    overrides: ForeshadowOverrides | None = None
