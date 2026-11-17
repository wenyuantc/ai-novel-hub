"""真实 0055 升级及复合外键、级联和有损降级防护。"""

import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy

from tests.test_setting_fields_migration import _config, _foreign_keys_during_migrations

PARENT_INDEXES = {
    "organizations": ("uq_organizations_project_id_id", ("project_id", "id")),
    "careers": ("uq_careers_project_id_id", ("project_id", "id")),
    "series_entries": ("uq_series_entries_series_id_id", ("series_id", "id")),
}


@pytest.mark.parametrize("database", ["sqlite", "postgresql"])
def test_0056_upgrade_constraints_and_cascades(tmp_path, monkeypatch, database):
    schema = f"thumbnail_migration_{uuid4().hex}"
    config = _config(tmp_path / "thumbnail.db")
    if database == "postgresql":
        series_url = os.environ.get("AINOVEL_SERIES_POSTGRES_TEST_URL")
        fields_url = os.environ.get("AINOVEL_FIELDS_POSTGRES_TEST_URL")
        url = series_url or fields_url
        if not url:
            pytest.skip("设置 AINOVEL_SERIES_POSTGRES_TEST_URL 或 AINOVEL_FIELDS_POSTGRES_TEST_URL")
        expected_database, expected_user = (("series_verify", "series_test") if series_url
                                            else ("fields_verify", "field_test"))
        parsed = make_url(url)
        if (parsed.drivername, parsed.host, parsed.database, parsed.username) != (
            "postgresql+asyncpg", "127.0.0.1", expected_database, expected_user,
        ):
            raise ValueError("缩略图迁移测试仅允许本机 series_test/series_verify 或 field_test/fields_verify")
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        original_factory = async_sqlalchemy.async_engine_from_config
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", lambda *args, **kwargs: original_factory(
            *args, **kwargs, connect_args={"server_settings": {"search_path": schema}},
        ))
    else:
        _foreign_keys_during_migrations(monkeypatch)
        url = f"sqlite+aiosqlite:///{tmp_path / 'thumbnail.db'}"

    async def run(action, *, public=False):
        kwargs = ({"connect_args": {"server_settings": {"search_path": "public" if public else schema}}}
                  if database == "postgresql" else {})
        engine = async_sqlalchemy.create_async_engine(url, **kwargs)
        if database == "sqlite":
            @event.listens_for(engine.sync_engine, "connect")
            def foreign_keys(connection, _record):
                connection.execute("PRAGMA foreign_keys=ON")
        try:
            async with engine.begin() as db:
                if database == "postgresql":
                    assert await db.scalar(text("SELECT current_database()")) == expected_database
                return await action(db)
        finally:
            await engine.dispose()

    async def schema_action(db, *, create):
        await db.execute(text(f'{"CREATE" if create else "DROP"} SCHEMA "{schema}"' + ("" if create else " CASCADE")))

    async def seed(db):
        statements = (
            "INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')",
            "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
            "VALUES(1,1,'旧作品一','','','','planning'),(2,1,'旧作品二','','','','planning')",
            "INSERT INTO characters(id,project_id,name,role_type,profile,relations_text,sort) "
            "VALUES(1,1,'甲','','','',0),(2,2,'乙','','','',0)",
            "INSERT INTO world_settings(id,project_id,title,content,category,sort) VALUES(1,1,'地点','','place',0)",
            "INSERT INTO organizations(id,project_id,name,sort) VALUES(1,1,'组织',0),(2,2,'另一组织',0)",
            "INSERT INTO careers(id,project_id,name,sort) VALUES(1,1,'职业',0),(2,2,'另一职业',0)",
            "INSERT INTO series(id,owner_id,name) VALUES(1,1,'系列一'),(2,1,'系列二')",
            "INSERT INTO series_entries(id,series_id,kind,display_name,data,ai_settings) "
            "VALUES(1,1,'character','角色','{}','{}'),(2,2,'character','角色二','{}','{}')",
        )
        for statement in statements:
            await db.execute(text(statement))

    async def resources(db):
        assert await db.scalar(text("SELECT title FROM projects WHERE id=1")) == "旧作品一"
        for ident, project, series in (("p1", 1, None), ("p2", 2, None), ("s1", None, 1), ("s2", None, 2)):
            await db.execute(text(
                "INSERT INTO setting_image_resources"
                "(id,project_id,series_id,mime_type,data,width,height,byte_size,sha256) "
                "VALUES(:id,:project,:series,'image/webp',:data,1,1,1,:hash)"
            ), {"id": ident, "project": project, "series": series, "data": b"x", "hash": "x" * 64})

    async def insert(db, statement):
        await db.execute(text(statement))

    async def parent_indexes(db):
        def inspect_indexes(connection):
            inspector = inspect(connection)
            return {table: {
                index["name"]: (tuple(index["column_names"]), index["unique"])
                for index in inspector.get_indexes(table)
            } for table in PARENT_INDEXES}

        return await db.run_sync(inspect_indexes)

    async def binding_foreign_keys(db):
        def inspect_foreign_keys(connection):
            return {fk["name"]: fk for fk in inspect(connection).get_foreign_keys("setting_thumbnail_bindings")}

        return await db.run_sync(inspect_foreign_keys)

    async def valid_bindings(db):
        for column in ("character_id", "world_setting_id", "organization_id", "career_id"):
            await db.execute(text(f"INSERT INTO setting_thumbnail_bindings(project_id,{column},image_resource_id) "
                                  "VALUES(1,1,'p1')"))
        await db.execute(text("INSERT INTO setting_thumbnail_bindings(series_id,series_entry_id,image_resource_id) "
                              "VALUES(1,1,'s1')"))

    async def cascades(db):
        await db.execute(text("DELETE FROM characters WHERE id=1"))
        assert await db.scalar(text("SELECT count(*) FROM setting_thumbnail_bindings WHERE character_id=1")) == 0
        assert await db.scalar(text("SELECT count(*) FROM setting_image_resources WHERE id='p1'")) == 1
        await db.execute(text("DELETE FROM projects WHERE id=1"))
        assert await db.scalar(text("SELECT count(*) FROM setting_thumbnail_bindings WHERE project_id=1")) == 0
        assert await db.scalar(text("SELECT count(*) FROM setting_image_resources WHERE project_id=1")) == 0
        await db.execute(text("DELETE FROM series WHERE id=1"))
        assert await db.scalar(text("SELECT count(*) FROM setting_thumbnail_bindings WHERE series_id=1")) == 0
        assert await db.scalar(text("SELECT count(*) FROM setting_image_resources WHERE series_id=1")) == 0
        assert await db.scalar(text("SELECT title FROM projects WHERE id=2")) == "旧作品二"
        await db.execute(text("DELETE FROM setting_image_resources"))
        if database == "sqlite":
            assert not (await db.execute(text("PRAGMA foreign_key_check"))).all()

    created = False
    try:
        if database == "postgresql":
            asyncio.run(run(lambda db: schema_action(db, create=True), public=True))
            created = True
        command.upgrade(config, "0055")
        asyncio.run(run(seed))
        before_indexes = asyncio.run(run(parent_indexes))
        for table, (name, _) in PARENT_INDEXES.items():
            assert name not in before_indexes[table]
        command.upgrade(config, "0056")
        after_indexes = asyncio.run(run(parent_indexes))
        for table, (name, columns) in PARENT_INDEXES.items():
            assert after_indexes[table][name] == (columns, True)
        foreign_keys = asyncio.run(run(binding_foreign_keys))
        for name, local, table, remote in (
            ("fk_thumbnail_organization", ("project_id", "organization_id"), "organizations", ("project_id", "id")),
            ("fk_thumbnail_career", ("project_id", "career_id"), "careers", ("project_id", "id")),
            ("fk_thumbnail_series_entry", ("series_id", "series_entry_id"), "series_entries", ("series_id", "id")),
        ):
            foreign_key = foreign_keys[name]
            assert tuple(foreign_key["constrained_columns"]) == local
            assert foreign_key["referred_table"] == table
            assert tuple(foreign_key["referred_columns"]) == remote
            assert foreign_key["options"].get("ondelete") == "CASCADE"
        asyncio.run(run(resources))
        for statement in (
            "INSERT INTO setting_thumbnail_bindings(project_id,character_id,image_resource_id) VALUES(1,2,'p1')",
            "INSERT INTO setting_thumbnail_bindings(project_id,character_id,image_resource_id) VALUES(1,1,'p2')",
            "INSERT INTO setting_thumbnail_bindings(project_id,organization_id,image_resource_id) VALUES(1,2,'p1')",
            "INSERT INTO setting_thumbnail_bindings(project_id,career_id,image_resource_id) VALUES(1,2,'p1')",
            "INSERT INTO setting_thumbnail_bindings(project_id,character_id,world_setting_id,image_resource_id) "
            "VALUES(1,1,1,'p1')",
            "INSERT INTO setting_thumbnail_bindings(series_id,series_entry_id,image_resource_id) VALUES(1,2,'s1')",
            "INSERT INTO setting_thumbnail_bindings(series_id,series_entry_id,image_resource_id) VALUES(1,1,'s2')",
        ):
            with pytest.raises(IntegrityError):
                asyncio.run(run(lambda db, statement=statement: insert(db, statement)))
        asyncio.run(run(valid_bindings))
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0055")
        asyncio.run(run(cascades))
        command.downgrade(config, "0055")
        downgraded_indexes = asyncio.run(run(parent_indexes))
        for table, (name, _) in PARENT_INDEXES.items():
            assert name not in downgraded_indexes[table]
    finally:
        if created:
            asyncio.run(run(lambda db: schema_action(db, create=False), public=True))
