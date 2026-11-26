"""一致性检查：事实台账、问题与证据、检查请求／预览／概要。

事实（LedgerFact）与证据（IssueEvidence）同时是落库 JSON 的结构契约：备份导入按它们校验，
子任务「改稿影响追踪」只读同一份台账。偏移一律是 Chapter.content 的码点半开区间，由服务端定位，不信任模型。
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.schemas.story_timeline import MonthId, Positive

FactCategory = Literal["character_history", "character_state", "item", "timeline", "foreshadow"]
IssueSeverity = Literal["high", "medium", "low"]
IssueStatus = Literal["open", "resolved", "ignored"]
RunStatus = Literal["running", "succeeded", "failed", "cancelled", "interrupted"]
CheckMode = Literal["all", "range"]
# 运行记录的范围：除全书／起止章节外，还有「重新核查某个问题」（范围 = 该问题现存证据章节）
RunMode = Literal["all", "range", "issue"]
ItemState = Literal["pending", "extracted", "reused", "skipped_changed", "skipped_missing", "failed"]

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Offset = Annotated[int, Field(ge=0, strict=True)]
Revision = Annotated[int, Field(ge=1, strict=True)]
IssueNote = Annotated[str, StringConstraints(max_length=2000)]

SUBJECT_LIMIT = 100
STATEMENT_LIMIT = 500
TIME_HINT_LIMIT = 100
QUOTE_LIMIT = 500
FACT_ID_LIMIT = 32
MAX_FACTS_PER_CHAPTER = 200
MAX_EVIDENCE_PER_ISSUE = 20


class _Anchored(BaseModel):
    """引句及其在 Chapter.content 中的位置；无法逐字定位时 start / end 同为 null。"""

    quote: str = Field(max_length=QUOTE_LIMIT)
    start: Offset | None = None
    end: Offset | None = None

    @model_validator(mode="after")
    def _offsets(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("start 与 end 必须同时给出或同时为 null")
        if self.start is not None and self.end <= self.start:
            raise ValueError("end 必须大于 start")
        return self


class LedgerFact(_Anchored):
    """台账中的一条事实（consistency_ledgers.facts 的元素）。"""

    model_config = ConfigDict(extra="forbid")

    # 章内稳定序号字符串
    id: str = Field(min_length=1, max_length=FACT_ID_LIMIT)
    category: FactCategory
    # 模型给出的主体原名
    subject: str = Field(max_length=SUBJECT_LIMIT)
    # 本地归一化键（去空白、全半角与大小写归一），只用于分组
    subject_key: str = Field(max_length=SUBJECT_LIMIT)
    statement: str = Field(min_length=1, max_length=STATEMENT_LIMIT)
    time_hint: str = Field(default="", max_length=TIME_HINT_LIMIT)


class StoryTimeEvidence(BaseModel):
    """服务端保留的场景日期快照；月份使用书内稳定标识，月序是核查时的位置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scene_id: Annotated[int, Field(ge=1, strict=True)] | None = None
    source: Literal["scene", "chapter", "unmarked", "unknown"]
    label: str = Field(default="", max_length=512)
    year: Positive | None = None
    month_id: MonthId | None = None
    month_order: Annotated[int, Field(ge=1, le=128, strict=True)] | None = None
    day: Positive | None = None

    @model_validator(mode="after")
    def _date_fields(self):
        if self.source == "unknown" and self.scene_id is not None:
            raise ValueError("无法定位的证据不能指定场景")
        fields = (self.year, self.month_id, self.month_order, self.day)
        if self.source in ("scene", "chapter"):
            if any(value is None for value in fields) or not self.label.strip():
                raise ValueError("有日期的证据须保存完整日期、月序及显示名称")
        elif any(value is not None for value in fields) or self.label:
            raise ValueError("缺少日期的证据不能携带日期值")
        return self

    @property
    def sort_key(self) -> tuple[int, int, int] | None:
        if self.year is None or self.month_order is None or self.day is None:
            return None
        return self.year, self.month_order, self.day


