"""0036 在完整字段资料图上原位添加整理标签与分类，不改写既有设定。"""

import asyncio
import json
import os
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy
from sqlalchemy.orm import Session

from app.models import Base, Career, Character, Organization, WorldSetting
from app.schemas.progression import StoryPosition
from app.services.setting_field_values import FieldSpec
from app.services.setting_progression_resolver import ProgressionEvent, resolve_progressions
from tests.schema_helpers import create_isolated_schema
from tests.test_setting_fields_migration import (
    _config,
    _foreign_keys_during_migrations,
    _historical_table,
    _schema_0036_model_contract,
    _schema_contract,
    _seed_connection,
)

ENTITY_TABLES = ("characters", "world_settings", "organizations", "careers")
NEW_TABLES = ("setting_categories", "setting_category_rules")
CHANGED_TABLES = ("projects", *ENTITY_TABLES, *NEW_TABLES)
APPLICABLE_TYPES = ["character", "world.place", "organization", "career"]


def _seed_fields(conn):
    """0035 的合法 typed 值与保留事件；不能用空库证明升级保留。"""
    conn.execute(text(
        "UPDATE projects SET tags=:tags, fields_revision=23, planning_revision=17 WHERE id=1"
    ), {"tags": json.dumps(["仙侠", "作品元数据"], ensure_ascii=False)})
    conn.execute(text(
        "INSERT INTO world_settings(id,project_id,title,content,category,sort) VALUES(2,1,'青云山','原貌','place',1)"
    ))
    conn.execute(text("UPDATE characters SET aliases=:aliases WHERE id=1"),
                 {"aliases": json.dumps(["旧人物别名😀"], ensure_ascii=False)})
    conn.execute(text("UPDATE world_settings SET aliases=:aliases WHERE id=2"),
                 {"aliases": json.dumps(["旧世界观别名"], ensure_ascii=False)})
    conn.execute(text(
        "INSERT INTO organizations(id,project_id,name,parent_id,sort) VALUES(3,1,'山门分支',1,1)"
    ))
    conn.execute(text(
        "INSERT INTO setting_timelines(project_id,subject_kind,organization_id,baseline) "
        "VALUES(1,'organization',3,'{}')"
    ))
    conn.execute(text("UPDATE planning_units SET pov_character_id=1 WHERE id=77"))
    conn.execute(text("INSERT INTO planning_unit_world(unit_id,world_id) VALUES(77,2)"))
    conn.execute(text("INSERT INTO planning_unit_organizations(unit_id,organization_id) VALUES(77,3)"))
    conn.execute(text("INSERT INTO planning_unit_careers(unit_id,career_id) VALUES(77,1)"))
    conn.execute(text(
        "INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
        "VALUES(5,1,20,'转折','原有章节正文','draft',6)"
    ))
    definitions = _historical_table(conn, "setting_field_definitions")
    options = Base.metadata.tables["setting_field_options"]
    handles = Base.metadata.tables["setting_reference_handles"]
    fields = _historical_table(conn, "setting_text_fields")
    events = Base.metadata.tables["setting_progressions"]
    for definition_id, field_type, label in (
        (11, "text", "经历"), (12, "line", "称号"), (13, "dropdown", "阵营"), (14, "reference", "驻地"),
    ):
        conn.execute(definitions.insert().values(
            id=definition_id, project_id=1, label=label, field_type=field_type,
            applicable_types=APPLICABLE_TYPES,
            reference_types=["world.place", "character"] if field_type == "reference" else [],
        ))
    conn.execute(options.insert(), [
        {"id": 21, "definition_id": 13, "label": "旧阵营", "archived": True},
        {"id": 22, "definition_id": 13, "label": "新阵营", "archived": False},
    ])
    conn.execute(handles.insert().values(id=31, project_id=1, kind="world", world_setting_id=2))
    conn.execute(handles.insert().values(id=32, project_id=1, kind="character", character_id=2))
    conn.execute(handles.insert().values(id=33, project_id=1, kind="organization", organization_id=1))
    conn.execute(handles.insert().values(id=34, project_id=1, kind="career", career_id=1))
    for field_id, definition_id, value in (
        (221, 11, "最初的经历😀"), (222, 12, "旧称号"), (223, 13, {"option_id": 21}),
        (224, 14, {"reference_handle_id": 31}),
    ):
        conn.execute(fields.insert().values(
            id=field_id, timeline_id=101, definition_id=definition_id, label=None, base_value=value,
        ))
    for event_id, field_id, value in (
        (401, 221, "后来追加"), (402, 222, "新称号"), (403, 223, {"option_id": 22}),
    ):
        conn.execute(events.insert().values(
            id=event_id, timeline_id=101, custom_field_id=field_id, operation="replace", value=value,
            anchor_kind="planned", chapter_no=20, boundary="end", note="保留原注释",
        ))
    conn.execute(events.insert().values(
        id=404, timeline_id=101, custom_field_id=224, operation="replace", value={"reference_handle_id": 32},
        anchor_kind="chapter", chapter_id=5, boundary="end", source="manual", protection="confirmed", revision=3,
    ))
    conn.execute(events.insert().values(
        id=405, timeline_id=101, custom_field_id=223, operation="replace", value=None,
        anchor_kind="planned", chapter_no=21, active=False,
    ))
    conn.execute(events.insert().values(
        id=406, timeline_id=101, custom_field_id=224, operation="replace", value=None,
        anchor_kind="chapter", chapter_id=None,
    ))
    if conn.dialect.name == "sqlite":
        conn.execute(fields.insert().values(id=9000, timeline_id=101, label="已删除的最高空绑定", base_value=""))
        conn.execute(fields.delete().where(fields.c.id == 9000))


