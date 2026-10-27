"""reading progress

阅读器：projects 新增 reading_progress（JSON，默认 {}，结构 {chapter_id, chapter_no, ratio, updated_at}）。
只加一列、不回填；downgrade 删除该列。

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-27 18:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: Union[str, Sequence[str], None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reading_progress", sa.JSON(), server_default="{}", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("reading_progress")
