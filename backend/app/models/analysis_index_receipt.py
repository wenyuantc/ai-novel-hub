"""分析到记忆索引的持久去重证据；不保存正文、向量或模型密钥。"""

from datetime import datetime
from typing import Any

from sqlalchemy import DDL, JSON, CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint, event, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.agent import _uuid_check
from app.models.base import Base


def _hash_check(column: str) -> str:
    remainder = column
    for char in "0123456789abcdef":
        remainder = f"replace({remainder}, '{char}', '')"
    return f"length({column}) = 64 AND {remainder} = ''"


def _int_check(column: str, *, allow_zero: bool = False) -> str:
    minimum = 0 if allow_zero else 1
    return f"CAST({column} AS INTEGER) = {column} AND {column} BETWEEN {minimum} AND 2147483647"


class AnalysisIndexReceipt(Base):
    """原分析轮次的唯一索引派生事实，不依赖父/子 Task 或 actor 行仍存在。"""

    __tablename__ = "analysis_index_receipts"
    __table_args__ = (
        CheckConstraint("source_kind IN ('single', 'batch')", name="source_kind"),
        *(CheckConstraint(_uuid_check(name, version4=name == "receipt_key"), name=f"{name}_uuid") for name in (
            "receipt_key", "source_key", "source_task_key", "parent_task_key", "parent_execution_key",
        )),
        CheckConstraint(f"child_task_key IS NULL OR ({_uuid_check('child_task_key')})", name="child_task_key_uuid"),
        CheckConstraint(
            "outcome = 'queued' AND child_task_key IS NOT NULL OR outcome = 'no_work' AND child_task_key IS NULL",
            name="outcome_child",
        ),
        CheckConstraint(
            "grant_invitation_id IS NULL AND grant_revision IS NULL OR "
            f"grant_invitation_id IS NOT NULL AND grant_revision IS NOT NULL AND ({_int_check('grant_revision')}) AND "
            f"({_uuid_check('grant_invitation_id')})", name="grant_identity",
        ),
        CheckConstraint(" AND ".join(
            f"({_int_check(name, allow_zero=name == 'actor_token_version')})" for name in (
                "actor_id", "actor_token_version", "project_id", "chapter_id", "analysis_id",
            )
        ), name="positive_identity"),
        CheckConstraint("json_valid(memory_refs) AND json_type(memory_refs) = 'array'",
                        name="memory_refs_array").ddl_if(dialect="sqlite"),
        CheckConstraint("json_typeof(memory_refs) = 'array'", name="memory_refs_array").ddl_if(dialect="postgresql"),
        CheckConstraint("outcome = 'no_work' OR json_array_length(memory_refs) > 0", name="queued_memory"),
        CheckConstraint("source_kind <> 'single' OR source_task_key = parent_task_key", name="single_source_task"),
        CheckConstraint(
            "receipt_key <> source_key AND receipt_key <> source_task_key AND receipt_key <> parent_task_key "
            "AND receipt_key <> parent_execution_key AND (child_task_key IS NULL OR "
            "(receipt_key <> child_task_key AND child_task_key <> source_key AND child_task_key <> source_task_key "
            "AND child_task_key <> parent_task_key AND child_task_key <> parent_execution_key))",
            name="distinct_identities",
        ),
        *(CheckConstraint(_hash_check(name), name=f"{name}_sha256") for name in (
            "analysis_hash", "content_hash", "input_hash", "selection_hash", "memory_hash",
        )),
        UniqueConstraint("source_kind", "source_key", name="uq_analysis_index_receipts_source"),
        UniqueConstraint("child_task_key"),
    )

    # 必须由调用方在提交前生成并保存，未知提交时只按本次两个随机身份查证。
    receipt_key: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    source_key: Mapped[str] = mapped_column(String(36), nullable=False)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    source_task_key: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_task_key: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_execution_key: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_id: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False,
    )
    chapter_id: Mapped[int] = mapped_column(Integer, nullable=False)
    analysis_id: Mapped[int] = mapped_column(Integer, nullable=False)
    grant_invitation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    grant_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 按 ref_id 排序保存 analysis/retrieval 标识、版本及 RetrievalChunk.text 的 SHA-256；不保存正文。
    # 消费方须严格验证结构、关联及摘要，不能仅凭数据库接受 JSON 推断证据有效。
    memory_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    memory_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    child_task_key: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# 原始来源和子任务 UUID 的事实不能被 UPDATE 改写；物理删除作品时允许 FK 级联清理。
event.listen(AnalysisIndexReceipt.__table__, "after_create", DDL(
    "CREATE TRIGGER analysis_index_receipts_immutable BEFORE UPDATE ON analysis_index_receipts "
    "BEGIN SELECT RAISE(ABORT, 'analysis index receipt is immutable'); END"
).execute_if(dialect="sqlite"))
event.listen(AnalysisIndexReceipt.__table__, "after_create", DDL(
    "CREATE OR REPLACE FUNCTION analysis_index_receipts_immutable_fn() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "BEGIN RAISE EXCEPTION 'analysis index receipt is immutable' USING ERRCODE = '23514'; END; $$"
).execute_if(dialect="postgresql"))
event.listen(AnalysisIndexReceipt.__table__, "after_create", DDL(
    "CREATE TRIGGER analysis_index_receipts_immutable BEFORE UPDATE ON analysis_index_receipts "
    "FOR EACH ROW EXECUTE FUNCTION analysis_index_receipts_immutable_fn()"
).execute_if(dialect="postgresql"))
event.listen(AnalysisIndexReceipt.__table__, "after_drop", DDL(
    "DROP FUNCTION IF EXISTS analysis_index_receipts_immutable_fn()"
).execute_if(dialect="postgresql"))
