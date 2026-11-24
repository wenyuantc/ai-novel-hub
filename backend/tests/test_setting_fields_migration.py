"""0035 在真实存量父子行上保留身份、原文、孤儿事件和回收站作品。"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import (
    CheckConstraint,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy

from app.models import Base

BACKEND = Path(__file__).resolve().parents[1]
LEGACY_TEXT = '原文\n"引号" 😀\\n {"option_id": 99}'
SCHEMA_TABLES = (
    "projects", "setting_timelines", "setting_text_fields", "setting_progressions",
    "setting_field_definitions", "setting_field_options", "setting_reference_handles",
)


def _config(path):
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    config.attributes["configure_logger"] = False
    return config


def _seed_connection(conn):
    statements = (
        "INSERT INTO users(id,username,password_hash) VALUES(1,'fields-owner','hash')",
        "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
        "VALUES(1,1,'旧作品','','','','writing'),(2,1,'回收站作品','','','','planning')",
        "UPDATE projects SET deleted_at='2026-10-01',last_deleted_at='2026-10-01' WHERE id=2",
        "INSERT INTO characters(id,project_id,name,role_type,profile,relations_text,sort) "
        "VALUES(1,1,'甲','','旧档案','',0),(2,1,'乙','','','',1)",
        "INSERT INTO world_settings(id,project_id,title,content,category,sort) VALUES(1,2,'旧山','原貌','place',0)",
        "INSERT INTO organizations(id,project_id,name,sort) VALUES(1,1,'山门',0),(2,2,'旧山门',0)",
        "INSERT INTO careers(id,project_id,name,sort) VALUES(1,1,'剑修',0),(2,2,'画师',0)",
        "INSERT INTO career_stages(id,career_id,position,name) VALUES(1,1,1,'入门')",
        "INSERT INTO character_careers(id,character_id,career_id,role,stage_id) VALUES(1,1,1,'main',1)",
        "INSERT INTO organization_members(id,organization_id,character_id) VALUES(1,1,1)",
        "INSERT INTO relationships(id,project_id,a_id,b_id,pair_low,pair_high,type_id,description) "
        "SELECT 1,1,1,2,1,2,id,'同行者' FROM relationship_types ORDER BY id LIMIT 1",
        "INSERT INTO setting_timelines(id,project_id,subject_kind,character_id,related_character_id,baseline) "
        "VALUES(100,1,'relationship',1,2,'{\"exists\": false}')",
        "INSERT INTO setting_timelines(id,project_id,subject_kind,character_id,baseline,baseline_status,revision) "
        "VALUES(101,1,'character',1,'{}','legacy_unverified',4)",
        "INSERT INTO setting_timelines(id,project_id,subject_kind,world_setting_id,baseline) "
        "VALUES(102,2,'world',1,'{\"content\":\"原貌\"}')",
        "INSERT INTO setting_timelines(id,project_id,subject_kind,character_id,career_id,baseline) "
        "VALUES(103,1,'career',1,1,'{\"exists\": false}')",
        "INSERT INTO setting_timelines(id,project_id,subject_kind,character_id,organization_id,baseline) "
        "VALUES(104,1,'membership',1,1,'{\"exists\": false}')",
        "INSERT INTO planning_units(id,project_id) VALUES(77,1)",
        "INSERT INTO planning_unit_relationships(unit_id,timeline_id) VALUES(77,100)",
    )
    for statement in statements:
        conn.execute(text(statement))
    conn.execute(text(
        "INSERT INTO setting_text_fields(id,timeline_id,label,base_value,created_at,updated_at) "
        "VALUES(201,101,'旧字段',:original,'2025-01-01 01:02:03','2026-01-01 04:05:06'),"
        "(202,102,'旧字段','null','2025-02-02 01:02:03','2026-02-02 04:05:06'),"
        "(203,101,'空字段','','2025-03-03 01:02:03','2026-03-03 04:05:06')"
    ), {"original": LEGACY_TEXT})
    conn.execute(text(
        "INSERT INTO setting_progressions(id,timeline_id,custom_field_id,operation,value,anchor_kind,"
        "chapter_no,active,created_at,updated_at) VALUES"
        "(301,101,201,'append','\"有效文本\"','planned',20,true,'2025-01-01','2026-01-01'),"
        "(302,101,201,'replace','\"已停用文本\"','planned',21,false,'2025-01-02','2026-01-02')"
    ))
    conn.execute(text(
        "INSERT INTO setting_progressions(id,timeline_id,custom_field_id,operation,value,anchor_kind) "
        "VALUES(303,102,202,'replace','\"孤儿文本\"','chapter')"
    ))


def _seed(engine):
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        _seed_connection(conn)


def _snapshot(conn, table, condition=""):
    return [dict(row) for row in conn.execute(text(f"SELECT * FROM {table} {condition} ORDER BY id")).mappings()]


def _historical_table(conn, name):
    """读写旧修订只用当时物理列，避免引入未来 ORM 列与 Python 默认。"""
    return Table(name, MetaData(), autoload_with=conn, resolve_fks=False)


def _foreign_keys_during_migrations(monkeypatch):
    original = async_sqlalchemy.async_engine_from_config

    def enabled(*args, **kwargs):
        engine = original(*args, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def pragma(connection, record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", enabled)


def _schema_contract(inspector, table):
    """比较升级与 create_all 的真实结构，包括约束内容而不只名称。"""
    def normalized_sql(value):
        return " ".join(str(value).split()) if value is not None else None

    return {
        "columns": {
            column["name"]: (
                str(column["type"]), column["nullable"], normalized_sql(column["default"]),
            )
            for column in inspector.get_columns(table)
        },
        "primary_key": inspector.get_pk_constraint(table),
        "foreign_keys": {
            fk["name"]: (
                tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]), fk["options"],
            )
            for fk in inspector.get_foreign_keys(table)
        },
        "unique_constraints": {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table)
        },
        "check_constraints": {
            constraint["name"]: normalized_sql(constraint["sqltext"])
            for constraint in inspector.get_check_constraints(table)
        },
        "indexes": {
            index["name"]: (
                tuple(index["column_names"]), bool(index["unique"]),
                {key: normalized_sql(value) for key, value in index.get("dialect_options", {}).items()},
            )
            for index in inspector.get_indexes(table)
        },
    }


def _create_pre_scene_anchor_contract(connection):
    """期望库用冻结旧DDL取同方言CHECK文本；不推导或改写实际历史库的约束。"""
    Table(
        "_pre_scene_anchor_contract", MetaData(),
        Column("anchor_kind", String(8)), Column("chapter_id", Integer), Column("chapter_no", Integer),
        CheckConstraint(
            "(anchor_kind = 'chapter' AND chapter_no IS NULL) OR "
            "(anchor_kind = 'planned' AND chapter_id IS NULL AND chapter_no IS NOT NULL AND chapter_no >= 1)",
            name="legacy_anchor_shape",
        ),
    ).create(connection)


def _schema_0055_model_contract(inspector, table):
    """从当前 ORM 的反射结果中移除 0055 之后新增的字段和索引。"""
    contract = _schema_contract(inspector, table)
    if table == "projects":
        # 删除请求的执行者归属由 0068 引入。
        assert contract["columns"].pop("deletion_worker_key") == ("VARCHAR(36)", True, None)
    if table == "agent_conversations":
        # 0069 才引入防止整数 ID 复用的内部会话身份。
        assert contract["columns"].pop("instance_key") == ("VARCHAR(36)", False, None)
        assert contract["indexes"].pop("uq_agent_conversations_instance_key") == (("instance_key",), True, {})
    added_index = {
        "organizations": ("uq_organizations_project_id_id", "project_id"),
        "careers": ("uq_careers_project_id_id", "project_id"),
        "series_entries": ("uq_series_entries_series_id_id", "series_id"),
    }.get(table)
    if added_index is not None:
        name, scope_column = added_index
        columns, unique, options = contract["indexes"].pop(name)
        assert columns == (scope_column, "id") and unique
        assert not options.get("sqlite_where")
    return contract


def _schema_0037_model_contract(inspector, table):
    """0038系列索引、0039场景锚点不混入0037及之前的冻结结构比较。"""
    contract = _schema_0055_model_contract(inspector, table)
    index = {
        "projects": "uq_projects_id_user_id",
        "characters": "uq_characters_project_id_id",
        "world_settings": "uq_world_settings_project_id_id",
    }.get(table)
    if index is not None:
        contract["indexes"].pop(index)
    if table == "agent_conversations":
        # 0053 摘要来源、0054 协作邀请轮次（E4-i）晚于 0037，不计入冻结结构。
        contract["columns"].pop("summary_source")
        contract["columns"].pop("collaboration_invitation_id")
    if table == "setting_progressions":
        contract["columns"].pop("scene_id")
        contract["foreign_keys"].pop("fk_setting_progressions_scene_id_scenes")
        contract["indexes"].pop("ix_setting_progressions_scene_id")
        legacy = inspector.get_check_constraints("_pre_scene_anchor_contract")
        assert len(legacy) == 1 and legacy[0]["name"] == "legacy_anchor_shape"
        contract["check_constraints"]["ck_setting_progressions_anchor_shape"] = " ".join(
            legacy[0]["sqltext"].split()
        )
    return contract


def _schema_0036_model_contract(inspector, table):
    """比较冻结的 0036 结构，不把当前的 AI 策略列计入历史合同。"""
    contract = _schema_0037_model_contract(inspector, table)
    columns = contract["columns"]
    if table == "projects":
        columns.pop("ai_visibility_revision")
        columns.pop("ai_history_epoch")
    if table == "agent_conversations":
        columns.pop("ai_history_epoch")
    if table in {"characters", "world_settings", "organizations", "careers"}:
        for name in (
            "ai_mode", "mention_enabled", "mention_case_sensitive", "mention_exclusions", "ai_hidden_builtin_fields",
        ):
            columns.pop(name)
        contract["check_constraints"].pop(f"ck_{table}_ai_mode")
    if table in {"setting_text_fields", "setting_field_definitions"}:
        columns.pop("ai_hidden")
    if table == "setting_text_fields":
        contract["check_constraints"].pop("ck_setting_text_fields_legacy_ai_hidden")
    if table == "setting_reference_handles":
        contract["indexes"].pop("uq_setting_reference_handles_project_id_id")
    return contract


def _schema_0035_model_contract(inspector, table):
    """0035 结构冻结：0036 只为 projects 增加独立分类目录代次。"""
    contract = _schema_0036_model_contract(inspector, table)
    if table == "projects":
        contract["columns"].pop("categories_revision")
    return contract


def test_0035_schema_matches_version_contract(tmp_path):
    path = tmp_path / "schema.db"
    command.upgrade(_config(path), "0035")
    migrated = create_engine(f"sqlite:///{path}")
    expected = create_engine("sqlite://")
    try:
        Base.metadata.create_all(expected)
        with expected.begin() as conn:
            _create_pre_scene_anchor_contract(conn)
        actual_inspector, expected_inspector = inspect(migrated), inspect(expected)
        for table in SCHEMA_TABLES:
            assert _schema_contract(actual_inspector, table) == _schema_0035_model_contract(
                expected_inspector, table
            ), table
        for engine in (migrated, expected):
            with engine.connect() as conn:
                ddl = conn.scalar(text(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='setting_text_fields'"
                ))
                assert "AUTOINCREMENT" in ddl
    finally:
        migrated.dispose()
        expected.dispose()


def test_0035_deleted_highest_empty_binding_id_is_never_reused(tmp_path):
    path = tmp_path / "binding-identities.db"
    config = _config(path)
    command.upgrade(config, "0034")
    engine = create_engine(f"sqlite:///{path}")
    try:
        _seed(engine)
    finally:
        engine.dispose()
    command.upgrade(config, "0035")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            maximum = conn.scalar(text("SELECT max(id) FROM setting_text_fields"))
            assert maximum == 203
            conn.execute(text("DELETE FROM setting_text_fields WHERE id=:id"), {"id": maximum})
            field = _historical_table(conn, "setting_text_fields")
            created = conn.scalar(field.insert().values(timeline_id=101, label="另一个空字段", base_value="")
                                  .returning(field.c.id))
            assert created > maximum
            assert conn.scalar(text("SELECT seq FROM sqlite_sequence WHERE name='setting_text_fields'")) == created
            assert conn.scalar(text("SELECT count(*) FROM setting_progressions")) == 3
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("foreign_keys", [False, True])
def test_0035_preserves_legacy_children_and_safe_empty_downgrade(tmp_path, monkeypatch, foreign_keys):
    path = tmp_path / "typed-fields.db"
    config = _config(path)
    command.upgrade(config, "0034")
    engine = create_engine(f"sqlite:///{path}")
    _seed(engine)
    tables = ("projects", "characters", "world_settings", "organizations", "careers", "character_careers",
              "organization_members", "relationships", "planning_units", "setting_timelines", "setting_text_fields",
              "setting_progressions")
    with engine.connect() as conn:
        before = {table: _snapshot(conn, table) for table in tables}
    if foreign_keys:
        _foreign_keys_during_migrations(monkeypatch)

    command.upgrade(config, "0035")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
        for table in tables:
            rows = _snapshot(conn, table, "WHERE id <= 104" if table == "setting_timelines" else "")
            if table == "projects":
                for row in rows:
                    assert row.pop("fields_revision") == 0
            if table == "setting_text_fields":
                for row in rows:
                    assert row.pop("definition_id") is None
                    row["base_value"] = json.loads(row["base_value"])
            assert rows == before[table], table
        entries = conn.execute(text(
            "SELECT subject_kind, project_id, character_id, organization_id, career_id, baseline, baseline_status "
            "FROM setting_timelines WHERE id > 104 ORDER BY subject_kind, project_id"
        )).all()
        assert [(row[0], row[1], row[2], row[3], row[4]) for row in entries] == [
            ("career_entry", 1, None, None, 1), ("career_entry", 2, None, None, 2),
            ("organization", 1, None, 1, None), ("organization", 2, None, 2, None),
        ]
        assert all(json.loads(row[5]) == {} and row[6] == "confirmed" for row in entries)
        assert conn.scalar(text("SELECT count(*) FROM setting_field_definitions")) == 0
        assert conn.scalar(text("SELECT count(*) FROM setting_reference_handles")) == 0
        assert conn.execute(text("SELECT unit_id,timeline_id FROM planning_unit_relationships")).all() == [(77, 100)]
    command.downgrade(config, "0034")
    with engine.connect() as conn:
        for table in tables:
            assert _snapshot(conn, table) == before[table]
        assert conn.execute(text("SELECT unit_id,timeline_id FROM planning_unit_relationships")).all() == [(77, 100)]
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
    assert "fields_revision" not in {column["name"] for column in inspect(engine).get_columns("projects")}
    engine.dispose()


def test_0035_shapes_json_null_stable_handles_and_project_cascade(tmp_path):
    path = tmp_path / "constraints.db"
    config = _config(path)
    command.upgrade(config, "0034")
    engine = create_engine(f"sqlite:///{path}")
    _seed(engine)
    command.upgrade(config, "0035")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text(
            "INSERT INTO setting_field_definitions(id,project_id,label,field_type,applicable_types) "
            "VALUES(1,1,'掌门','reference','[\"character\"]')"
        ))
        conn.execute(text(
            "INSERT INTO setting_reference_handles(project_id,kind,character_id) VALUES(1,'character',1)"
        ))
        first = conn.scalar(text("SELECT id FROM setting_reference_handles"))
        conn.execute(text("DELETE FROM setting_reference_handles"))
        conn.execute(text(
            "INSERT INTO setting_reference_handles(project_id,kind,character_id) VALUES(1,'character',1)"
        ))
        assert conn.scalar(text("SELECT id FROM setting_reference_handles")) > first
        binding = _historical_table(conn, "setting_text_fields")
        conn.execute(binding.insert().values(timeline_id=101, definition_id=1, label=None, base_value=None))
        assert conn.execute(text(
            "SELECT base_value IS NULL, json_type(base_value) FROM setting_text_fields WHERE definition_id=1"
        )).one() == (0, "null")
        field_id = conn.scalar(text("SELECT id FROM setting_text_fields WHERE definition_id=1"))
        events = _historical_table(conn, "setting_progressions")
        conn.execute(events.insert().values(
            timeline_id=101, custom_field_id=field_id, operation="replace", value=None,
            anchor_kind="planned", chapter_no=32,
        ))
        assert conn.execute(text(
            "SELECT value IS NULL, json_type(value) FROM setting_progressions WHERE custom_field_id=:id"
        ), {"id": field_id}).one() == (0, "null")
        for statement in (
            "DELETE FROM characters WHERE id=1",
            "INSERT INTO setting_reference_handles(project_id,kind,character_id) VALUES(1,'character',1)",
            "INSERT INTO setting_reference_handles(project_id,kind,character_id) VALUES(1,'world',2)",
            "INSERT INTO setting_text_fields(timeline_id,definition_id,label) VALUES(101,1,'共享字段不能有本地名称')",
            "INSERT INTO setting_text_fields(timeline_id,label) VALUES(101,NULL)",
            "INSERT INTO setting_text_fields(timeline_id,label,base_value) VALUES(101,'SQL空值',NULL)",
            "INSERT INTO setting_text_fields(timeline_id,definition_id,label) VALUES(101,1,NULL)",
            "INSERT INTO setting_progressions(timeline_id,field_key,operation,value,anchor_kind,chapter_no) "
            "VALUES(101,'profile','replace',NULL,'planned',3)",
            "INSERT INTO setting_timelines(project_id,subject_kind,character_id,organization_id,baseline) "
            "VALUES(1,'organization',1,1,'{}')",
            "INSERT INTO setting_timelines(project_id,subject_kind,career_id,baseline) "
            "VALUES(1,'career_entry',1,'{}')",
        ):
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(text(statement))
        conn.execute(text("DELETE FROM projects WHERE id=1"))
        for table in ("setting_field_definitions", "setting_reference_handles"):
            assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        assert conn.scalar(text("SELECT count(*) FROM projects")) == 1
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


@pytest.mark.parametrize(
    "new_data", [
        "definition", "handle", "entry_event", "entry_baseline", "legacy_object",
        "legacy_event_object", "orphan_event_object", "inactive_orphan_event_null",
    ],
)
def test_0035_refuses_lossy_downgrade_before_mutation(tmp_path, new_data):
    path = tmp_path / "downgrade.db"
    config = _config(path)
    command.upgrade(config, "0034")
    engine = create_engine(f"sqlite:///{path}")
    _seed(engine)
    command.upgrade(config, "0035")
    statements = {
        "definition": "INSERT INTO setting_field_definitions(project_id,label,field_type,applicable_types) "
                      "VALUES(1,'态度','line','[\"organization\"]')",
        "handle": "INSERT INTO setting_reference_handles(project_id,kind,character_id) VALUES(1,'character',1)",
        "entry_event": "INSERT INTO setting_progressions(timeline_id,field_key,operation,value,anchor_kind,chapter_no) "
                       "SELECT id,'future','replace','\"记录\"','planned',3 FROM setting_timelines "
                       "WHERE subject_kind='organization' AND organization_id=1",
        "entry_baseline": "UPDATE setting_timelines SET baseline='{\"future\":\"记录\"}' "
                          "WHERE subject_kind='career_entry' AND career_id=1",
        "legacy_object": "UPDATE setting_text_fields SET base_value='{\"option_id\": 1}' WHERE id=201",
        "legacy_event_object": "UPDATE setting_progressions SET value='{\"option_id\": 1}' WHERE id=302",
        "orphan_event_object": "UPDATE setting_progressions SET value='{\"reference_handle_id\": 1}' WHERE id=303",
        "inactive_orphan_event_null": "UPDATE setting_progressions SET value='null',active=false WHERE id=303",
    }
    with engine.begin() as conn:
        conn.execute(text(statements[new_data]))
    with pytest.raises(RuntimeError, match="不能无损降级"):
        command.downgrade(config, "0034")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0035"
        assert conn.scalar(text("SELECT count(*) FROM setting_progressions")) >= 3
        assert conn.scalar(text("SELECT definition_id FROM setting_text_fields WHERE id=201")) is None
    engine.dispose()
