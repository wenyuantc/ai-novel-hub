"""章节正文批量修订与按场景保存的整批撤销记录。"""

import sqlalchemy as sa
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "revision_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("request_id", sa.String(36)),
        sa.Column("payload_hash", sa.String(64)),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("replacement", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("chapter_count", sa.Integer(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("undone_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "actor_id", "request_id", name="uq_revision_batches_request"),
        sa.CheckConstraint("status IN ('applied','undone')", name="status"),
        sa.CheckConstraint("chapter_count > 0 AND hit_count >= chapter_count", name="counts"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_revision_batches_project_id", "revision_batches", ["project_id"])
    op.create_table(
        "revision_batch_scenes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("revision_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id", ondelete="SET NULL")),
        sa.Column("scene_id", sa.Integer(), sa.ForeignKey("scenes.id", ondelete="SET NULL")),
        sa.Column("chapter_slot", sa.Integer(), nullable=False),
        sa.Column("chapter_no_at_apply", sa.Integer(), nullable=False),
        sa.Column("chapter_title", sa.String(200), nullable=False),
        sa.Column("applied_revision", sa.Integer()),
        sa.Column("before_document", sa.JSON(), nullable=False),
        sa.Column("start_offsets", sa.JSON(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("batch_id", "scene_id", name="uq_revision_batch_scenes_target"),
        sa.CheckConstraint("applied_revision >= 0", name="applied_revision_nonnegative"),
        sa.CheckConstraint("hit_count > 0", name="hit_count_positive"),
        sa.CheckConstraint("chapter_slot > 0", name="chapter_slot_positive"),
    )
    for column in ("chapter_id", "scene_id"):
        op.create_index(f"ix_revision_batch_scenes_{column}", "revision_batch_scenes", [column])


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE revision_batches, revision_batch_scenes IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("批量修订降级不支持当前数据库")
    if db.execute(sa.text("SELECT 1 FROM revision_batches LIMIT 1")).first() is not None:
        raise RuntimeError("仍有批量修订历史，请先备份并确认处置方式")
    op.drop_table("revision_batch_scenes")
    op.drop_table("revision_batches")
