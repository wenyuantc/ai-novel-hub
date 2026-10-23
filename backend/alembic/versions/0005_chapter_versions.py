"""chapter_versions + generation_logs

只新建表，不改现有表；存量章节没有版本也不影响使用（不做回填）。
generation_logs.chapter_id / ai_config_id 与 chapter_versions.generation_log_id 为 SET NULL，
chapter_versions.chapter_id 与 generation_logs.project_id 为 CASCADE。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('generation_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('chapter_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('ai_config_id', sa.Integer(), nullable=True),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=200), nullable=False),
    sa.Column('params', sa.JSON(), nullable=False),
    sa.Column('messages', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('output_chars', sa.Integer(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('prompt_tokens', sa.Integer(), nullable=True),
    sa.Column('completion_tokens', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['ai_config_id'], ['ai_configs.id'], name=op.f('fk_generation_logs_ai_config_id_ai_configs'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['chapter_id'], ['chapters.id'], name=op.f('fk_generation_logs_chapter_id_chapters'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_generation_logs_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_generation_logs'))
    )
    with op.batch_alter_table('generation_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_generation_logs_ai_config_id'), ['ai_config_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_generation_logs_chapter_id'), ['chapter_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_generation_logs_project_id'), ['project_id'], unique=False)

    op.create_table('chapter_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chapter_id', sa.Integer(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('word_count', sa.Integer(), nullable=False),
    sa.Column('starred', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('note', sa.String(length=200), server_default='', nullable=False),
    sa.Column('generation_log_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['chapter_id'], ['chapters.id'], name=op.f('fk_chapter_versions_chapter_id_chapters'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['generation_log_id'], ['generation_logs.id'], name=op.f('fk_chapter_versions_generation_log_id_generation_logs'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chapter_versions')),
    sa.UniqueConstraint('chapter_id', 'seq', name=op.f('uq_chapter_versions_chapter_id'))
    )
    with op.batch_alter_table('chapter_versions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_chapter_versions_chapter_id'), ['chapter_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_chapter_versions_generation_log_id'), ['generation_log_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('chapter_versions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chapter_versions_generation_log_id'))
        batch_op.drop_index(batch_op.f('ix_chapter_versions_chapter_id'))
    op.drop_table('chapter_versions')

    with op.batch_alter_table('generation_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_generation_logs_project_id'))
        batch_op.drop_index(batch_op.f('ix_generation_logs_chapter_id'))
        batch_op.drop_index(batch_op.f('ix_generation_logs_ai_config_id'))
    op.drop_table('generation_logs')