class IssueEvidence(_Anchored):
    """问题的一处证据（consistency_issues.evidence 的元素）。chapter_id 为 null = 章节在导入的备份中已不存在。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Annotated[int, Field(ge=1, strict=True)] | None
    fact_id: str = Field(max_length=FACT_ID_LIMIT)
    # 记录证据时该章正文的指纹；与当前正文不同即位置失效
    text_hash: Sha256
    story_time: StoryTimeEvidence | None = None


class LedgerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    chapter_id: int
    source_hash: str
    text_hash: str
    facts: list[LedgerFact]
    model: str
    schema_version: int
    created_at: datetime
    updated_at: datetime


# ---------------- 检查请求、预览与运行 ----------------


class CheckScope(BaseModel):
    """检查范围：全书，或起止章节（按章号两端包含）。"""

    model_config = ConfigDict(extra="forbid")

    mode: CheckMode = "all"
    start_chapter_id: int | None = Field(default=None, gt=0)
    end_chapter_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _range(self):
        has_range = self.start_chapter_id is not None or self.end_chapter_id is not None
        if self.mode == "range" and (self.start_chapter_id is None or self.end_chapter_id is None):
            raise ValueError("按范围检查必须同时给出起止章节")
        if self.mode == "all" and has_range:
            raise ValueError("全书检查不能指定起止章节")
        return self


class CheckRequest(CheckScope):
    ai_config_id: int | None = Field(default=None, gt=0)


class RecheckRequest(BaseModel):
    """单个问题重新核查：范围固定为该问题现存证据章节，只能选择模型。"""

    model_config = ConfigDict(extra="forbid")

    ai_config_id: int | None = Field(default=None, gt=0)


class CheckPreview(BaseModel):
    # 范围内章数
    total: int
    # 需要重新抽取（将调用模型）的章数
    to_extract: int
    # 台账可复用的章数
    reusable: int
    # 空章数（不参与检查）
    empty: int
    warnings: list[str] = Field(default_factory=list)


class RunSkipped(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chapter_id: int
    state: ItemState
    error: str = ""
    # 读取时按 chapter_id 补当前章号与标题（不落库）；章节已删除时为 null
    chapter_no: int | None = None
    chapter_title: str | None = None


class RunStats(BaseModel):
    """consistency_runs.stats：缺失的计数按 0 读取。"""

    model_config = ConfigDict(extra="ignore")

    # 范围内参与检查的非空章数
    total: int = 0
    extracted: int = 0
    reused: int = 0
    skipped: int = 0
    failed: int = 0
    created: int = 0
    # 已存在且本次再次检出的问题数（含 reappeared）
    updated: int = 0
    reappeared: int = 0
    not_reproduced: int = 0
    stale_outside: int = 0
    # 比对批次数与其中失败的批次数（有失败批次时本次不标记「未复现」）
    batches: int = 0
    failed_batches: int = 0
    skipped_chapters: list[RunSkipped] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RunScope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: RunMode = "all"
    start_chapter_id: int | None = None
    end_chapter_id: int | None = None
    # mode = issue 时为被重新核查的问题
    issue_id: int | None = None
    chapter_ids: list[int] = Field(default_factory=list)


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int | None
    scope: RunScope
    status: RunStatus
    stats: RunStats
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class LedgerCoverage(BaseModel):
    # 台账与当前正文一致／已过期／尚未抽取的非空章数
    fresh: int = 0
    stale: int = 0
    missing: int = 0


class ConsistencySummary(BaseModel):
    last_run: RunOut | None
    counts_by_status: dict[IssueStatus, int]
    ledger_coverage: LedgerCoverage
    # 本作品进行中（排队或运行）的检查任务；task_key 只在任务属于当前用户时返回（订阅进度需要）
    running_task_id: int | None = None
    running_task_key: str | None = None


# ---------------- 问题 ----------------


class EvidenceOut(BaseModel):
    """读取时补上当前章号与标题；chapter_no 为 null = 章节已删除。"""

    chapter_id: int | None
    chapter_no: int | None
    chapter_title: str | None
    fact_id: str
    quote: str
    start: int | None
    end: int | None
    text_hash: str
    story_time: StoryTimeEvidence | None = None
    story_time_stale: bool = False
    # 章节已删除，或当前正文指纹与 text_hash 不同
    stale: bool


class IssueOut(BaseModel):
    id: int
    project_id: int
    category: FactCategory
    title: str
    description: str
    suggestion: str
    severity: IssueSeverity
    status: IssueStatus
    note: str
    status_changed_by: int | None
    status_changed_at: datetime | None
    revision: int
    evidence: list[EvidenceOut]
    reappeared: bool
    # 以下两项读取时计算，不落库
    needs_recheck: bool
    not_reproduced: bool
    created_at: datetime
    updated_at: datetime


class IssuePage(BaseModel):
    items: list[IssueOut]
    total: int
    page: int
    page_size: int


class IssueUpdate(BaseModel):
    """状态与备注的 CAS 写入：revision 不符 → 409 code=stale_revision。"""

    model_config = ConfigDict(extra="forbid")

    revision: Revision
    status: IssueStatus | None = None
    note: IssueNote | None = None

    @field_validator("status", "note", mode="before")
    @classmethod
    def _reject_null(cls, value: Any):
        if value is None:
            raise ValueError("状态和备注不能为 null；清空备注请传空字符串")
        return value

    @model_validator(mode="after")
    def _require_one(self):
        if not self.model_fields_set & {"status", "note"}:
            raise ValueError("至少提供 status 或 note 之一")
        return self


# ---------------- 修改影响 ----------------

ImpactChangeKind = Literal["added", "removed", "changed"]
ImpactItemStatus = Literal["pending", "reviewed", "dismissed"]
LedgerState = Literal["fresh", "stale", "missing"]
# 不能评估的原因：no_baseline = 没有改动前的台账（先运行一次一致性检查）；unchanged = 台账与当前正文一致且没有上一版；
# empty = 正文为空
AssessBlock = Literal["no_baseline", "unchanged", "empty"]
ChapterRef = Annotated[int, Field(ge=1, strict=True)]

MAX_IMPACT_CHANGES = 2 * MAX_FACTS_PER_CHAPTER
MAX_IMPACT_ITEMS = 10_000
# 每个受影响章节保存的证据条数上限；命中的事实总数另记 fact_count，不静默截断
MAX_IMPACT_EVIDENCE = 5
MAX_IMPACT_SUBJECTS = 50
MAX_IMPACT_REFS = 10_000


class ImpactChange(BaseModel):
    """一处剧情事实变化（consistency_impacts.changes 的元素）。before / after 是改动前 / 后的事实陈述。"""

    model_config = ConfigDict(extra="forbid")

    kind: ImpactChangeKind
    category: FactCategory
    subject: str = Field(max_length=SUBJECT_LIMIT)
    subject_key: str = Field(default="", max_length=SUBJECT_LIMIT)
    before: str = Field(default="", max_length=STATEMENT_LIMIT)
    after: str = Field(default="", max_length=STATEMENT_LIMIT)


class ImpactEvidence(_Anchored):
    """受影响章节中的一处相关原文；偏移是该章 Chapter.content 的码点半开区间，text_hash 为记录时的正文指纹。"""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(max_length=FACT_ID_LIMIT)
    text_hash: Sha256


class ImpactItem(BaseModel):
    """一个可能受影响的后续章节（consistency_impacts.items 的元素）。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: ChapterRef
    subjects: list[Annotated[str, StringConstraints(max_length=SUBJECT_LIMIT)]] = Field(max_length=MAX_IMPACT_SUBJECTS)
    evidence: list[ImpactEvidence] = Field(max_length=MAX_IMPACT_EVIDENCE)
    # 该章台账中命中变化主体的事实总数（evidence 只保存前几条）
    fact_count: int = Field(default=0, ge=0, strict=True)
    status: ImpactItemStatus = "pending"