def _sqlite_database(tmp_path):
    path = tmp_path / "categories.db"
    config = _config(path)
    command.upgrade(config, "0034")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        _seed_connection(conn)
    command.upgrade(config, "0035")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        _seed_fields(conn)
    return config, engine


def _snapshot(conn):
    """记录所有旧列；新加列不能掩盖旧资料、版本与时间戳的任何变化。"""
    inspector = inspect(conn)
    snapshots = {}
    for name in inspector.get_table_names():
        if name == "alembic_version":
            continue
        columns = [column["name"] for column in inspector.get_columns(name)]
        order = inspector.get_pk_constraint(name)["constrained_columns"]
        projection = ",".join(f'"{column}"' for column in columns)
        sort = ",".join(f'"{column}"' for column in order)
        rows = conn.execute(text(f'SELECT {projection} FROM "{name}" ORDER BY {sort}')).all()
        snapshots[name] = (columns, rows)
    return snapshots


def _assert_preserved(conn, before):
    for name, (columns, rows) in before.items():
        projection = ",".join(f'"{column}"' for column in columns)
        order = inspect(conn).get_pk_constraint(name)["constrained_columns"]
        sort = ",".join(f'"{column}"' for column in order)
        assert conn.execute(text(f'SELECT {projection} FROM "{name}" ORDER BY {sort}')).all() == rows, name


