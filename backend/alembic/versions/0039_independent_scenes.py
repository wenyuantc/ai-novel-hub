"""稳定章内场景、结构历史、本地规划及场景演变锚点。"""

from contextlib import contextmanager
import re

from alembic import op
import sqlalchemy as sa

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_MODES = ("pov", "tags", "character", "world", "organization", "career", "relationship")
_OLD_ANCHOR = (
    "(anchor_kind = 'chapter' AND chapter_no IS NULL) OR "
    "(anchor_kind = 'planned' AND chapter_id IS NULL AND chapter_no IS NOT NULL AND chapter_no >= 1)"
)
_NEW_ANCHOR = (
    "(anchor_kind = 'chapter' AND chapter_no IS NULL AND scene_id IS NULL) OR "
    "(anchor_kind = 'planned' AND chapter_id IS NULL AND scene_id IS NULL "
    "AND chapter_no IS NOT NULL AND chapter_no >= 1) OR "
    "(anchor_kind = 'scene' AND chapter_id IS NULL AND chapter_no IS NULL)"
)


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


@contextmanager
def _sqlite_rebuild():
    """重建 planning 父表前关闭 FK；保全所有旧子行及已删除身份的序列高水位。"""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return
    if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("旧资料存在无效外键，拒绝场景迁移")
    sequences = dict(bind.execute(sa.text("SELECT name, seq FROM sqlite_sequence")).all())
    tables = sa.inspect(bind).get_table_names()
    counts = {table: bind.exec_driver_sql(f'SELECT count(*) FROM "{table}"').scalar()
              for table in tables if table != "alembic_version"}
    enabled = bind.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    try:
        if enabled:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
            raise RuntimeError("不能安全关闭迁移连接外键，拒绝重建父表")
        yield
        remaining = set(sa.inspect(bind).get_table_names())
        for name, count in counts.items():
            if name in remaining and bind.exec_driver_sql(f'SELECT count(*) FROM "{name}"').scalar() != count:
                raise RuntimeError(f"场景迁移改变了旧表 {name} 的行数，拒绝继续")
        for name, sequence in sequences.items():
            if name not in remaining:
                continue
            result = bind.execute(sa.text(
                "UPDATE sqlite_sequence SET seq=CASE WHEN seq < :seq THEN :seq ELSE seq END WHERE name=:name"
            ), {"name": name, "seq": sequence})
            if result.rowcount == 0:
                bind.execute(sa.text("INSERT INTO sqlite_sequence(name,seq) VALUES (:name,:seq)"),
                             {"name": name, "seq": sequence})
        if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("场景迁移后外键校验失败")
    finally:
        if enabled:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def _alter_planning(*, add):
    bind = op.get_bind()
    kwargs = {"recreate": "always", "table_kwargs": {"sqlite_autoincrement": True}} if bind.dialect.name == "sqlite" else {}
    with op.batch_alter_table("planning_units", **kwargs) as batch:
        batch.drop_constraint(op.f("ck_planning_units_retired_detached"), type_="check")
        if add:
            batch.add_column(sa.Column("scene_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_planning_units_scene_id_scenes", "scenes", ["scene_id"], ["id"], ondelete="SET NULL")
            batch.create_unique_constraint("uq_planning_units_scene_id", ["scene_id"])
            batch.add_column(sa.Column("source_level", sa.String(8), nullable=False, server_default="chapter"))
            batch.create_check_constraint("source_level", "source_level IN ('chapter', 'scene')")
            batch.create_check_constraint("source_shape", "(source_level = 'chapter' AND scene_id IS NULL) OR "
                                          "(source_level = 'scene' AND chapter_id IS NULL AND plan_id IS NULL)")
            for field in _MODES:
                batch.add_column(sa.Column(f"{field}_mode", sa.String(8), nullable=False, server_default="inherit"))
                batch.create_check_constraint(f"{field}_mode", f"{field}_mode IN ('inherit', 'override')")
            batch.create_check_constraint("retired_detached", "NOT retired OR (chapter_id IS NULL AND plan_id IS NULL AND scene_id IS NULL)")
        else:
            batch.drop_constraint("fk_planning_units_scene_id_scenes", type_="foreignkey")
            batch.drop_constraint("uq_planning_units_scene_id", type_="unique")
            for name in ("source_level", "source_shape", *(f"{field}_mode" for field in _MODES)):
                batch.drop_constraint(op.f(f"ck_planning_units_{name}"), type_="check")
            for name in ("scene_id", "source_level", *(f"{field}_mode" for field in _MODES)):
                batch.drop_column(name)
            batch.create_check_constraint("retired_detached", "NOT retired OR (chapter_id IS NULL AND plan_id IS NULL)")
    with op.batch_alter_table("setting_progressions") as batch:
        batch.drop_constraint(op.f("ck_setting_progressions_anchor_shape"), type_="check")
        if add:
            batch.add_column(sa.Column("scene_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_setting_progressions_scene_id_scenes", "scenes", ["scene_id"], ["id"], ondelete="SET NULL")
            batch.create_index("ix_setting_progressions_scene_id", ["scene_id"])
        else:
            batch.drop_index("ix_setting_progressions_scene_id")
            batch.drop_constraint("fk_setting_progressions_scene_id_scenes", type_="foreignkey")
            batch.drop_column("scene_id")
        batch.create_check_constraint("anchor_shape", _NEW_ANCHOR if add else _OLD_ANCHOR)


def upgrade() -> None:
    with _sqlite_rebuild():
        # 原位 ADD COLUMN，不重建 chapters，避免多级子表 CASCADE／SET NULL。
        if op.get_bind().dialect.name == "sqlite":
            op.execute("ALTER TABLE chapters ADD COLUMN content_revision INTEGER NOT NULL DEFAULT 0 "
                       "CONSTRAINT ck_chapters_content_revision_nonnegative CHECK (content_revision >= 0)")
        else:
            op.add_column("chapters", sa.Column("content_revision", sa.Integer(), nullable=False, server_default="0"))
            op.create_check_constraint("content_revision_nonnegative", "chapters", "content_revision >= 0")
        op.add_column("chapter_versions", sa.Column("scene_snapshot", sa.JSON(), nullable=True))
        op.create_table(
            "scenes", sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
            sa.Column("title", sa.String(200), server_default="", nullable=False),
            sa.Column("content", sa.Text(), server_default="", nullable=False),
            sa.Column("word_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("summary", sa.Text(), server_default="", nullable=False),
            sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
            sa.Column("separator_before", sa.Text(), server_default="", nullable=False),
            sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
            *_timestamps(), sa.CheckConstraint("sort >= 0", name="sort_nonnegative"), sqlite_autoincrement=True,
        )
        op.create_index("ix_scenes_chapter_id", "scenes", ["chapter_id"])
        op.create_index("uq_scenes_active_sort", "scenes", ["chapter_id", "sort"], unique=True,
                        sqlite_where=sa.text("active"), postgresql_where=sa.text("active"))
        bind = op.get_bind()
        # 不推断边界／摘要／POV；含空章及回收站，旧章时间戳只读不改写。
        scene_table = sa.table("scenes", sa.column("chapter_id"), sa.column("content"), sa.column("word_count"),
                               sa.column("created_at", sa.DateTime(timezone=True)), sa.column("updated_at", sa.DateTime(timezone=True)))
        chapters = sa.table("chapters", sa.column("id"), sa.column("content"),
                            sa.column("created_at", sa.DateTime(timezone=True)), sa.column("updated_at", sa.DateTime(timezone=True)))
        last = 0
        while True:
            rows = bind.execute(sa.select(chapters).where(chapters.c.id > last).order_by(chapters.c.id).limit(500)).mappings().all()
            if not rows:
                break
            bind.execute(scene_table.insert(), [{"chapter_id": row["id"], "content": row["content"],
                                                "word_count": len(re.sub(r"\s+", "", row["content"])),
                                                "created_at": row["created_at"], "updated_at": row["updated_at"]} for row in rows])
            last = rows[-1]["id"]
        _alter_planning(add=True)
        op.create_table(
            "planning_unit_characters",
            sa.Column("unit_id", sa.Integer(), sa.ForeignKey("planning_units.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("character_id", sa.Integer(), sa.ForeignKey("characters.id", ondelete="CASCADE"), primary_key=True),
            *_timestamps(),
        )
        op.create_index("ix_planning_unit_characters_character_id", "planning_unit_characters", ["character_id"])


def _check_downgrade():
    bind = op.get_bind()
    checks = (
        "SELECT 1 FROM scenes WHERE NOT active OR sort <> 0 OR title <> '' OR summary <> '' OR separator_before <> '' LIMIT 1",
        "SELECT 1 FROM scenes GROUP BY chapter_id HAVING count(*) <> 1 LIMIT 1",
        "SELECT 1 FROM chapter_versions WHERE scene_snapshot IS NOT NULL AND scene_snapshot <> 'null' LIMIT 1",
        "SELECT 1 FROM planning_units WHERE source_level = 'scene' OR scene_id IS NOT NULL LIMIT 1",
        "SELECT 1 FROM setting_progressions WHERE anchor_kind = 'scene' OR scene_id IS NOT NULL LIMIT 1",
        "SELECT 1 FROM planning_unit_characters LIMIT 1",
    )
    # JSON 与 text 的相等运算在 PG 不通用，单独按 Python 解码检查。
    for index, sql in enumerate(checks):
        if index == 2:
            table = sa.table("chapter_versions", sa.column("scene_snapshot", sa.JSON()))
            if any(value is not None for value in bind.execute(sa.select(table.c.scene_snapshot)).scalars()):
                raise RuntimeError("已有场景结构历史，不能无损降级；请先完整备份并明确处理新资料")
        elif bind.execute(sa.text(sql)).first() is not None:
            raise RuntimeError("已有独立场景、规划或锚点，不能无损降级；请先完整备份并明确处理新资料")


def downgrade() -> None:
    _check_downgrade()
    with _sqlite_rebuild():
        op.drop_table("planning_unit_characters")
        _alter_planning(add=False)
        op.drop_table("scenes")
        op.drop_column("chapter_versions", "scene_snapshot")
        if op.get_bind().dialect.name == "sqlite":
            # SQLite 列级 CHECK 随列移除，不重建 chapters。
            op.drop_column("chapters", "content_revision")
        else:
            op.drop_constraint(op.f("ck_chapters_content_revision_nonnegative"), "chapters", type_="check")
            op.drop_column("chapters", "content_revision")
