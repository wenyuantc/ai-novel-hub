"""batch generate

批量顺序生成：tasks 加可空的 checkpoint（JSON，多步任务的结构化断点）；任务新增 paused（待审核）状态与
chapter.batch 类型——status / kind 列没有 CHECK 约束，只改代码常量。
downgrade 先把 paused 任务改为 cancelled（旧代码不认识 paused），再删除 checkpoint 列。

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-26 23:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("checkpoint", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute(
        "UPDATE tasks SET status = 'cancelled', stage = '已取消', error = '版本回退，待审核任务已取消', "
        "finished_at = CURRENT_TIMESTAMP WHERE status = 'paused'"
    )
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("checkpoint")
