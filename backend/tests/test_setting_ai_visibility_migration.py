"""0037 存量图、默认行为、约束及拒绝有损回退的真实迁移验证。"""

import asyncio
import os
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy
from sqlalchemy.orm import Session

from app.models import Base, Career, Character, Organization, WorldSetting
from tests.schema_helpers import create_isolated_schema
from tests.test_setting_categories_migration import (
    ENTITY_TABLES,
    _assert_preserved,
    _seed_fields,
    _snapshot,
    _sqlite_database,
)
from tests.test_setting_fields_migration import (
    _config,
    _foreign_keys_during_migrations,
    _schema_0037_model_contract,
    _schema_contract,
    _seed_connection,
)

CHANGED_TABLES = (
    "projects", "agent_conversations", *ENTITY_TABLES, "setting_text_fields", "setting_field_definitions",
    "setting_reference_handles", "setting_ai_links",
)
ENTRY_MODELS = ((Character, "name"), (WorldSetting, "title"), (Organization, "name"), (Career, "name"))


def _seed_old_extra(conn):
    conn.execute(text("INSERT INTO agent_conversations(id,user_id,project_id,title) VALUES(11,1,1,'旧历史')"))
    conn.execute(text("INSERT INTO agent_messages(conversation_id,role,content) VALUES(11,'assistant','旧回复')"))
    conn.execute(text("UPDATE characters SET always_in_context=true WHERE id=1"))
    if conn.dialect.name == "sqlite":
        conn.execute(text(
            "INSERT INTO characters(id,project_id,name,role_type,profile,relations_text,sort) "
            "VALUES(8000,1,'已删除的最高角色','','','',0)"
        ))
        conn.execute(text("DELETE FROM characters WHERE id=8000"))


def _assert_legacy_defaults(conn):
    for model, _ in ENTRY_MODELS:
        rows = conn.execute(select(
            model.ai_mode, model.mention_enabled, model.mention_case_sensitive,
            model.mention_exclusions, model.ai_hidden_builtin_fields,
        )).all()
        assert rows and all(row == (None, None, True, [], []) for row in rows)
    for table in ("setting_text_fields", "setting_field_definitions"):
        column = Base.metadata.tables[table].c.ai_hidden
        hidden = conn.scalars(select(column)).all()
        assert hidden and all(flag is False for flag in hidden)
    assert conn.execute(text("SELECT ai_visibility_revision,ai_history_epoch FROM projects")).all() == [(0, 0)] * 2
    assert conn.scalar(text("SELECT ai_history_epoch FROM agent_conversations WHERE id=11")) == 0


def _assert_link_graph_constraints(conn):
    links = Base.metadata.tables["setting_ai_links"]
    handles = Base.metadata.tables["setting_reference_handles"]
    conn.execute(handles.insert().values(id=9001, project_id=2, kind="world", world_setting_id=1))
    conn.execute(handles.insert().values(id=9002, project_id=2, kind="organization", organization_id=2))
    for project_id, source, target in ((1, 33, 34), (1, 34, 33), (2, 9001, 9002)):
        conn.execute(links.insert().values(project_id=project_id, source_handle_id=source, target_handle_id=target))
    for project_id, source, target in ((1, 33, 34), (1, 33, 33), (1, 9001, 33), (2, 9001, 33), (1, 33, 9999)):
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(links.insert().values(project_id=project_id, source_handle_id=source, target_handle_id=target))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(text("UPDATE setting_text_fields SET ai_hidden=true WHERE definition_id IS NOT NULL"))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(text("UPDATE careers SET ai_mode='unknown' WHERE id=1"))
    conn.execute(handles.delete().where(handles.c.id == 33))
    assert conn.execute(select(links.c.project_id, links.c.source_handle_id, links.c.target_handle_id)).all() == [
        (2, 9001, 9002),
    ]
    conn.execute(links.insert().values(project_id=1, source_handle_id=31, target_handle_id=32))
    conn.execute(text("DELETE FROM projects WHERE id=1"))
    if conn.dialect.name == "postgresql":
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert conn.execute(select(links.c.project_id)).all() == [(2,)]
    conn.execute(text("DELETE FROM projects WHERE id=2"))
    assert conn.execute(select(links.c.id)).all() == []


@pytest.mark.parametrize("foreign_keys", [False, True])
def test_0037_preserves_real_0036_rows_and_schema_then_safe_roundtrip(tmp_path, monkeypatch, foreign_keys):
    config, engine = _sqlite_database(tmp_path)
    expected = create_engine("sqlite://")
    try:
        command.upgrade(config, "0036")
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            _seed_old_extra(conn)
            before = _snapshot(conn)
            sequences = dict(conn.exec_driver_sql("SELECT name,seq FROM sqlite_sequence").all())
        if foreign_keys:
            _foreign_keys_during_migrations(monkeypatch)
        command.upgrade(config, "0037")
        Base.metadata.create_all(expected)
        for table in CHANGED_TABLES:
            current = _schema_contract(inspect(engine), table)
            assert current == _schema_0037_model_contract(inspect(expected), table), table
        with engine.connect() as conn:
            _assert_preserved(conn, before)
            _assert_legacy_defaults(conn)
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            after = dict(conn.exec_driver_sql("SELECT name,seq FROM sqlite_sequence").all())
            assert {name: after[name] for name in sequences} == sequences
        command.downgrade(config, "0036")
        with engine.connect() as conn:
            assert _snapshot(conn) == before
            assert dict(conn.exec_driver_sql("SELECT name,seq FROM sqlite_sequence").all()) == sequences
        command.upgrade(config, "0037")
        with Session(engine) as db:
            for model, name_field in ENTRY_MODELS:
                explicit_legacy = model(project_id=1, ai_mode=None, **{name_field: "恢复的兼容条目"})
                fresh = model(project_id=1, **{name_field: "新条目"})
                db.add_all([explicit_legacy, fresh])
                db.flush()
                assert explicit_legacy.ai_mode is None and fresh.ai_mode == "detected"
                assert fresh.mention_enabled is None and fresh.mention_case_sensitive is True
                if model is Character:
                    assert 8000 < explicit_legacy.id < fresh.id
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            _assert_link_graph_constraints(conn)
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
        expected.dispose()