class ImpactItemOut(ImpactItem):
    """读取时补当前章号与标题；chapter_no 为 null = 章节已删除。"""

    model_config = ConfigDict(extra="ignore")

    chapter_no: int | None
    chapter_title: str | None
    # 章节已删除，或当前正文指纹与证据记录时不同
    changed: bool


class ImpactChapterRef(BaseModel):
    chapter_id: int
    # 章节已删除时为 null
    chapter_no: int | None
    chapter_title: str | None


class AssessmentOut(BaseModel):
    """最近一次评估。assessed = false 表示只保存过第一级的确认、尚未评估。"""

    assessed: bool
    assessed_at: datetime | None
    # 评估后本章正文（或 AI 可见区块）又有修改
    outdated: bool
    # 已评估且未发现剧情事实变化
    no_change: bool
    changes: list[ImpactChange]
    items: list[ImpactItemOut]
    # 评估时台账缺失或过期、无法判断的后续章节
    uncovered: list[ImpactChapterRef]
    model: str


class StaleSummary(BaseModel):
    analysis_id: int
    analyzed_at: datetime


class StaleMemory(BaseModel):
    id: int
    kind: str
    content: str


class StaleProgression(BaseModel):
    """由本章旧分析写入、来源正文已变的设定变化。跳转用 subject_kind + 各主体 id；名称读取时补。"""

    id: int
    timeline_id: int
    subject_kind: str
    character_id: int | None
    related_character_id: int | None
    world_setting_id: int | None
    career_id: int | None
    organization_id: int | None
    subject_name: str
    field_key: str | None
    custom_field_id: int | None
    anchor_label: str
    # 作者已确认无需更新（不再计入过期数）
    acknowledged: bool


