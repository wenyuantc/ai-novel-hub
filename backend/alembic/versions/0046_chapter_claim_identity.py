"""章节逐次抢占身份；不重建含场景和版本外键的章节父表。

Revision ID: 0046
Revises: 0045
"""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

_CLAIM_CHECK = (
    "(generation_claim_key IS NULL AND generation_claim_task_key IS NULL "
    "AND generation_claim_prev_status IS NULL) OR "
    "(generation_claim_key IS NOT NULL AND generation_claim_task_key IS NOT NULL "
    "AND generation_claim_prev_status IS NOT NULL AND length(generation_claim_key) = 36 "
    "AND length(generation_claim_task_key) = 36 AND generation_claim_prev_status IN ('draft', 'done') "
    "AND status = 'generating')"
)


def upgrade() -> None:
    op.add_column("chapters", sa.Column("generation_claim_key", sa.String(36), nullable=True))
    op.add_column("chapters", sa.Column("generation_claim_task_key", sa.String(36), nullable=True))
    if op.get_bind().dialect.name == "sqlite":
        # SQLite 在最后一列原位增加跨列 CHECK，避免 batch 重建父表造成级联。
        op.execute(
            f"ALTER TABLE chapters ADD COLUMN generation_claim_prev_status VARCHAR(16) "
            f"CONSTRAINT ck_chapters_generation_claim_shape CHECK ({_CLAIM_CHECK})"
        )
    else:
        op.add_column("chapters", sa.Column("generation_claim_prev_status", sa.String(16), nullable=True))
        op.create_check_constraint(op.f("ck_chapters_generation_claim_shape"), "chapters", _CLAIM_CHECK)
    op.create_index("ix_chapters_generation_claim_task_key", "chapters", ["generation_claim_task_key"])


def downgrade() -> None:
    db = op.get_bind()
    if db.execute(sa.text(
        "SELECT 1 FROM chapters WHERE status = 'generating' OR generation_claim_key IS NOT NULL "
        "OR generation_claim_task_key IS NOT NULL OR generation_claim_prev_status IS NOT NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有生成中章节或章节占用身份，拒绝有损降级；请先安全排空并核对")
    op.drop_index("ix_chapters_generation_claim_task_key", table_name="chapters")
    if db.dialect.name != "sqlite":
        op.drop_constraint(op.f("ck_chapters_generation_claim_shape"), "chapters", type_="check")
    op.drop_column("chapters", "generation_claim_prev_status")
    op.drop_column("chapters", "generation_claim_task_key")
    op.drop_column("chapters", "generation_claim_key")
