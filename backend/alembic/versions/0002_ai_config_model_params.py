"""ai_configs: thinking_level, context_window_k, max_output_k

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ai_configs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('thinking_level', sa.String(length=16), server_default='off', nullable=False))
        batch_op.add_column(sa.Column('context_window_k', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('max_output_k', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('ai_configs', schema=None) as batch_op:
        batch_op.drop_column('max_output_k')
        batch_op.drop_column('context_window_k')
        batch_op.drop_column('thinking_level')
