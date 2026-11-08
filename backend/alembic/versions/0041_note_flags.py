"""作品笔记收藏与固定标记。

旧笔记由 server_default 回填为未收藏、未固定，不改动 revision / updated_at。
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


# project_notes 是 sqlite_autoincrement 叶子表：SQLite 重建时须保留 AUTOINCREMENT 与已用 ID 高水位。
_TABLE_KWARGS = {"sqlite_autoincrement": True}


def _sqlite_sequence() -> int | None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return None
    return bind.execute(sa.text("SELECT seq FROM sqlite_sequence WHERE name='project_notes'")).scalar()


def _restore_sequence(seq: int | None) -> None:
    if seq is None:
        return
    bind = op.get_bind()
    current = bind.execute(sa.text("SELECT seq FROM sqlite_sequence WHERE name='project_notes'")).scalar()
    if current is None:
        bind.execute(sa.text("INSERT INTO sqlite_sequence(name, seq) VALUES('project_notes', :seq)"), {"seq": seq})
    elif current < seq:
        bind.execute(sa.text("UPDATE sqlite_sequence SET seq=:seq WHERE name='project_notes'"), {"seq": seq})


def upgrade() -> None:
    seq = _sqlite_sequence()
    with op.batch_alter_table("project_notes", table_kwargs=_TABLE_KWARGS) as batch:
        batch.add_column(sa.Column("is_favorite", sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.add_column(sa.Column("is_pinned", sa.Boolean(), server_default=sa.false(), nullable=False))
    _restore_sequence(seq)
    op.create_index("ix_project_notes_project_pinned", "project_notes", ["project_id", "is_pinned", "updated_at"])


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM project_notes WHERE is_favorite OR is_pinned LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有收藏或固定的作品笔记，拒绝有损降级；请先取消标记或导出备份")
    seq = _sqlite_sequence()
    op.drop_index("ix_project_notes_project_pinned", table_name="project_notes")
    with op.batch_alter_table("project_notes", table_kwargs=_TABLE_KWARGS) as batch:
        batch.drop_column("is_pinned")
        batch.drop_column("is_favorite")
    _restore_sequence(seq)
