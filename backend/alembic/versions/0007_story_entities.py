"""structured story entities

角色结构化字段、世界观分类、组织 / 成员、关系类型 / 关系 / 关系历史、章节出场角色。
只改结构，不调用 AI，不改动存量内容：
- world_settings.category 回填 other
- characters.always_in_context：role_type 为「主角」或 protagonist 的回填为 true
- 写入全局预置关系类型（project_id 为空）

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-23 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 冻结的预置类型（与 app.models.entities.PRESET_RELATIONSHIP_TYPES 保持一致，由迁移测试断言数量）
PRESET_RELATIONSHIP_TYPES = (
    ("family", "父亲", "子女"),
    ("family", "母亲", "子女"),
    ("family", "兄长", "弟妹"),
    ("family", "姐姐", "弟妹"),
    ("family", "配偶", "配偶"),
    ("family", "亲属", "亲属"),
    ("social", "师父", "徒弟"),
    ("social", "朋友", "朋友"),
    ("social", "知己", "知己"),
    ("social", "恋人", "恋人"),
    ("social", "同门", "同门"),
    ("social", "恩人", "受恩者"),
    ("hostile", "仇人", "仇人"),
    ("hostile", "宿敌", "宿敌"),
    ("hostile", "竞争对手", "竞争对手"),
    ("work", "上司", "下属"),
    ("work", "同僚", "同僚"),
    ("work", "合作者", "合作者"),
    ("work", "主人", "仆从"),
    ("other", "相识", "相识"),
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    # ---- characters / world_settings 新列 ----
    with op.batch_alter_table('characters', schema=None) as batch_op:
        batch_op.add_column(sa.Column('aliases', sa.JSON(), server_default='[]', nullable=False))
        batch_op.add_column(sa.Column('age', sa.String(length=50), server_default='', nullable=False))
        batch_op.add_column(sa.Column('gender', sa.String(length=20), server_default='', nullable=False))
        batch_op.add_column(sa.Column('appearance', sa.Text(), server_default='', nullable=False))
        batch_op.add_column(sa.Column('personality', sa.Text(), server_default='', nullable=False))
        batch_op.add_column(sa.Column('background', sa.Text(), server_default='', nullable=False))
        batch_op.add_column(sa.Column('current_state', sa.Text(), server_default='', nullable=False))
        batch_op.add_column(sa.Column('life_status', sa.String(length=16), server_default='alive', nullable=False))
        batch_op.add_column(sa.Column('always_in_context', sa.Boolean(), server_default=sa.false(), nullable=False))

    with op.batch_alter_table('world_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('category', sa.String(length=16), server_default='other', nullable=False))

    characters = sa.table('characters', sa.column('role_type', sa.String), sa.column('always_in_context', sa.Boolean))
    op.execute(
        characters.update()
        .where(sa.func.lower(sa.func.trim(characters.c.role_type)).in_(['主角', 'protagonist']))
        .values(always_in_context=sa.true())
    )
    world = sa.table('world_settings', sa.column('category', sa.String))
    op.execute(world.update().values(category='other'))

    # ---- 组织 ----
    op.create_table('organizations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('org_type', sa.String(length=50), server_default='', nullable=False),
    sa.Column('purpose', sa.Text(), server_default='', nullable=False),
    sa.Column('location', sa.String(length=200), server_default='', nullable=False),
    sa.Column('power_level', sa.Integer(), nullable=True),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('sort', sa.Integer(), nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['parent_id'], ['organizations.id'], name=op.f('fk_organizations_parent_id_organizations'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_organizations_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_organizations')),
    sa.UniqueConstraint('project_id', 'name', name=op.f('uq_organizations_project_id'))
    )
    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_organizations_parent_id'), ['parent_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_organizations_project_id'), ['project_id'], unique=False)

    op.create_table('organization_members',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('character_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.String(length=50), server_default='', nullable=False),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('note', sa.Text(), server_default='', nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['character_id'], ['characters.id'], name=op.f('fk_organization_members_character_id_characters'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_organization_members_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_organization_members')),
    sa.UniqueConstraint('organization_id', 'character_id', name=op.f('uq_organization_members_organization_id'))
    )
    with op.batch_alter_table('organization_members', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_organization_members_character_id'), ['character_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_organization_members_organization_id'), ['organization_id'], unique=False)

    # ---- 关系 ----
    rel_types = op.create_table('relationship_types',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=True),
    sa.Column('category', sa.String(length=16), server_default='other', nullable=False),
    sa.Column('role_a', sa.String(length=20), nullable=False),
    sa.Column('role_b', sa.String(length=20), nullable=False),
    sa.Column('sort', sa.Integer(), nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_relationship_types_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_relationship_types')),
    sa.UniqueConstraint('project_id', 'role_a', 'role_b', name=op.f('uq_relationship_types_project_id'))
    )
    with op.batch_alter_table('relationship_types', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_relationship_types_project_id'), ['project_id'], unique=False)
    op.bulk_insert(
        rel_types,
        [
            {"project_id": None, "category": c, "role_a": a, "role_b": b, "sort": i}
            for i, (c, a, b) in enumerate(PRESET_RELATIONSHIP_TYPES, start=1)
        ],
    )

    op.create_table('relationships',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('a_id', sa.Integer(), nullable=False),
    sa.Column('b_id', sa.Integer(), nullable=False),
    sa.Column('pair_low', sa.Integer(), nullable=False),
    sa.Column('pair_high', sa.Integer(), nullable=False),
    sa.Column('type_id', sa.Integer(), nullable=False),
    sa.Column('intimacy', sa.Integer(), server_default='0', nullable=False),
    sa.Column('status', sa.String(length=16), server_default='normal', nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    *_timestamps(),
    sa.CheckConstraint('a_id <> b_id', name=op.f('ck_relationships_distinct_pair')),
    sa.ForeignKeyConstraint(['a_id'], ['characters.id'], name=op.f('fk_relationships_a_id_characters'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['b_id'], ['characters.id'], name=op.f('fk_relationships_b_id_characters'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_relationships_project_id_projects'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['type_id'], ['relationship_types.id'], name=op.f('fk_relationships_type_id_relationship_types')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_relationships')),
    sa.UniqueConstraint('pair_low', 'pair_high', name=op.f('uq_relationships_pair_low'))
    )
    with op.batch_alter_table('relationships', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_relationships_a_id'), ['a_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_relationships_b_id'), ['b_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_relationships_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_relationships_type_id'), ['type_id'], unique=False)

    op.create_table('relationship_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('relationship_id', sa.Integer(), nullable=False),
    sa.Column('chapter_no', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('changes', sa.JSON(), nullable=False),
    sa.Column('note', sa.String(length=200), server_default='', nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['relationship_id'], ['relationships.id'], name=op.f('fk_relationship_history_relationship_id_relationships'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_relationship_history'))
    )
    with op.batch_alter_table('relationship_history', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_relationship_history_relationship_id'), ['relationship_id'], unique=False)

    # ---- 章节出场角色 ----
    op.create_table('chapter_characters',
    sa.Column('chapter_id', sa.Integer(), nullable=False),
    sa.Column('character_id', sa.Integer(), nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['chapter_id'], ['chapters.id'], name=op.f('fk_chapter_characters_chapter_id_chapters'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['character_id'], ['characters.id'], name=op.f('fk_chapter_characters_character_id_characters'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('chapter_id', 'character_id', name=op.f('pk_chapter_characters'))
    )
    with op.batch_alter_table('chapter_characters', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_chapter_characters_character_id'), ['character_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('chapter_characters', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chapter_characters_character_id'))
    op.drop_table('chapter_characters')

    with op.batch_alter_table('relationship_history', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_relationship_history_relationship_id'))
    op.drop_table('relationship_history')

    with op.batch_alter_table('relationships', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_relationships_type_id'))
        batch_op.drop_index(batch_op.f('ix_relationships_project_id'))
        batch_op.drop_index(batch_op.f('ix_relationships_b_id'))
        batch_op.drop_index(batch_op.f('ix_relationships_a_id'))
    op.drop_table('relationships')

    with op.batch_alter_table('relationship_types', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_relationship_types_project_id'))
    op.drop_table('relationship_types')

    with op.batch_alter_table('organization_members', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_organization_members_organization_id'))
        batch_op.drop_index(batch_op.f('ix_organization_members_character_id'))
    op.drop_table('organization_members')

    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_organizations_project_id'))
        batch_op.drop_index(batch_op.f('ix_organizations_parent_id'))
    op.drop_table('organizations')

    with op.batch_alter_table('world_settings', schema=None) as batch_op:
        batch_op.drop_column('category')

    with op.batch_alter_table('characters', schema=None) as batch_op:
        batch_op.drop_column('always_in_context')
        batch_op.drop_column('life_status')
        batch_op.drop_column('current_state')
        batch_op.drop_column('background')
        batch_op.drop_column('personality')
        batch_op.drop_column('appearance')
        batch_op.drop_column('gender')
        batch_op.drop_column('age')
        batch_op.drop_column('aliases')