def test_0037_preserves_sequence_when_rebuilt_table_was_empty(tmp_path):
    path = tmp_path / "empty-careers.db"
    config = _config(path)
    command.upgrade(config, "0036")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'empty-visibility','hash')"))
            conn.execute(text(
                "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                "VALUES(1,1,'作品','','','','planning')"
            ))
            conn.execute(text("INSERT INTO careers(id,project_id,name,sort) VALUES(5000,1,'最高已删除',0)"))
            conn.execute(text("DELETE FROM careers"))
        command.upgrade(config, "0037")
        with engine.begin() as conn:
            new_id = conn.scalar(text("INSERT INTO careers(project_id,name,sort) VALUES(1,'新身份',0) RETURNING id"))
            assert new_id > 5000
    finally:
        engine.dispose()


def test_0037_refuses_lossy_downgrade_before_any_mutation(tmp_path):
    config, engine = _sqlite_database(tmp_path)
    try:
        command.upgrade(config, "0037")
        for setup, cleanup in (
            ("UPDATE characters SET ai_mode='detected' WHERE id=1", "UPDATE characters SET ai_mode=NULL WHERE id=1"),
            ("UPDATE careers SET mention_enabled=false WHERE id=1",
             "UPDATE careers SET mention_enabled=NULL WHERE id=1"),
            ("UPDATE setting_text_fields SET ai_hidden=true WHERE id=201",
             "UPDATE setting_text_fields SET ai_hidden=false WHERE id=201"),
            ("UPDATE projects SET ai_history_epoch=1 WHERE id=1", "UPDATE projects SET ai_history_epoch=0 WHERE id=1"),
            ("INSERT INTO setting_ai_links(project_id,source_handle_id,target_handle_id) VALUES(1,31,32)",
             "DELETE FROM setting_ai_links"),
        ):
            with engine.begin() as conn:
                conn.execute(text(setup))
                protected = _snapshot(conn)
            with pytest.raises(RuntimeError, match="不能无损降级"):
                command.downgrade(config, "0036")
            with engine.begin() as conn:
                assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0037"
                assert _snapshot(conn) == protected
                conn.execute(text(cleanup))
    finally:
        engine.dispose()


def test_0037_postgresql_preserves_rows_constraints_and_orm_contract(tmp_path, monkeypatch):
    database_url = os.environ.get("AINOVEL_FIELDS_POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("设置 AINOVEL_FIELDS_POSTGRES_TEST_URL 才运行隔离 PostgreSQL 验证")
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+asyncpg" or parsed.host != "127.0.0.1" or parsed.database != "fields_verify":
        raise ValueError("AI 可见性迁移测试只允许本机 fields_verify 专用测试数据库")
    schema = f"setting_visibility_test_{uuid.uuid4().hex}"
    original = async_sqlalchemy.async_engine_from_config

    def isolated_factory(*args, **kwargs):
        return original(*args, **kwargs, connect_args={"server_settings": {"search_path": schema}})

    async def run(action, *, path=schema):
        db = async_sqlalchemy.create_async_engine(database_url, connect_args={"server_settings": {"search_path": path}})
        try:
            async with db.begin() as conn:
                assert await conn.scalar(text("SELECT current_database()")) == "fields_verify"
                return await conn.run_sync(action)
        finally:
            await db.dispose()

    def seed_legacy(conn):
        _seed_connection(conn)
        for table in ("setting_timelines", "setting_text_fields", "setting_progressions"):
            conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}','id'), (SELECT max(id) FROM {table}))"))

    def inspect_contract(conn):
        return {name: _schema_contract(inspect(conn), name) for name in CHANGED_TABLES}

    config = _config(tmp_path / "unused.db")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    asyncio.run(run(lambda conn: conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"'), path="public"))
    try:
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", isolated_factory)
        command.upgrade(config, "0034")
        asyncio.run(run(seed_legacy))
        command.upgrade(config, "0035")
        asyncio.run(run(_seed_fields))
        command.upgrade(config, "0036")
        asyncio.run(run(_seed_old_extra))
        before = asyncio.run(run(_snapshot))
        command.upgrade(config, "0037")
        asyncio.run(run(lambda conn: _assert_preserved(conn, before)))
        asyncio.run(run(_assert_legacy_defaults))
        actual = asyncio.run(run(inspect_contract))

        def compare_with_models(conn):
            model_schema = f"{schema}_model"
            conn.exec_driver_sql(f'CREATE SCHEMA "{model_schema}"')
            conn.exec_driver_sql(f'SET LOCAL search_path TO "{model_schema}"')
            create_isolated_schema(conn)
            assert {name: _schema_0037_model_contract(inspect(conn), name) for name in CHANGED_TABLES} == actual
            conn.exec_driver_sql(f'DROP SCHEMA "{model_schema}" CASCADE')

        asyncio.run(run(compare_with_models))
        command.downgrade(config, "0036")
        assert asyncio.run(run(_snapshot)) == before
        command.upgrade(config, "0037")
        asyncio.run(run(_assert_link_graph_constraints))
    finally:
        asyncio.run(run(lambda conn: conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE'), path="public"))
