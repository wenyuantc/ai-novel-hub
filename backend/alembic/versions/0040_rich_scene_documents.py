"""场景富格式、区块、作者标记和待核对的 AI 提案。

旧场景不回填结构行，也不重写 Scene.content / Chapter.content / 时间戳。
"""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "scene_documents",
        sa.Column("scene_id", sa.Integer(), sa.ForeignKey("scenes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("formatting", sa.JSON(), server_default="{}", nullable=False),
        *_timestamps(),
    )
    op.create_table(
        "scene_sections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scene_id", sa.Integer(), sa.ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("logical_key", sa.String(36), nullable=False),
        sa.Column("origin_section_id", sa.Integer(), sa.ForeignKey("scene_sections.id", ondelete="SET NULL")),
        sa.Column("anchor", sa.Integer(), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("formatting", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("include_in_ai", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("anchor >= 0", name="anchor_nonnegative"),
        sa.CheckConstraint("sort >= 0", name="sort_nonnegative"),
        sa.UniqueConstraint("scene_id", "logical_key", name="uq_scene_sections_scene_key"),
        sa.UniqueConstraint("scene_id", "id", name="uq_scene_sections_scene_id_id"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_scene_sections_scene_id", "scene_sections", ["scene_id"])
    op.create_index("uq_scene_sections_active_position", "scene_sections", ["scene_id", "anchor", "sort"],
                    unique=True, sqlite_where=sa.text("active"), postgresql_where=sa.text("active"))
    op.create_table(
        "scene_markers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scene_id", sa.Integer(), sa.ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("logical_key", sa.String(36), nullable=False),
        sa.Column("origin_marker_id", sa.Integer(), sa.ForeignKey("scene_markers.id", ondelete="SET NULL")),
        sa.Column("section_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("color", sa.String(8), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        sa.Column("last_start", sa.Integer(), nullable=True),
        sa.Column("excerpt", sa.Text(), server_default="", nullable=False),
        sa.Column("excerpt_truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("draft_excerpt", sa.Text(), nullable=True),
        sa.Column("draft_excerpt_truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("draft_last_start", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["scene_id", "section_id"], ["scene_sections.scene_id", "scene_sections.id"],
            name="fk_scene_markers_scene_section", ondelete="CASCADE", deferrable=True, initially="DEFERRED",
        ),
        sa.UniqueConstraint("scene_id", "logical_key", name="uq_scene_markers_scene_key"),
        sa.CheckConstraint("status IN ('active', 'stale', 'deleted')", name="status_valid"),
        sa.CheckConstraint("color IN ('yellow', 'green', 'blue', 'purple', 'red')", name="color_valid"),
        sa.CheckConstraint(
            "(status = 'active' AND start_offset IS NOT NULL AND end_offset IS NOT NULL "
            "AND start_offset >= 0 AND end_offset > start_offset) OR "
            "(status = 'stale' AND start_offset IS NULL AND end_offset IS NULL "
            "AND last_start IS NOT NULL AND last_start >= 0) OR status = 'deleted'",
            name="position_shape",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_scene_markers_scene_id", "scene_markers", ["scene_id"])
    op.create_index("ix_scene_markers_timeline", "scene_markers", ["scene_id", "status", "start_offset", "id"])
    op.create_index("ix_scene_markers_origin", "scene_markers", ["origin_marker_id"])
    op.create_table(
        "scene_review_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_key", sa.String(36), nullable=False),
        sa.Column("run_key", sa.String(36), nullable=False),
        sa.Column("token", sa.String(36), nullable=False, unique=True),
        sa.Column("content_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(12), server_default="pending", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("status IN ('pending', 'applied', 'discarded', 'stale')", name="status_valid"),
        sa.CheckConstraint("content_revision >= 0", name="revision_nonnegative"),
        sa.UniqueConstraint("task_key", "run_key", name="uq_scene_review_task_run"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_scene_review_proposals_task_id", "scene_review_proposals", ["task_id"])
    op.create_index("ix_scene_review_proposals_chapter_id", "scene_review_proposals", ["chapter_id"])
    op.create_table(
        "scene_review_payload_chunks",
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("scene_review_proposals.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
    )
    op.create_table(
        "scene_selection_receipts",
        sa.Column("request_key", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_scene_selection_receipts_user_id", "scene_selection_receipts", ["user_id"])
    op.create_table(
        "scene_review_analysis_receipts",
        sa.Column("run_key", sa.String(36), primary_key=True),
        sa.Column("task_key", sa.String(36), nullable=False),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("analysis_id", sa.Integer(), sa.ForeignKey("chapter_analyses.id", ondelete="SET NULL")),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name="status_valid"),
    )
    op.create_index("ix_scene_review_analysis_receipts_chapter_id", "scene_review_analysis_receipts", ["chapter_id"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("scene_documents", "scene_sections", "scene_markers", "scene_review_proposals",
                  "scene_review_payload_chunks", "scene_selection_receipts", "scene_review_analysis_receipts"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("已有富格式、区块、标记或待确认提案，不能无损降级；请先完整备份")
    versions = sa.table("chapter_versions", sa.column("scene_snapshot", sa.JSON()))
    for snapshot in bind.execute(sa.select(versions.c.scene_snapshot)).scalars():
        if isinstance(snapshot, dict) and snapshot.get("schema_version") != 1:
            raise RuntimeError("已有富文档历史，不能无损降级；请先完整备份")
    op.drop_table("scene_review_analysis_receipts")
    op.drop_table("scene_selection_receipts")
    op.drop_table("scene_review_payload_chunks")
    op.drop_table("scene_review_proposals")
    op.drop_table("scene_markers")
    op.drop_table("scene_sections")
    op.drop_table("scene_documents")
