"""careers

职业等级体系：careers / career_stages / character_careers / career_history 四张新表。
只新增表，不改动存量数据；downgrade 删除这四张表。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-23 23:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table('careers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.String(length=8), server_default='main', nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('requirements', sa.Text(), server_default='', nullable=False),
    sa.Column('abilities', sa.Text(), server_default='', nullable=False),
    sa.Column('rules', sa.Text(), server_default='', nullable=False),
    sa.Column('sort', sa.Integer(), nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_careers_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_careers')),
    sa.UniqueConstraint('project_id', 'name', name=op.f('uq_careers_project_id'))
    )
    with op.batch_alter_table('careers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_careers_project_id'), ['project_id'], unique=False)

    op.create_table('career_stages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('career_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['career_id'], ['careers.id'], name=op.f('fk_career_stages_career_id_careers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_career_stages'))
    )
    with op.batch_alter_table('career_stages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_career_stages_career_id'), ['career_id'], unique=False)

    op.create_table('character_careers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('character_id', sa.Integer(), nullable=False),
    sa.Column('career_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=8), nullable=False),
    sa.Column('stage_id', sa.Integer(), nullable=False),
    sa.Column('note', sa.String(length=200), server_default='', nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['career_id'], ['careers.id'], name=op.f('fk_character_careers_career_id_careers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['character_id'], ['characters.id'], name=op.f('fk_character_careers_character_id_characters'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['stage_id'], ['career_stages.id'], name=op.f('fk_character_careers_stage_id_career_stages')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_character_careers')),
    sa.UniqueConstraint('character_id', 'career_id', name=op.f('uq_character_careers_character_id'))
    )
    with op.batch_alter_table('character_careers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_character_careers_career_id'), ['career_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_character_careers_character_id'), ['character_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_character_careers_stage_id'), ['stage_id'], unique=False)

    op.create_table('career_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('character_id', sa.Integer(), nullable=False),
    sa.Column('career_id', sa.Integer(), nullable=True),
    sa.Column('career_name', sa.String(length=100), server_default='', nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('chapter_no', sa.Integer(), nullable=True),
    sa.Column('changes', sa.JSON(), nullable=False),
    sa.Column('note', sa.String(length=200), server_default='', nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['career_id'], ['careers.id'], name=op.f('fk_career_history_career_id_careers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['character_id'], ['characters.id'], name=op.f('fk_career_history_character_id_characters'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_career_history'))
    )
    with op.batch_alter_table('career_history', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_career_history_career_id'), ['career_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_career_history_character_id'), ['character_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('career_history', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_career_history_character_id'))
        batch_op.drop_index(batch_op.f('ix_career_history_career_id'))
    op.drop_table('career_history')

    with op.batch_alter_table('character_careers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_character_careers_stage_id'))
        batch_op.drop_index(batch_op.f('ix_character_careers_character_id'))
        batch_op.drop_index(batch_op.f('ix_character_careers_career_id'))
    op.drop_table('character_careers')

    with op.batch_alter_table('career_stages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_career_stages_career_id'))
    op.drop_table('career_stages')

    with op.batch_alter_table('careers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_careers_project_id'))
    op.drop_table('careers')
