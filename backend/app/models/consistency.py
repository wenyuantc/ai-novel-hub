"""一致性检查：逐章事实台账、检查运行记录与跨章问题。

各表都不存章节号：章号一律在读取时按 chapter_id 联表得到（因此不登记 CHAPTER_NO_REFS）。
台账、问题与修改影响记录随作品备份（project_transfer 注册表）；运行记录是任务运行态，不导出。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

CONSISTENCY_CATEGORIES = ("character_history", "character_state", "item", "timeline", "foreshadow")
CONSISTENCY_SEVERITIES = ("high", "medium", "low")
CONSISTENCY_ISSUE_STATUSES = ("open", "resolved", "ignored")
CONSISTENCY_RUN_STATUSES = ("running", "succeeded", "failed", "cancelled", "interrupted")
IMPACT_CHANGE_KINDS = ("added", "removed", "changed")
IMPACT_ITEM_STATUSES = ("pending", "reviewed", "dismissed")
# 事实台账的结构版本：与存值不符的台账视为过期，需要重新抽取。
LEDGER_SCHEMA_VERSION = 1


class ConsistencyLedger(TimestampMixin, Base):
    """一章的当前事实台账（每章至多一行）。source_hash 与当前分析指纹相同才可复用。"""

    __tablename__ = "consistency_ledgers"
    __table_args__ = (CheckConstraint("schema_version >= 1", name="schema_version_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), unique=True, nullable=False)
    # 抽取时的分析指纹（rich_document_ai.analysis_fingerprints，含开启 AI 可见的作者区块）
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 抽取时 Chapter.content 的 sha256（mentions.matcher.text_hash），证据锚点的指纹
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # [{id, category, subject, subject_key, statement, time_hint, quote, start, end}]
    facts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    model: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True)
    schema_version: Mapped[int] = mapped_column(
        Integer, default=LEDGER_SCHEMA_VERSION, server_default=str(LEDGER_SCHEMA_VERSION), nullable=False
    )
    # 上一版台账（0059）：覆盖且来源指纹不同时由 save_ledger 移入，供「修改影响」比较改动前后的事实。
    prev_facts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    prev_source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prev_text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ConsistencyRun(TimestampMixin, Base):
    """一次检查的运行记录：「最近一次检查」概要，以及问题首次／最近检出的引用。"""

    __tablename__ = "consistency_runs"
    # 问题按运行 id 的先后判断「本次未复现」，id 不得复用。
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    # {mode: all|range, start_chapter_id, end_chapter_id, chapter_ids}
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", server_default="running", nullable=False)
    # 抽取／复用／跳过／新增／再现／未复现计数与跳过章节列表
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConsistencyIssue(TimestampMixin, Base):
    """跨章潜在冲突。needs_recheck / not_reproduced 不落库，读取时计算。"""

    __tablename__ = "consistency_issues"
    __table_args__ = (
        UniqueConstraint("project_id", "identity_key"),
        CheckConstraint("revision >= 1", name="revision_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    suggestion: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", server_default="open", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    status_changed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 状态与备注的 CAS；检查更新证据不递增。
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    # 去重身份：sha256(category | subject_key | 排序后的证据 chapter_id 集合)
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # [{chapter_id, fact_id, quote, start, end, text_hash}]；start/end 为 Chapter.content 的码点偏移，无法定位为 null
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    first_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("consistency_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    last_seen_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("consistency_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    last_checked_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("consistency_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    reappeared: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)


class ConsistencyImpact(TimestampMixin, Base):
    """一章最近一次「修改影响」评估与确认状态（每章至多一行）。outdated / 影响项 changed 不落库，读取时计算。

    assessed_at 为空 = 尚未评估（只为保存第一级的 acknowledged 而建的行）。
    """

    __tablename__ = "consistency_impacts"
    __table_args__ = (CheckConstraint("revision >= 1", name="revision_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), unique=True, nullable=False)
    # 评估时该章的分析指纹与正文指纹；与当前不同即「评估后正文又有修改」
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 改动前台账的来源指纹
    base_source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # [{kind: added|removed|changed, category, subject, subject_key, before, after}]
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    # [{chapter_id, subjects[], evidence[{fact_id, quote, start, end, text_hash}], status}]
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    # 评估时台账缺失或过期、无法判断的后续章节 id
    uncovered: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    # 第一级中作者确认无需更新的设定变化（setting_progressions.id）
    acknowledged: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    # 确认状态的 CAS；重新评估同样递增（影响项整体替换）
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True)
    model: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    assessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
