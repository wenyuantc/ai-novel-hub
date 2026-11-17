"""真实迁移验证模板身份和创建收据的独立生命周期。"""

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.ext import asyncio as async_sqlalchemy

from tests.pg_support import collab_test_url
from tests.test_setting_fields_migration import _config, _foreign_keys_during_migrations


def test_0057_template_ids_and_receipts_survive_result_deletion(tmp_path, monkeypatch):
    _foreign_keys_during_migrations(monkeypatch)
    path = tmp_path / "novel-template-migration.sqlite3"
    config = _config(path)
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        command.upgrade(config, "0056")
        with engine.begin() as db:
            db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'author','hash')"))
            db.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES(1,1,'原书','','','','planning')"))
        command.upgrade(config, "0057")
        with engine.begin() as db:
            assert db.scalar(text("SELECT title FROM projects WHERE id=1")) == "原书"
            ddl = db.scalar(text("SELECT sql FROM sqlite_master WHERE name='novel_templates'"))
            assert "AUTOINCREMENT" in ddl.upper()
            db.execute(text("INSERT INTO novel_templates(owner_id,name,description,snapshot,counts,source_title) "
                            "VALUES(1,'旧模板','','{}','{}','原书')"))
            old_id = db.scalar(text("SELECT id FROM novel_templates WHERE name='旧模板'"))
            db.execute(text("DELETE FROM novel_templates WHERE id=:id"), {"id": old_id})
            db.execute(text("INSERT INTO novel_templates(owner_id,name,description,snapshot,counts,source_title) "
                            "VALUES(1,'新模板','','{}','{}','原书')"))
            assert db.scalar(text("SELECT id FROM novel_templates WHERE name='新模板'")) > old_id
        # 先分别验证模板和图片存在时拒绝降级，再验证只剩成功收据时也拒绝降级。
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0056")
        with engine.begin() as db:
            db.execute(text("INSERT INTO novel_template_images"
                            "(template_id,local_ref,mime_type,data,width,height,byte_size,sha256) "
                            "SELECT id,'character.1','image/webp',:data,1,1,1,:digest FROM novel_templates"),
                       {"data": b"x", "digest": "a" * 64})
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0056")
        with engine.begin() as db:
            db.execute(text("INSERT INTO novel_template_creation_receipts"
                            "(owner_id,intent_key,request_version,request_digest,result_project_id) "
                            "VALUES(1,'00000000-0000-4000-8000-000000000001',1,:digest,1)"),
                       {"digest": "a" * 64})
            db.execute(text("DELETE FROM novel_templates"))
            db.execute(text("DELETE FROM projects WHERE id=1"))
            assert db.scalar(text("SELECT result_project_id FROM novel_template_creation_receipts")) is None
            assert db.scalar(text("SELECT count(*) FROM novel_template_creation_receipts")) == 1
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0056")
        with engine.begin() as db:
            db.execute(text("DELETE FROM novel_template_creation_receipts"))
            assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
        command.downgrade(config, "0056")
        with engine.connect() as db:
            assert "novel_templates" not in inspect(db).get_table_names()
    finally:
        engine.dispose()


def test_0057_postgresql_identity_and_receipt_lifecycle(tmp_path, monkeypatch):
    url = collab_test_url(purpose="小说模板迁移测试")
    schema = f"novel_template_{uuid4().hex}"
    factory = async_sqlalchemy.async_engine_from_config

    def isolated(*args, **kwargs):
        return factory(*args, **kwargs, connect_args={"server_settings": {"search_path": schema}})

    async def run(action, *, public=False):
        engine = async_sqlalchemy.create_async_engine(url, connect_args={"server_settings": {
            "search_path": "public" if public else schema,
        }})
        try:
            async with engine.begin() as db:
                assert await db.scalar(text("SELECT current_database()")) == "collab_test"
                await action(db)
        finally:
            await engine.dispose()

    async def create(db):
        await db.execute(text(f'CREATE SCHEMA "{schema}"'))

    async def drop(db):
        await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async def seed(db):
        await db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'author','hash')"))
        await db.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                              "VALUES(1,1,'源作品','','','','planning')"))

    async def exercise(db):
        insert = text("INSERT INTO novel_templates(owner_id,name,description,snapshot,counts,source_title) "
                      "VALUES(1,:name,'','{}','{}','源作品') RETURNING id")
        first = await db.scalar(insert, {"name": "旧模板"})
        await db.execute(text("DELETE FROM novel_templates WHERE id=:id"), {"id": first})
        second = await db.scalar(insert, {"name": "新模板"})
        assert second > first
        await db.execute(text("INSERT INTO novel_template_creation_receipts"
                              "(owner_id,intent_key,request_version,request_digest,result_project_id) "
                              "VALUES(1,'00000000-0000-4000-8000-000000000001',1,:digest,1)"),
                         {"digest": "a" * 64})
        await db.execute(text("DELETE FROM novel_templates"))
        await db.execute(text("DELETE FROM projects WHERE id=1"))
        result = (await db.execute(text("SELECT result_project_id FROM novel_template_creation_receipts"))).one()
        assert result.result_project_id is None

    async def cleanup(db):
        await db.execute(text("DELETE FROM novel_template_creation_receipts"))

    async def add_template(db):
        await db.execute(text("INSERT INTO novel_templates(owner_id,name,description,snapshot,counts,source_title) "
                              "VALUES(1,'有数据模板','','{}','{}','源作品')"))

    async def remove_templates(db):
        await db.execute(text("DELETE FROM novel_templates"))

    config = _config(tmp_path / "unused.db")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    created = False
    try:
        asyncio.run(run(create, public=True))
        created = True
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", isolated)
        command.upgrade(config, "0056")
        asyncio.run(run(seed))
        command.upgrade(config, "0057")
        asyncio.run(run(add_template))
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0056")
        asyncio.run(run(remove_templates))
        asyncio.run(run(exercise))
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0056")
        asyncio.run(run(cleanup))
        command.downgrade(config, "0056")
    finally:
        if created:
            asyncio.run(run(drop, public=True))