class ImpactDetail(BaseModel):
    chapter_id: int
    chapter_no: int
    chapter_title: str
    # 是否有过章节分析；没有时摘要不算过期
    analyzed: bool
    # 以下三项只列出已过期的派生内容
    stale_summary: StaleSummary | None
    stale_memories: list[StaleMemory]
    stale_progressions: list[StaleProgression]
    # 摘要 + 记忆 + 未确认的设定变化
    stale_count: int
    ledger_state: LedgerState
    can_assess: bool
    assess_block: AssessBlock | None
    assessment: AssessmentOut | None
    # 确认状态的 CAS；0 = 尚无记录（首次保存时传 0）
    revision: int
    # 本作品进行中的影响评估任务（任一章）；task_key 只给发起者本人
    running_task_id: int | None = None
    running_task_key: str | None = None
    running_chapter_id: int | None = None


class ImpactChapterSummary(BaseModel):
    chapter_id: int
    chapter_no: int
    chapter_title: str
    stale_summary: bool
    stale_memories: int
    # 未确认的过期设定变化数
    stale_progressions: int
    stale_count: int
    ledger_state: LedgerState
    assessed: bool
    assessed_at: datetime | None
    # 最近一次评估的任务 id（任务已被清理时为 null）；任务面板的跳转据此定位到章节
    task_id: int | None = None
    outdated: bool
    no_change: bool
    changes: int
    items: int
    pending_items: int
    # 被列章节在评估后又有修改（或已删除）的影响项数
    changed_items: int
    uncovered: int


class ImpactRunning(BaseModel):
    """本作品进行中的影响评估任务（任一章）；task_key 只给发起者本人。轮询用的轻量读取。"""

    running_task_id: int | None = None
    running_task_key: str | None = None
    running_chapter_id: int | None = None


class ImpactOverview(BaseModel):
    """有过期派生内容或有评估记录的章节，按章号排序。"""

    chapters: list[ImpactChapterSummary]
    running_task_id: int | None = None
    running_task_key: str | None = None
    running_chapter_id: int | None = None


class AssessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_config_id: int | None = Field(default=None, gt=0)


class ImpactItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: ChapterRef
    status: ImpactItemStatus


class ImpactUpdate(BaseModel):
    """确认状态的 CAS 写入：revision 不符 → 409 code=stale_revision。尚无记录时 revision 传 0。"""

    model_config = ConfigDict(extra="forbid")

    revision: Annotated[int, Field(ge=0, strict=True)]
    # 只改列出的影响项
    items: list[ImpactItemUpdate] | None = Field(default=None, max_length=MAX_IMPACT_ITEMS)
    # 本章当前仍过期的设定变化中「已确认无需更新」的 id：只替换这部分，此前确认过、现已不再过期的 id 由服务端保留
    acknowledged: list[ChapterRef] | None = Field(default=None, max_length=MAX_IMPACT_REFS)

    @field_validator("items", "acknowledged", mode="before")
    @classmethod
    def _reject_null(cls, value: Any):
        if value is None:
            raise ValueError("items 和 acknowledged 不能为 null")
        return value

    @model_validator(mode="after")
    def _require_one(self):
        if not self.model_fields_set & {"items", "acknowledged"}:
            raise ValueError("至少提供 items 或 acknowledged 之一")
        if self.items is not None and len({item.chapter_id for item in self.items}) != len(self.items):
            raise ValueError("items 中的章节不能重复")
        return self