def _assert_sqlite_rebuild_integrity(conn, before_sequences, *, upgraded):
    sequences = dict(conn.exec_driver_sql("SELECT name,seq FROM sqlite_sequence").all())
    assert {name: sequences[name] for name in before_sequences} == before_sequences
    assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert not any(name.startswith("_alembic_tmp_") for name in inspect(conn).get_table_names())
    for name in ENTITY_TABLES:
        ddl = conn.scalar(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"), {"name": name})
        assert ("AUTOINCREMENT" in ddl) is upgraded
        if upgraded:
            assert sequences[name] >= conn.scalar(text(f"SELECT max(id) FROM {name}"))


def _record_migration_fk_states(monkeypatch, foreign_keys):
    if foreign_keys:
        _foreign_keys_during_migrations(monkeypatch)
    original = async_sqlalchemy.async_engine_from_config
    states = []

    def factory(*args, **kwargs):
        engine = original(*args, **kwargs)

        @event.listens_for(engine.sync_engine, "checkin")
        def record(connection, _record):
            if connection is not None:
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys")
                states.append(cursor.fetchone()[0])
                cursor.close()

        return engine

    monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", factory)
    return states


def _effective_values(conn):
    fields = Base.metadata.tables["setting_text_fields"]
    values = dict(conn.execute(select(fields.c.id, fields.c.base_value).where(fields.c.timeline_id == 101)).all())
    specs = {
        201: FieldSpec("text"), 203: FieldSpec("text"), 221: FieldSpec("text", definition_id=11),
        222: FieldSpec("line", definition_id=12),
        223: FieldSpec("dropdown", definition_id=13, option_ids=frozenset({21, 22})),
        224: FieldSpec("reference", definition_id=14, reference_handle_ids=frozenset({31, 32})),
    }
    events = []
    for row in conn.execute(select(Base.metadata.tables["setting_progressions"]).where(
        Base.metadata.tables["setting_progressions"].c.timeline_id == 101
    )).mappings():
        events.append(ProgressionEvent(**{
            key: row[key] for key in ProgressionEvent.__dataclass_fields__ if key in row
        }, resolved_chapter_no=20 if row["chapter_id"] == 5 else None))
    return {
        (chapter, boundary): resolve_progressions(
            "character", {}, values, events, StoryPosition(chapter_no=chapter, boundary=boundary), field_specs=specs,
        ).field_values
        for chapter, boundary in ((5, "start"), (20, "start"), (20, "end"), (21, "start"))
    }


@pytest.mark.parametrize("foreign_keys", [False, True])
def test_0036_preserves_every_0035_value_anchor_and_revision_then_empty_downgrade(tmp_path, monkeypatch, foreign_keys):
    config, engine = _sqlite_database(tmp_path)
    try:
        with engine.connect() as conn:
            before, effective = _snapshot(conn), _effective_values(conn)
            before_sequences = dict(conn.exec_driver_sql("SELECT name,seq FROM sqlite_sequence").all())
            before_schema = {name: _schema_contract(inspect(conn), name) for name in ENTITY_TABLES}
        assert effective[(5, "start")][223] == {"option_id": 21}
        assert effective[(20, "end")][224] == {"reference_handle_id": 32}
        states = _record_migration_fk_states(monkeypatch, foreign_keys)
        command.upgrade(config, "0036")
        assert states and set(states) == {int(foreign_keys)}
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            _assert_preserved(conn, before)
            _assert_sqlite_rebuild_integrity(conn, before_sequences, upgraded=True)
            assert _effective_values(conn) == effective
            for table in ENTITY_TABLES:
                assert conn.execute(text(f"SELECT DISTINCT setting_tags FROM {table}")).scalars().all() == ["[]"]
            assert conn.execute(text("SELECT categories_revision FROM projects ORDER BY id")).scalars().all() == [0, 0]
            assert conn.scalar(text("SELECT tags FROM projects WHERE id=1")) != "[]"
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            # 目录代次是并发元数据；清空新资料后并不妨碍无损退回旧结构。
            conn.execute(text("UPDATE projects SET categories_revision=9 WHERE id=1"))
        command.downgrade(config, "0035")
        assert set(states) == {int(foreign_keys)}
        with engine.connect() as conn:
            _assert_preserved(conn, before)
            _assert_sqlite_rebuild_integrity(conn, before_sequences, upgraded=False)
            assert {name: _schema_contract(inspect(conn), name) for name in ENTITY_TABLES} == before_schema
            assert _effective_values(conn) == effective
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert not set(NEW_TABLES) & set(inspect(engine).get_table_names())
        command.upgrade(config, "0036")
        assert set(states) == {int(foreign_keys)}
        with engine.connect() as conn:
            _assert_preserved(conn, before)
            _assert_sqlite_rebuild_integrity(conn, before_sequences, upgraded=True)
            assert _effective_values(conn) == effective
    finally:
        engine.dispose()


@pytest.mark.parametrize("foreign_keys", [False, True])
def test_0036_rejects_invalid_legacy_fk_before_rebuilding_any_table(tmp_path, monkeypatch, foreign_keys):
    config, engine = _sqlite_database(tmp_path)
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(text("UPDATE organization_members SET character_id=999999 WHERE id=1"))
            before = _snapshot(conn)
        states = _record_migration_fk_states(monkeypatch, foreign_keys)
        with pytest.raises(RuntimeError, match="旧资料存在无效外键"):
            command.upgrade(config, "0036")
        assert states and set(states) == {int(foreign_keys)}
        with engine.connect() as conn:
            _assert_preserved(conn, before)
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0035"
            assert not set(NEW_TABLES) & set(inspect(conn).get_table_names())
            assert not any(name.startswith("_alembic_tmp_") for name in inspect(conn).get_table_names())
    finally:
        engine.dispose()


def test_0036_schema_matches_full_current_orm_and_stable_identities(tmp_path):
    config, migrated = _sqlite_database(tmp_path)
    expected = create_engine("sqlite://")
    try:
        command.upgrade(config, "0036")
        Base.metadata.create_all(expected)
        for name in CHANGED_TABLES:
            assert _schema_contract(inspect(migrated), name) == _schema_0036_model_contract(
                inspect(expected), name,
            ), name
        for engine in (migrated, expected):
            with engine.connect() as conn:
                for name in (*NEW_TABLES, *ENTITY_TABLES):
                    ddl = conn.scalar(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
                                      {"name": name})
                    assert "AUTOINCREMENT" in ddl
        with migrated.begin() as conn:
            categories = Base.metadata.tables["setting_categories"]
            rules = Base.metadata.tables["setting_category_rules"]
            first = conn.scalar(categories.insert().values(
                project_id=1, name="旧分类", applicable_types=["character"], match_mode="all",
            ).returning(categories.c.id))
            conn.execute(categories.delete().where(categories.c.id == first))
            next_id = conn.scalar(categories.insert().values(
                project_id=1, name="新分类", applicable_types=["character"], match_mode="any",
            ).returning(categories.c.id))
            assert next_id > first
            first_rule = conn.scalar(rules.insert().values(category_id=next_id, kind="tag", tag="旧条件")
                                     .returning(rules.c.id))
            conn.execute(rules.delete().where(rules.c.id == first_rule))
            assert conn.scalar(rules.insert().values(category_id=next_id, kind="tag", tag="新条件")
                               .returning(rules.c.id)) > first_rule
    finally:
        migrated.dispose()
        expected.dispose()


@pytest.mark.parametrize("model,name_column", [(Character, "name"), (WorldSetting, "title"),
                                              (Organization, "name"), (Career, "name")])
def test_entity_tag_defaults_are_independent_per_row(tmp_path, model, name_column):
    config, engine = _sqlite_database(tmp_path)
    try:
        command.upgrade(config, "0037")
        with Session(engine) as db:
            first = model(project_id=1, **{name_column: "新增甲"})
            second = model(project_id=1, **{name_column: "新增乙"})
            db.add_all([first, second])
            db.flush()
            assert first.setting_tags == second.setting_tags == []
            first.setting_tags.append("只属于甲")
            assert second.setting_tags == []
            db.refresh(second)
            assert second.setting_tags == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("target", ["setting_field_definitions", "setting_field_options"])
@pytest.mark.parametrize("operation", ["delete", "invalid_reference"])
def test_0036_sqlite_rule_reference_requires_valid_targets_at_real_commit(tmp_path, target, operation):
    config, engine = _sqlite_database(tmp_path)
    try:
        command.upgrade(config, "0036")
        categories = Base.metadata.tables["setting_categories"]
        rules = Base.metadata.tables["setting_category_rules"]
        options = Base.metadata.tables["setting_field_options"]
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            definitions = _historical_table(conn, "setting_field_definitions")
            category_id = conn.scalar(categories.insert().values(
                project_id=1, name="仅规则引用", applicable_types=["character"], match_mode="all",
            ).returning(categories.c.id))
            definition_id = conn.scalar(definitions.insert().values(
                project_id=1, label="未绑定下拉", field_type="dropdown", applicable_types=["character"],
            ).returning(definitions.c.id))
            option_id = conn.scalar(options.insert().values(definition_id=definition_id, label="仅条件使用")
                                   .returning(options.c.id))
            conn.execute(rules.insert().values(category_id=category_id, kind="dropdown", definition_id=definition_id,
                                              option_id=option_id))
        identity = definition_id if target == "setting_field_definitions" else option_id
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            if operation == "delete":
                conn.execute(Base.metadata.tables[target].delete().where(Base.metadata.tables[target].c.id == identity))
            else:
                conn.execute(rules.insert().values(
                    category_id=category_id, kind="dropdown",
                    definition_id=999999 if target == "setting_field_definitions" else definition_id,
                    option_id=999999 if target == "setting_field_options" else option_id,
                ))
        with engine.connect() as conn:
            assert conn.scalar(select(definitions.c.id).where(definitions.c.id == definition_id)) == definition_id
            assert conn.scalar(select(options.c.id).where(options.c.id == option_id)) == option_id
            assert conn.scalar(select(rules.c.id).where(rules.c.category_id == category_id)) is not None
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_0036_rule_shape_names_foreign_keys_category_delete_and_project_cascade(tmp_path):
    config, engine = _sqlite_database(tmp_path)
    try:
        command.upgrade(config, "0036")
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            _verify_rules_and_cascade(conn)
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def _verify_rules_and_cascade(conn):
    categories = Base.metadata.tables["setting_categories"]
    rules = Base.metadata.tables["setting_category_rules"]
    definitions = _historical_table(conn, "setting_field_definitions")
    options = Base.metadata.tables["setting_field_options"]
    first = conn.scalar(categories.insert().values(
        project_id=1, name="焦点", applicable_types=APPLICABLE_TYPES, match_mode="all",
    ).returning(categories.c.id))
    other = conn.scalar(categories.insert().values(
        project_id=2, name="焦点", applicable_types=["world.place"], match_mode="any",
    ).returning(categories.c.id))
    definition_id = conn.scalar(definitions.insert().values(
        project_id=1, label="仅规则使用", field_type="dropdown", applicable_types=APPLICABLE_TYPES,
    ).returning(definitions.c.id))
    option_id = conn.scalar(options.insert().values(definition_id=definition_id, label="可用选项")
                           .returning(options.c.id))
    conn.execute(rules.insert().values(category_id=first, kind="tag", tag="焦点"))
    conn.execute(rules.insert().values(category_id=first, kind="dropdown", definition_id=definition_id,
                                      option_id=option_id))
    conn.execute(rules.insert().values(category_id=other, kind="tag", tag="保留"))
    for values in (
        {"kind": "tag", "tag": ""}, {"kind": "tag", "tag": "   "}, {"kind": "tag"},
        {"kind": "other", "tag": "焦点"}, {"kind": "dropdown", "definition_id": definition_id},
        {"kind": "dropdown", "option_id": option_id},
        {"kind": "dropdown", "tag": "焦点", "definition_id": definition_id, "option_id": option_id},
        {"kind": "tag", "tag": "焦点", "definition_id": definition_id, "option_id": option_id},
    ):
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(rules.insert().values(category_id=first, **values))
    for name, mode in (("焦点", "all"), ("", "all"), ("   ", "all"), ("非法匹配", "ALL")):
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(categories.insert().values(
                project_id=1, name=name, applicable_types=["character"], match_mode=mode,
            ))
    for table, identity in ((definitions, definition_id), (options, option_id)):
        if conn.dialect.name == "postgresql":
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(table.delete().where(table.c.id == identity))
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    if conn.dialect.name == "postgresql":
        for missing_definition in (True, False):
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(rules.insert().values(
                    category_id=first, kind="dropdown",
                    definition_id=999999 if missing_definition else definition_id,
                    option_id=option_id if missing_definition else 999999,
                ))
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    with conn.begin_nested() as savepoint:
        conn.execute(categories.delete().where(categories.c.id == first))
        assert conn.scalar(select(rules.c.id).where(rules.c.category_id == first)) is None
        assert conn.scalar(select(definitions.c.id).where(definitions.c.id == definition_id)) == definition_id
        assert conn.scalar(text("SELECT count(*) FROM characters")) == 2
        savepoint.rollback()
    conn.execute(text("DELETE FROM projects WHERE id=1"))
    assert conn.execute(select(categories.c.id, categories.c.project_id)).all() == [(other, 2)]
    assert conn.execute(select(rules.c.category_id, rules.c.tag)).all() == [(other, "保留")]
    assert conn.scalar(text("SELECT deleted_at IS NOT NULL FROM projects WHERE id=2"))


@pytest.mark.parametrize("new_data", [*ENTITY_TABLES, "category"])
def test_0036_refuses_lossy_downgrade_before_any_change(tmp_path, new_data):
    config, engine = _sqlite_database(tmp_path)
    try:
        command.upgrade(config, "0036")
        with engine.begin() as conn:
            if new_data == "category":
                conn.execute(text(
                    "INSERT INTO setting_categories(project_id,name,applicable_types,match_mode) "
                    "VALUES(1,'还没有条件的新类别','[\"character\"]','all')"
                ))
            else:
                conn.execute(text(f"UPDATE {new_data} SET setting_tags=:tags WHERE id=1"),
                             {"tags": json.dumps(["不能丢失😀"], ensure_ascii=False)})
            before = _snapshot(conn)
        with pytest.raises(RuntimeError, match="不能无损降级"):
            command.downgrade(config, "0035")
        with engine.connect() as conn:
            _assert_preserved(conn, before)
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0036"
    finally:
        engine.dispose()


def test_0036_postgresql_0035_roundtrip_and_no_action_rules_project_cascade(monkeypatch, tmp_path):
    database_url = os.environ.get("AINOVEL_FIELDS_POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("设置 AINOVEL_FIELDS_POSTGRES_TEST_URL 才运行隔离 PostgreSQL 验证")
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+asyncpg" or parsed.host != "127.0.0.1" or parsed.database != "fields_verify":
        raise ValueError("分类迁移测试只允许本机 fields_verify 专用测试数据库")
    schema = f"setting_categories_test_{uuid.uuid4().hex}"
    original_factory = async_sqlalchemy.async_engine_from_config

    def isolated_factory(*args, **kwargs):
        return original_factory(*args, **kwargs, connect_args={"server_settings": {"search_path": schema}})

    async def run(action, *, isolated=True):
        kwargs = {"connect_args": {"server_settings": {"search_path": schema}}} if isolated else {}
        engine = async_sqlalchemy.create_async_engine(database_url, **kwargs)
        try:
            async with engine.begin() as conn:
                assert await conn.scalar(text("SELECT current_database()")) == "fields_verify"
                return await action(conn)
        finally:
            await engine.dispose()

    async def create_schema(conn):
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    async def drop_schema(conn):
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async def seed_legacy(conn):
        await conn.run_sync(_seed_connection)
        for table in ("setting_timelines", "setting_text_fields", "setting_progressions"):
            await conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}','id'), (SELECT max(id) FROM {table}))"
            ))

    async def seed_fields(conn):
        await conn.run_sync(_seed_fields)
        for table in ("setting_field_definitions", "setting_field_options", "setting_reference_handles",
                      "setting_text_fields", "setting_progressions"):
            await conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}','id'), (SELECT max(id) FROM {table}))"
            ))
        return await conn.run_sync(_snapshot), await conn.run_sync(_effective_values)

    async def verify(conn):
        await conn.run_sync(lambda sync: _assert_preserved(sync, before))
        assert await conn.run_sync(_effective_values) == effective
        for name in ENTITY_TABLES:
            rows = await conn.scalars(select(Base.metadata.tables[name].c.setting_tags))
            assert all(tags == [] for tags in rows)
        revisions = await conn.scalars(text("SELECT categories_revision FROM projects ORDER BY id"))
        assert revisions.all() == [0, 0]
        actual = await conn.run_sync(lambda sync: {
            name: _schema_contract(inspect(sync), name) for name in CHANGED_TABLES
        })
        expected_schema = f"{schema}_model"
        await conn.execute(text(f'CREATE SCHEMA "{expected_schema}"'))
        try:
            await conn.execute(text(f'SET LOCAL search_path TO "{expected_schema}"'))
            await conn.run_sync(create_isolated_schema)
            expected = await conn.run_sync(lambda sync: {
                name: _schema_0036_model_contract(inspect(sync), name) for name in CHANGED_TABLES
            })
            assert actual == expected
        finally:
            await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await conn.execute(text(f'DROP SCHEMA "{expected_schema}" CASCADE'))

    async def cascade(conn):
        actions = (await conn.execute(text(
            "SELECT confdeltype,condeferrable,condeferred FROM pg_constraint "
            "WHERE conrelid='setting_category_rules'::regclass "
            "AND contype='f' AND confrelid <> 'setting_categories'::regclass"
        ))).all()
        assert actions == [(b"a", True, True), (b"a", True, True)]
        await conn.run_sync(_verify_rules_and_cascade)
        await conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        await conn.execute(text("UPDATE world_settings SET setting_tags='[\"仍然保留\"]' WHERE id=1"))

    config = _config(tmp_path / "unused.db")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    asyncio.run(run(create_schema, isolated=False))
    try:
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", isolated_factory)
        command.upgrade(config, "0034")
        asyncio.run(run(seed_legacy))
        command.upgrade(config, "0035")
        before, effective = asyncio.run(run(seed_fields))
        command.upgrade(config, "0036")
        asyncio.run(run(verify))
        command.downgrade(config, "0035")
        command.upgrade(config, "0036")
        asyncio.run(run(verify))
        asyncio.run(run(cascade))
        with pytest.raises(RuntimeError, match="不能无损降级"):
            command.downgrade(config, "0035")
    finally:
        asyncio.run(run(drop_schema, isolated=False))
