"""专用本机PG：真实迁移、内容CAS、活动顺序与级联；每例使用UUID schema。"""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy.ext.asyncio as sa_async
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app import main
from app.models import Chapter, Project, Scene
from app.services import authorization_service
from tests.helpers import create_project, make_settings, register
from tests.schema_helpers import create_isolated_schema


def _test_url():
    url = os.environ.get("AINOVEL_SCENES_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("设置 AINOVEL_SCENES_POSTGRES_TEST_URL 才运行隔离PG场景验证")
    parsed = make_url(url)
    if (parsed.drivername, parsed.host, parsed.database, parsed.username) != (
        "postgresql+asyncpg", "127.0.0.1", "scenes_verify", "scenes_test",
    ):
        raise ValueError("仅允许本机 scenes_test/scenes_verify 专用测试库")
    return url


@pytest.fixture
async def postgres_scenes_app(tmp_path, fake_llm, monkeypatch):
    url = _test_url()
    schema = f"scenes_test_{uuid4().hex}"
    admin = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    application = None
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as conn:
            await conn.run_sync(create_isolated_schema)
        monkeypatch.setattr(main, "create_engine", lambda _url: engine)
        application = main.create_app(make_settings(tmp_path, database_url=url),
                                      provider_factory=fake_llm.factory, start_task_engine=False)
        await authorization_service.initialize(application.state.sessionmaker)
        await application.state.system_settings.load()
        yield application
    finally:
        if application is not None:
            await application.state.system_settings.close()
        await engine.dispose()
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin.dispose()


async def test_pg_revision_competition_order_swap_and_cascade(postgres_scenes_app):
    app = postgres_scenes_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await register(client)
        project = await create_project(client)
        base = f"/api/v1/projects/{project['id']}"
        chapter = (await client.post(f"{base}/chapters", json={"content": "初稿"})).json()
        url = f"{base}/chapters/{chapter['id']}"
        first = (await client.get(f"{url}/scenes")).json()["items"][0]
        requests = [{"expected_content_revision": 0, "content": value} for value in ("甲稿", "乙稿")]
        responses = await asyncio.gather(*(client.patch(f"{url}/scenes/{first['id']}", json=body) for body in requests))
        assert sorted(response.status_code for response in responses) == [200, 409]
        conflict = next(response for response in responses if response.status_code == 409)
        assert conflict.json()["code"] == "stale_scene_revision"
        second = await client.post(f"{url}/scenes", json={"expected_content_revision": 1, "content": "二稿"})
        assert second.status_code == 201, second.text
        swap = await client.post(f"{url}/scenes/reorder", json={
            "expected_content_revision": 2, "scene_ids": [second.json()["id"], first["id"]],
        })
        assert swap.status_code == 200, swap.text
        assert [row["sort"] for row in swap.json()["items"]] == [0, 1]
        async with app.state.sessionmaker() as db:
            assert (await db.get(Chapter, chapter["id"])).content_revision == 3
            await db.execute(delete(Project).where(Project.id == project["id"]))
            await db.commit()
            assert list(await db.scalars(select(Scene).where(Scene.chapter_id == chapter["id"]))) == []


def test_pg_fresh_and_0038_upgrade_preserve_children(monkeypatch):
    url = _test_url()
    schema = f"scenes_migrate_{uuid4().hex}"

    async def sql(statements):
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                for statement, values in statements:
                    await conn.execute(text(statement), values)
        finally:
            await engine.dispose()

    asyncio.run(sql([(f'CREATE SCHEMA "{schema}"', {})]))
    original_factory = sa_async.async_engine_from_config

    def isolated_factory(*args, **kwargs):
        return original_factory(*args, **kwargs, connect_args={"server_settings": {"search_path": schema}})

    monkeypatch.setattr(sa_async, "async_engine_from_config", isolated_factory)
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    try:
        command.upgrade(config, "0038")  # fresh 全链；再用真实存量子行升级。
        statements = [
            (f'SET search_path TO "{schema}"', {}),
            ("INSERT INTO users(id,username,password_hash) VALUES(1,'legacy','hash')", {}),
            ("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
             "VALUES(1,1,'旧书','','','','planning')", {}),
            ("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
             "VALUES(4,1,1,'旧章',:content,'done',5)", {"content": " 甲🙂\r\n尾 "}),
            ("INSERT INTO chapter_versions(chapter_id,seq,source,content,word_count) "
             "VALUES(4,1,'manual','历史',2)", {}),
            ("INSERT INTO planning_units(project_id,chapter_id) VALUES(1,4)", {}),
            ("INSERT INTO planning_tags(id,project_id,name) VALUES(3,1,'标签')", {}),
            ("INSERT INTO planning_unit_tags(unit_id,tag_id) SELECT id,3 FROM planning_units WHERE chapter_id=4", {}),
        ]
        asyncio.run(sql(statements))
        command.upgrade(config, "0039")

        async def verify():
            engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
            try:
                async with engine.begin() as conn:
                    assert await conn.scalar(text("SELECT content FROM scenes WHERE chapter_id=4")) == " 甲🙂\r\n尾 "
                    assert await conn.scalar(text("SELECT count(*) FROM planning_unit_tags")) == 1
                    assert await conn.scalar(text("SELECT scene_snapshot FROM chapter_versions")) is None
                    assert await conn.scalar(text("SELECT content_revision FROM chapters")) == 0
                    await conn.execute(text("DELETE FROM projects WHERE id=1"))
                    assert await conn.scalar(text("SELECT count(*) FROM scenes")) == 0
            finally:
                await engine.dispose()

        asyncio.run(verify())
        command.downgrade(config, "0038")
    finally:
        asyncio.run(sql([(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE', {})]))
