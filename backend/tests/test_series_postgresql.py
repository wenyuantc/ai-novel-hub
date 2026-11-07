"""用本机专用 PostgreSQL 数据库验证父作品锁、绑定约束与级联。"""

import asyncio
import os
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app import main
from app.models import ChapterCharacter, Project, SeriesEntry, SeriesEntryUse
from app.services import authorization_service, project_lifecycle, retrieval_service
from tests.helpers import create_project, make_settings, register
from tests.schema_helpers import create_isolated_schema
from tests.test_series_transfer import BASE, _join_books, _post


@pytest.fixture
async def postgres_series_app(tmp_path, fake_llm, monkeypatch):
    url = os.environ.get("AINOVEL_SERIES_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("设置 AINOVEL_SERIES_POSTGRES_TEST_URL 才运行隔离 PostgreSQL 系列验证")
    parsed = make_url(url)
    if (parsed.drivername, parsed.host, parsed.database, parsed.username) != (
        "postgresql+asyncpg", "127.0.0.1", "series_verify", "series_test",
    ):
        raise ValueError("系列测试仅允许本机 series_test/series_verify 专用测试库")
    schema = f"series_test_{uuid4().hex}"
    admin = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    application = None
    created = False
    retrieval_service.invalidate()
    try:
        async with admin.begin() as conn:
            assert await conn.scalar(text("SELECT current_database()")) == "series_verify"
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        async with engine.begin() as conn:
            assert await conn.scalar(text("SELECT current_schema()")) == schema
            await conn.run_sync(create_isolated_schema)
        monkeypatch.setattr(main, "create_engine", lambda _url: engine)
        application = main.create_app(
            make_settings(tmp_path, database_url=url),
            provider_factory=fake_llm.factory,
            start_task_engine=False,
        )
        await authorization_service.initialize(application.state.sessionmaker)
        await application.state.system_settings.load()
        yield application
    finally:
        if application is not None:
            await application.state.system_settings.close()
        await engine.dispose()
        try:
            if created:
                async with admin.begin() as conn:
                    await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin.dispose()
            retrieval_service.invalidate()


async def _wait_for_blocker(maker, holder_pid: int) -> None:
    query = text(
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
        "WHERE datname = current_database() AND :holder = ANY(pg_blocking_pids(pid)))"
    )
    while True:
        # pg_stat_activity 快照不能缓存到下一次轮询。
        async with maker() as db:
            if await db.scalar(query, {"holder": holder_pid}):
                return
        await asyncio.sleep(0.01)


async def test_reference_write_beats_unuse_and_purge_preserves_other_books(postgres_series_app):
    app = postgres_series_app
    maker = app.state.sessionmaker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await register(client)
        a = await create_project(client, title="前作")
        b = await create_project(client, title="续作")
        route, series = await _join_books(client, a, b)
        mutation = await _post(client, route + "/entries", {
            "kind": "character", "data": {"name": "林青"}, "expected_revision": series["revision"],
        })
        entry_id = mutation["item"]["id"]
        uses = f"{route}/entries/{entry_id}/uses"
        for book in (a, b):
            mutation = await _post(client, uses, {
                "project_id": book["id"], "expected_revision": mutation["revision"],
            })
        local_id = mutation["ref"]["id"]
        chapters = f"{BASE}/projects/{b['id']}/chapters"
        chapter = await _post(client, chapters, {"title": "续作第一章"})
        unuse = f"{uses}/{b['id']}"
        pending = None
        try:
            async with maker() as writer:
                await project_lifecycle.lock_active(writer, b["id"])
                pid = await writer.scalar(text("SELECT pg_backend_pid()"))
                writer.add(ChapterCharacter(chapter_id=chapter["id"], character_id=local_id))
                await writer.flush()
                pending = asyncio.create_task(client.delete(unuse, params={"expected_revision": mutation["revision"]}))
                await asyncio.wait_for(_wait_for_blocker(maker, pid), 6)
                await writer.commit()
            result = await asyncio.wait_for(pending, 6)
            assert result.status_code == 409, result.text
            assert result.json()["code"] == "series_setting_in_use"
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending

        async with maker() as db:
            await db.execute(delete(Project).where(Project.id == a["id"]))
            await db.commit()
            assert await db.get(SeriesEntry, entry_id) is not None
            ids = await db.scalars(select(SeriesEntryUse.project_id).where(SeriesEntryUse.series_entry_id == entry_id))
            assert list(ids) == [b["id"]]
        cast_url = f"{chapters}/{chapter['id']}/cast"
        assert (await client.get(cast_url)).json() == {"character_ids": [local_id]}
        assert (await client.put(cast_url, json={"character_ids": []})).is_success
        latest = (await client.get(route)).json()["revision"]
        removed = await client.delete(unuse, params={"expected_revision": latest})
        assert removed.is_success, removed.text
        invalid = await client.put(cast_url, json={"character_ids": [local_id]})
        assert invalid.status_code in (400, 404, 409), invalid.text
        assert (await client.get(cast_url)).json() == {"character_ids": []}
