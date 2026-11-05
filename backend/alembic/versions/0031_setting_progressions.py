"""设定时间线与旧档案的待校准基础快照。

旧历史仅作审计保留，不按章推断或重放。关系载体原位加标记，不重建 SQLite 父表。

Revision ID: 0031
Revises: 0030
"""

from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


SUBJECT_SHAPE = (
    "(subject_kind = 'character' AND character_id IS NOT NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NULL AND career_id IS NULL AND organization_id IS NULL) OR "
    "(subject_kind = 'world' AND character_id IS NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NOT NULL AND career_id IS NULL AND organization_id IS NULL) OR "
    "(subject_kind = 'relationship' AND character_id IS NOT NULL AND related_character_id IS NOT NULL "
    "AND character_id < related_character_id AND world_setting_id IS NULL "
    "AND career_id IS NULL AND organization_id IS NULL) OR "
    "(subject_kind = 'career' AND character_id IS NOT NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NULL AND career_id IS NOT NULL AND organization_id IS NULL) OR "
    "(subject_kind = 'membership' AND character_id IS NOT NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NULL AND career_id IS NULL AND organization_id IS NOT NULL)"
)
CHARACTER_FIELDS = (
    "profile", "appearance", "personality", "background", "current_state", "life_status", "age", "gender"
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _check_legacy_references(conn: sa.Connection) -> None:
    """在 SQLite DDL 前检查旧引用，避免异常后留下半建表的不可重试迁移。"""
    invalid_queries = {
        "角色": """
            SELECT c.id FROM characters c LEFT JOIN projects p ON p.id = c.project_id
            WHERE p.id IS NULL LIMIT 1
        """,
        "世界观": """
            SELECT w.id FROM world_settings w LEFT JOIN projects p ON p.id = w.project_id
            WHERE p.id IS NULL LIMIT 1
        """,
        "人物关系": """
            SELECT r.id FROM relationships r
            LEFT JOIN projects p ON p.id = r.project_id
            LEFT JOIN characters a ON a.id = r.a_id
            LEFT JOIN characters b ON b.id = r.b_id
            LEFT JOIN relationship_types t ON t.id = r.type_id
            WHERE p.id IS NULL OR a.id IS NULL OR b.id IS NULL OR t.id IS NULL
               OR a.project_id <> r.project_id OR b.project_id <> r.project_id
               OR (t.project_id IS NOT NULL AND t.project_id <> r.project_id)
               OR r.pair_low <> CASE WHEN r.a_id < r.b_id THEN r.a_id ELSE r.b_id END
               OR r.pair_high <> CASE WHEN r.a_id > r.b_id THEN r.a_id ELSE r.b_id END
            LIMIT 1
        """,
        "职业挂接": """
            SELECT x.id FROM character_careers x
            LEFT JOIN characters c ON c.id = x.character_id
            LEFT JOIN careers d ON d.id = x.career_id
            LEFT JOIN career_stages s ON s.id = x.stage_id
            WHERE c.id IS NULL OR d.id IS NULL OR s.id IS NULL
               OR c.project_id <> d.project_id OR s.career_id <> d.id
            LIMIT 1
        """,
        "组织成员": """
            SELECT x.id FROM organization_members x
            LEFT JOIN characters c ON c.id = x.character_id
            LEFT JOIN organizations o ON o.id = x.organization_id
            WHERE c.id IS NULL OR o.id IS NULL OR c.project_id <> o.project_id
            LIMIT 1
        """,
    }
    for label, query in invalid_queries.items():
        invalid_id = conn.execute(sa.text(query)).scalar_one_or_none()
        if invalid_id is not None:
            raise RuntimeError(f"旧{label} #{invalid_id} 的关联无效或跨作品，请先修复后重试迁移")


def _timeline_table() -> sa.TableClause:
    return sa.table(
        "setting_timelines",
        sa.column("project_id", sa.Integer),
        sa.column("subject_kind", sa.String),
        sa.column("character_id", sa.Integer),
        sa.column("related_character_id", sa.Integer),
        sa.column("world_setting_id", sa.Integer),
        sa.column("career_id", sa.Integer),
        sa.column("organization_id", sa.Integer),
        sa.column("baseline", sa.JSON),
        sa.column("baseline_status", sa.String),
        sa.column("legacy_compat_allowed", sa.Boolean),
        sa.column("revision", sa.Integer),
    )


def _backfill(conn: sa.Connection) -> None:
    """只复制各主体的当前值，分批写入，不解析旧关系／职业／分析日志。"""
    timeline = _timeline_table()

    def character(row: sa.RowMapping) -> dict[str, Any]:
        return {
            "project_id": row["project_id"], "subject_kind": "character", "character_id": row["id"],
            "baseline": {field: row[field] for field in CHARACTER_FIELDS},
        }

    def world(row: sa.RowMapping) -> dict[str, Any]:
        return {
            "project_id": row["project_id"], "subject_kind": "world", "world_setting_id": row["id"],
            "baseline": {"content": row["content"]},
        }

    def relationship(row: sa.RowMapping) -> dict[str, Any]:
        return {
            "project_id": row["project_id"], "subject_kind": "relationship",
            "character_id": row["pair_low"], "related_character_id": row["pair_high"],
            "baseline": {
                "exists": True,
                "identity": {"a_id": row["a_id"], "b_id": row["b_id"], "type_id": row["type_id"]},
                "intimacy": row["intimacy"], "status": row["status"], "description": row["description"],
            },
        }

    def career(row: sa.RowMapping) -> dict[str, Any]:
        return {
            "project_id": row["project_id"], "subject_kind": "career", "character_id": row["character_id"],
            "career_id": row["career_id"],
            "baseline": {"exists": True, "role": row["role"], "stage_id": row["stage_id"], "note": row["note"]},
        }

    def membership(row: sa.RowMapping) -> dict[str, Any]:
        return {
            "project_id": row["project_id"], "subject_kind": "membership", "character_id": row["character_id"],
            "organization_id": row["organization_id"],
            "baseline": {"exists": True, "position": row["position"], "status": row["status"], "note": row["note"]},
        }

    queries: tuple[tuple[str, Callable[[sa.RowMapping], dict[str, Any]]], ...] = (
        ("SELECT id, project_id, " + ", ".join(CHARACTER_FIELDS) + " FROM characters ORDER BY id", character),
        ("SELECT id, project_id, content FROM world_settings ORDER BY id", world),
        ("SELECT project_id, a_id, b_id, pair_low, pair_high, type_id, intimacy, status, description "
         "FROM relationships ORDER BY id", relationship),
        ("SELECT c.project_id, x.character_id, x.career_id, x.role, x.stage_id, x.note "
         "FROM character_careers x JOIN characters c ON c.id = x.character_id ORDER BY x.id", career),
        ("SELECT c.project_id, x.character_id, x.organization_id, x.position, x.status, x.note "
         "FROM organization_members x JOIN characters c ON c.id = x.character_id ORDER BY x.id", membership),
    )
    for query, convert in queries:
        result = conn.execute(sa.text(query)).mappings()
        while batch := result.fetchmany(500):
            entries = []
            for row in batch:
                entry = convert(row)
                entry.update({"baseline_status": "legacy_unverified", "legacy_compat_allowed": True, "revision": 1})
                entries.append(entry)
            # 角色、地点、关系等批次的非空外键不同，不把不同形状合并到一次 executemany。
            conn.execute(timeline.insert(), entries)


def _partial_unique(name: str, table: str, columns: list[str], condition: str) -> None:
    where = sa.text(condition)
    op.create_index(name, table, columns, unique=True, sqlite_where=where, postgresql_where=where)


def upgrade() -> None:
    conn = op.get_bind()
    _check_legacy_references(conn)
    # 不用 batch 重建 relationships：它是 relationship_history 的父表，重建可能级联删除旧审计。
    op.add_column("relationships", sa.Column("is_present", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.create_table(
        "setting_timelines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject_kind", sa.String(16), nullable=False),
        sa.Column("character_id", sa.Integer(), sa.ForeignKey("characters.id", ondelete="CASCADE")),
        sa.Column("related_character_id", sa.Integer(), sa.ForeignKey("characters.id", ondelete="CASCADE")),
        sa.Column("world_setting_id", sa.Integer(), sa.ForeignKey("world_settings.id", ondelete="CASCADE")),
        sa.Column("career_id", sa.Integer(), sa.ForeignKey("careers.id", ondelete="CASCADE")),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("baseline", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("baseline_status", sa.String(24), server_default="confirmed", nullable=False),
        sa.Column("legacy_compat_allowed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
        sa.CheckConstraint(SUBJECT_SHAPE, name=op.f("ck_setting_timelines_subject_shape")),
        sa.CheckConstraint(
            "baseline_status IN ('confirmed', 'legacy_unverified')", name=op.f("ck_setting_timelines_baseline_status")
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_setting_timelines_positive_revision")),
    )
    for column in (
        "project_id", "character_id", "related_character_id", "world_setting_id", "career_id", "organization_id"
    ):
        op.create_index(f"ix_setting_timelines_{column}", "setting_timelines", [column])
    for kind, columns in (
        ("character", ["character_id"]),
        ("world", ["world_setting_id"]),
        ("relationship", ["character_id", "related_character_id"]),
        ("career", ["character_id", "career_id"]),
        ("membership", ["character_id", "organization_id"]),
    ):
        _partial_unique(
            f"uq_setting_timelines_{kind}", "setting_timelines", ["project_id", *columns], f"subject_kind = '{kind}'"
        )

    op.create_table(
        "setting_text_fields",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "timeline_id", sa.Integer(), sa.ForeignKey("setting_timelines.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("base_value", sa.Text(), server_default="", nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("timeline_id", "label", name=op.f("uq_setting_text_fields_timeline_id_label")),
        sa.CheckConstraint("length(trim(label)) > 0", name=op.f("ck_setting_text_fields_label_nonempty")),
    )
    op.create_index("ix_setting_text_fields_timeline_id", "setting_text_fields", ["timeline_id"])

    op.create_table(
        "setting_progressions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "timeline_id", sa.Integer(), sa.ForeignKey("setting_timelines.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("field_key", sa.String(100)),
        sa.Column("custom_field_id", sa.Integer(), sa.ForeignKey("setting_text_fields.id")),
        sa.Column("operation", sa.String(8), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("anchor_kind", sa.String(8), nullable=False),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id", ondelete="SET NULL")),
        sa.Column("chapter_no", sa.Integer()),
        sa.Column("anchor_label", sa.String(250), server_default="", nullable=False),
        sa.Column("boundary", sa.String(8), server_default="start", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source", sa.String(16), server_default="manual", nullable=False),
        sa.Column("protection", sa.String(12), server_default="manual", nullable=False),
        # 删章触发章锚和分析来源两条级联路径，PG 需要在事务末尾校验来源引用。
        sa.Column(
            "source_change_id", sa.Integer(),
            sa.ForeignKey("analysis_changes.id", ondelete="SET NULL", deferrable=True, initially="DEFERRED"),
        ),
        sa.Column("source_content_hash", sa.String(64)),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("superseded_by_id", sa.Integer(), sa.ForeignKey("setting_progressions.id", ondelete="SET NULL")),
        sa.Column("reverted_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.String(200), server_default="", nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "(field_key IS NOT NULL AND field_key <> '' AND custom_field_id IS NULL) OR "
            "(field_key IS NULL AND custom_field_id IS NOT NULL)", name=op.f("ck_setting_progressions_field_reference")
        ),
        sa.CheckConstraint(
            "(anchor_kind = 'chapter' AND chapter_no IS NULL) OR "
            "(anchor_kind = 'planned' AND chapter_id IS NULL AND chapter_no IS NOT NULL AND chapter_no >= 1)",
            name=op.f("ck_setting_progressions_anchor_shape"),
        ),
        sa.CheckConstraint("boundary IN ('start', 'end')", name=op.f("ck_setting_progressions_boundary")),
        sa.CheckConstraint("operation IN ('append', 'replace')", name=op.f("ck_setting_progressions_operation")),
        sa.CheckConstraint(
            "source IN ('manual', 'analysis', 'legacy_import')", name=op.f("ck_setting_progressions_source")
        ),
        sa.CheckConstraint(
            "protection IN ('automatic', 'confirmed', 'manual')", name=op.f("ck_setting_progressions_protection")
        ),
        sa.CheckConstraint("sort_order >= 0", name=op.f("ck_setting_progressions_nonnegative_order")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_setting_progressions_positive_revision")),
    )
    for column in ("timeline_id", "custom_field_id", "chapter_id", "source_change_id"):
        op.create_index(f"ix_setting_progressions_{column}", "setting_progressions", [column])
    for suffix, field in (("field", "field_key"), ("custom", "custom_field_id")):
        _partial_unique(
            f"uq_setting_progressions_analysis_{suffix}", "setting_progressions", ["source_change_id", field],
            f"source_change_id IS NOT NULL AND {field} IS NOT NULL",
        )
    _backfill(conn)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT id FROM relationships WHERE NOT is_present LIMIT 1")).first() is not None:
        raise RuntimeError("存在已隐藏的人物关系，旧版本无法保留其不可见状态和审计；请先备份并前滚处理")
    op.drop_table("setting_progressions")
    op.drop_table("setting_text_fields")
    op.drop_table("setting_timelines")
    # 原位删除列不重建关系父表；最新可见关系及其旧历史保留。
    op.drop_column("relationships", "is_present")
