"""跨章移动保留原场景身份与历史所属章节。"""

import sqlalchemy as sa
from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scene_moves",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scene_id", sa.Integer(), sa.ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_chapter_id", sa.Integer(), sa.ForeignKey("chapters.id", ondelete="SET NULL")),
        sa.Column("to_chapter_id", sa.Integer(), sa.ForeignKey("chapters.id", ondelete="SET NULL")),
        sa.Column("planning_unit_id", sa.Integer(), sa.ForeignKey("planning_units.id", ondelete="SET NULL")),
        sa.Column("first_move", sa.Boolean(), nullable=False),
        sa.Column("from_version_seq", sa.Integer()),
        sa.Column("to_version_seq", sa.Integer()),
        sa.CheckConstraint("from_chapter_id <> to_chapter_id", name="distinct_chapters"),
        sa.CheckConstraint("from_version_seq > 0", name="from_version_positive"),
        sa.CheckConstraint("to_version_seq > 0", name="to_version_positive"),
        sqlite_autoincrement=True,
    )
    for column in ("scene_id", "from_chapter_id", "to_chapter_id", "planning_unit_id"):
        op.create_index(f"ix_scene_moves_{column}", "scene_moves", [column])
    op.create_index(
        "uq_scene_moves_first", "scene_moves", ["scene_id"], unique=True,
        sqlite_where=sa.text("first_move"), postgresql_where=sa.text("first_move"),
    )


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE scene_moves IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("场景移动降级不支持当前数据库")
    if db.execute(sa.text("SELECT 1 FROM scene_moves LIMIT 1")).first() is not None:
        raise RuntimeError("仍有场景跨章移动记录，请先备份并确认处置方式")
    op.drop_table("scene_moves")
