"""ai_configs: model_type

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default 回填存量配置为文本模型，原有默认配置即文本默认
    with op.batch_alter_table('ai_configs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('model_type', sa.String(length=16), server_default='text', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('ai_configs', schema=None) as batch_op:
        batch_op.drop_column('model_type')
