"""场景富格式、区块、作者标记和待确认提案。

场景正文仍由 scenes.content 承载；本模块不复制整章投影。
"""

from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SceneDocumentMeta(TimestampMixin, Base):
    __tablename__ = "scene_documents"

    scene_id: Mapped[int] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), primary_key=True)
    formatting: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}", nullable=False)


class SceneSection(TimestampMixin, Base):
    __tablename__ = "scene_sections"
    __table_args__ = (
        CheckConstraint("anchor >= 0", name="anchor_nonnegative"),
        CheckConstraint("sort >= 0", name="sort_nonnegative"),
        UniqueConstraint("scene_id", "logical_key", name="uq_scene_sections_scene_key"),
        UniqueConstraint("scene_id", "id", name="uq_scene_sections_scene_id_id"),
        Index(
            "uq_scene_sections_active_position", "scene_id", "anchor", "sort", unique=True,
            sqlite_where=text("active"), postgresql_where=text("active"),
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scene_id: Mapped[int] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    logical_key: Mapped[str] = mapped_column(String(36), nullable=False)
    origin_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("scene_sections.id", ondelete="SET NULL"), nullable=True,
    )
    anchor: Mapped[int] = mapped_column(Integer, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    formatting: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}", nullable=False)
    include_in_ai: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)


class SceneMarker(TimestampMixin, Base):
    __tablename__ = "scene_markers"
    __table_args__ = (
        # 复合外键阻止使用另一个场景的真实区块 ID；延迟检查允许拆并时同事务迁移身份。
        ForeignKeyConstraint(
            ["scene_id", "section_id"], ["scene_sections.scene_id", "scene_sections.id"],
            name="fk_scene_markers_scene_section", ondelete="CASCADE", deferrable=True, initially="DEFERRED",
        ),
        UniqueConstraint("scene_id", "logical_key", name="uq_scene_markers_scene_key"),
        CheckConstraint("status IN ('active', 'stale', 'deleted')", name="status_valid"),
        CheckConstraint("color IN ('yellow', 'green', 'blue', 'purple', 'red')", name="color_valid"),
        CheckConstraint(
            "(status = 'active' AND start_offset IS NOT NULL AND end_offset IS NOT NULL "
            "AND start_offset >= 0 AND end_offset > start_offset) OR "
            "(status = 'stale' AND start_offset IS NULL AND end_offset IS NULL "
            "AND last_start IS NOT NULL AND last_start >= 0) OR status = 'deleted'",
            name="position_shape",
        ),
        Index("ix_scene_markers_timeline", "scene_id", "status", "start_offset", "id"),
        Index("ix_scene_markers_origin", "origin_marker_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scene_id: Mapped[int] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    logical_key: Mapped[str] = mapped_column(String(36), nullable=False)
    # 跨场景迁移时保留可验证的来源墓碑；不能仅凭同 UUID／相同文字推断身份。
    origin_marker_id: Mapped[int | None] = mapped_column(
        ForeignKey("scene_markers.id", ondelete="SET NULL"), nullable=True,
    )
    section_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    color: Mapped[str] = mapped_column(String(8), nullable=False)
    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    excerpt: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    excerpt_truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    # 来自作者未保存事务的补充快照；不是服务端可独立验证的历史事实。
    draft_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_excerpt_truncated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False,
    )
    draft_last_start: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SceneReviewProposal(TimestampMixin, Base):
    __tablename__ = "scene_review_proposals"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'applied', 'discarded', 'stale')", name="status_valid"),
        CheckConstraint("content_revision >= 0", name="revision_nonnegative"),
        UniqueConstraint("task_key", "run_key", name="uq_scene_review_task_run"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    # 任务整数 ID 可复用；操作还须同时绑定创建该提案的统计 UUID 与独立执行轮次。
    task_key: Mapped[str] = mapped_column(String(36), nullable=False)
    run_key: Mapped[str] = mapped_column(String(36), nullable=False)
    token: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    content_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="pending", server_default="pending", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SceneReviewPayloadChunk(Base):
    """任务候选完整分块，避免单个 JSON 数据库字段容量限制。"""

    __tablename__ = "scene_review_payload_chunks"

    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("scene_review_proposals.id", ondelete="CASCADE"), primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)


class SceneSelectionReceipt(TimestampMixin, Base):
    """一次性用户确认的防重放凭据；仅保存随机身份，不保留已删除作品的正文或身份。"""

    __tablename__ = "scene_selection_receipts"

    request_key: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)


class SceneReviewAnalysisReceipt(TimestampMixin, Base):
    """批量提案采纳后的单次分析；与分析结果同事务完成，避免崩溃后重复调用。"""

    __tablename__ = "scene_review_analysis_receipts"
    __table_args__ = (CheckConstraint("status IN ('running', 'completed', 'failed')", name="status_valid"),)

    run_key: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_key: Mapped[str] = mapped_column(String(36), nullable=False)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True, nullable=False)
    analysis_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapter_analyses.id", ondelete="SET NULL"), nullable=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
