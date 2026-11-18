"""连载排期、定稿与发布历史。已有章节从无人工定稿开始。"""

import sqlalchemy as sa
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "serialization_goals",
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("daily_goal", sa.Integer()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("daily_goal > 0", name="positive_daily_goal"),
        sa.CheckConstraint("revision >= 0", name="revision_nonnegative"),
    )
    op.create_table(
        "serialization_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id", ondelete="SET NULL")),
        sa.Column("stage", sa.String(16), nullable=False, server_default="revision"),
        sa.Column("planned_date", sa.Date()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("chapter_id"),
        sa.CheckConstraint("stage IN ('revision','proofreading','final')", name="stage"),
        sa.CheckConstraint("revision >= 0", name="revision_nonnegative"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_serialization_entries_project_planned", "serialization_entries", ["project_id", "planned_date"])
    op.create_table(
        "serialization_editions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entry_id", sa.Integer(), sa.ForeignKey("serialization_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("chapter_no_at_confirmation", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("blocks", sa.JSON()),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("entry_id", "seq"),
        sa.CheckConstraint("seq > 0", name="seq_positive"),
        sa.CheckConstraint("chapter_no_at_confirmation > 0", name="chapter_no_positive"),
        sa.CheckConstraint("length(fingerprint) = 64", name="fingerprint_length"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "serialization_publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("edition_id", sa.Integer(), sa.ForeignKey("serialization_editions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("channel", sa.String(100), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("request_id", sa.String(36)),
        sa.Column("payload_hash", sa.String(64)),
        sa.UniqueConstraint("project_id", "actor_id", "request_id", name="uq_serialization_publications_request"),
        sa.CheckConstraint("length(trim(channel)) > 0", name="channel_nonempty"),
        sqlite_autoincrement=True,
    )
    for parent in ("edition", "project"):
        op.create_index(f"ix_serialization_publications_{parent}_time", "serialization_publications",
                        [f"{parent}_id", "published_at"])


def downgrade() -> None:
    db = op.get_bind()
    tables = ("serialization_publications", "serialization_editions", "serialization_entries", "serialization_goals")
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE serialization_goals, serialization_entries, serialization_editions, "
                   "serialization_publications IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("连载工作台降级不支持当前数据库")
    for table in tables:
        if db.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("仍有连载排期、定稿或发布记录，请先备份并确认处置方式")
    for table in tables:
        op.drop_table(table)
