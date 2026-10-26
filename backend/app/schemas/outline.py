"""大纲进阶（outline-pro）：细纲、大纲概览、追加 / 排序、快照、导入导出、续写 / 展开任务的请求与响应。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.chapter import ChapterBrief
from app.schemas.story import OutlineOut

OUTLINE_FILE_FORMAT = "ainovel.outline"
OUTLINE_FILE_VERSION = 1
# 导入数据大小上限（紧凑序列化后的 UTF-8 字节数）
MAX_OUTLINE_FILE_BYTES = 2 * 1024 * 1024
MAX_KEY_EVENTS = 20
MAX_KEY_EVENT_LEN = 200
DEFAULT_EST_WORDS = 3000
MIN_EST_WORDS = 300
MAX_EST_WORDS = 20000

SnapshotReason = Literal["wizard_replace", "import_replace", "reexpand", "manual", "restore"]
OutlineMode = Literal["replace", "append"]


def clean_key_events(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        v = v.strip()[:MAX_KEY_EVENT_LEN]
        if v and v not in out:
            out.append(v)
    return out[:MAX_KEY_EVENTS]


# ---------------- 细纲 ----------------


class PlanFields(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = ""
    key_events: list[str] = Field(default_factory=list, max_length=50)
    character_ids: list[int] = Field(default_factory=list, max_length=100)
    emotion: str = Field(default="", max_length=100)
    goal: str = ""
    est_words: int = Field(default=DEFAULT_EST_WORDS, ge=MIN_EST_WORDS, le=MAX_EST_WORDS)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("细纲标题不能为空")
        return v

    @field_validator("key_events")
    @classmethod
    def _events(cls, v: list[str]) -> list[str]:
        return clean_key_events(v)


class PlanIn(PlanFields):
    """整体同步中的一条：带 id = 更新已有细纲，无 id = 新建。"""

    id: int | None = None


class PlansSync(BaseModel):
    plans: list[PlanIn] = Field(max_length=50)


class PlanUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str | None = None
    key_events: list[str] | None = Field(default=None, max_length=50)
    character_ids: list[int] | None = Field(default=None, max_length=100)
    emotion: str | None = Field(default=None, max_length=100)
    goal: str | None = None
    est_words: int | None = Field(default=None, ge=MIN_EST_WORDS, le=MAX_EST_WORDS)

    @field_validator("key_events")
    @classmethod
    def _events(cls, v: list[str] | None) -> list[str] | None:
        return clean_key_events(v) if v is not None else None


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    outline_id: int
    seq: int
    title: str
    summary: str
    key_events: list[str]
    # 已过滤掉被删除的角色
    character_ids: list[int]
    emotion: str
    goal: str
    est_words: int
    chapter_id: int | None
    # 已建章时：章节号与是否已有正文（有正文的细纲不能删除）
    chapter_no: int | None = None
    has_content: bool = False
    created_at: datetime
    updated_at: datetime


class PlanBuildResult(BaseModel):
    # 本次新建或绑定的章节（按章节号）
    chapters: list[ChapterBrief]
    plans: list[PlanOut]


class OutlineDetail(OutlineOut):
    """大纲页一行：大纲 + 细纲 + 展开状态。"""

    # 「1 纲 1 章」关联的章节（未展开时）；已展开时为空，章节见 plans[].chapter_id
    chapter_id: int | None = None
    plans: list[PlanOut] = Field(default_factory=list)
    plan_count: int = 0
    built_count: int = 0


# ---------------- 追加 / 排序 ----------------


class OutlineAppendItem(BaseModel):
    volume_no: int = Field(default=1, ge=1)
    volume_title: str = Field(default="", max_length=200)
    title: str = Field(min_length=1, max_length=200)
    summary: str = ""


class OutlineAppendRequest(BaseModel):
    items: list[OutlineAppendItem] = Field(min_length=1, max_length=50)


class OutlineMoveRequest(BaseModel):
    direction: Literal["up", "down"]


# ---------------- 快照 ----------------


class SnapshotCreate(BaseModel):
    note: str = Field(default="", max_length=200)


class SnapshotOut(BaseModel):
    id: int
    reason: SnapshotReason
    note: str
    created_at: datetime
    outline_count: int
    plan_count: int
    link_count: int


class SnapshotOutlineBrief(BaseModel):
    volume_no: int
    volume_title: str
    chapter_no: int
    title: str
    plan_count: int


class SnapshotDetail(SnapshotOut):
    outlines: list[SnapshotOutlineBrief]
    # 恢复前的提示（如快照保存后章节号发生过平移）
    warnings: list[str]


class SnapshotRestoreResult(BaseModel):
    # 恢复前为当前状态自动保存的快照（可再恢复它以撤销本次恢复）
    backup: SnapshotOut
    outlines: int
    plans: int
    links_restored: int
    warnings: list[str]


# ---------------- 导入导出（ainovel.outline v1） ----------------


class OutlineFilePlan(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = ""
    key_events: list[str] = Field(default_factory=list, max_length=50)
    characters: list[str] = Field(default_factory=list, max_length=100)
    emotion: str = Field(default="", max_length=100)
    goal: str = ""
    est_words: int = DEFAULT_EST_WORDS


class OutlineFileOutline(BaseModel):
    chapter_no: int | None = None
    title: str = Field(min_length=1, max_length=200)
    summary: str = ""
    plans: list[OutlineFilePlan] = Field(default_factory=list, max_length=50)


class OutlineFileVolume(BaseModel):
    volume_no: int | None = None
    volume_title: str = Field(default="", max_length=200)
    outlines: list[OutlineFileOutline] = Field(default_factory=list, max_length=2000)


class OutlineFile(BaseModel):
    format: Literal["ainovel.outline"]
    version: int
    volumes: list[OutlineFileVolume] = Field(max_length=500)


class OutlineImportPreviewRequest(BaseModel):
    # 原样的 JSON（结构在 outline_transfer_service.parse_outline_file 中校验）
    data: Any


class OutlineImportRequest(BaseModel):
    data: Any
    mode: OutlineMode = "append"


class OutlineImportPreview(BaseModel):
    volumes: int
    outlines: int
    plans: int
    unresolved_characters: list[str]
    warnings: list[str]


class OutlineImportResult(BaseModel):
    mode: OutlineMode
    volumes: int
    outlines: int
    plans: int
    # replace 时替换前自动保存的快照
    snapshot_id: int | None = None
    unresolved_characters: list[str]
    warnings: list[str]
    items: list[OutlineOut]


# ---------------- AI 任务 ----------------


class NewVolume(BaseModel):
    title: str = Field(default="", max_length=200)


class OutlineContinueRequest(BaseModel):
    ai_config_id: int | None = None
    count: int = Field(default=5, ge=1, le=20)
    direction: str = Field(default="", max_length=2000)
    # 给出 = 开新卷（卷号 = 当前最后一卷 + 1）；为空 = 接在当前最后一卷之后
    new_volume: NewVolume | None = None


class OutlineExpandRequest(BaseModel):
    ai_config_id: int | None = None
    # 展开后的总章数（含保留的有正文细纲）
    count: int = Field(default=3, ge=2, le=10)
    instruction: str = Field(default="", max_length=2000)
