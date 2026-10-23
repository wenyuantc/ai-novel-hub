"""tasks + generation_logs.task_id

新建持久化后台任务表 tasks（数据库即队列），并给 generation_logs 增加可空的 task_id。
uq_tasks_active_resource 为部分唯一索引：同一 resource_key 同时只能有一个 queued / running 任务。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-23 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE_RESOURCE_WHERE = "status IN ('queued', 'running') AND resource_key IS NOT NULL"


def upgrade() -> None:
    op.create_table('tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=True),
    sa.Column('chapter_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('title', sa.String(length=200), server_default='', nullable=False),
    sa.Column('resource_key', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('input', sa.JSON(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.Column('output_text', sa.Text(), server_default='', nullable=False),
    sa.Column('progress', sa.Integer(), server_default='0', nullable=False),
    sa.Column('stage', sa.String(length=100), server_default='', nullable=False),
    sa.Column('error', sa.Text(), server_default='', nullable=False),
    sa.Column('step_cursor', sa.Integer(), server_default='0', nullable=False),
    sa.Column('retry_of_id', sa.Integer(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['chapter_id'], ['chapters.id'], name=op.f('fk_tasks_chapter_id_chapters'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_tasks_project_id_projects'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['retry_of_id'], ['tasks.id'], name=op.f('fk_tasks_retry_of_id_tasks'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_tasks_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tasks'))
    )
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tasks_chapter_id'), ['chapter_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_retry_of_id'), ['retry_of_id'], unique=False)
        batch_op.create_index('ix_tasks_status_id', ['status', 'id'], unique=False)
        batch_op.create_index('ix_tasks_user_id_id', ['user_id', 'id'], unique=False)
        batch_op.create_index(
            'uq_tasks_active_resource',
            ['resource_key'],
            unique=True,
            postgresql_where=sa.text(ACTIVE_RESOURCE_WHERE),
            sqlite_where=sa.text(ACTIVE_RESOURCE_WHERE),
        )

    with op.batch_alter_table('generation_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('task_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_generation_logs_task_id'), ['task_id'], unique=False)
        batch_op.create_foreign_key(
            batch_op.f('fk_generation_logs_task_id_tasks'), 'tasks', ['task_id'], ['id'], ondelete='SET NULL'
        )


def downgrade() -> None:
    with op.batch_alter_table('generation_logs', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_generation_logs_task_id_tasks'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_generation_logs_task_id'))
        batch_op.drop_column('task_id')

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_index('uq_tasks_active_resource')
        batch_op.drop_index('ix_tasks_user_id_id')
        batch_op.drop_index('ix_tasks_status_id')
        batch_op.drop_index(batch_op.f('ix_tasks_retry_of_id'))
        batch_op.drop_index(batch_op.f('ix_tasks_project_id'))
        batch_op.drop_index(batch_op.f('ix_tasks_chapter_id'))
    op.drop_table('tasks')
