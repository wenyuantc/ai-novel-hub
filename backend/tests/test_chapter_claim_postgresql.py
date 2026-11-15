"""0046 迁移及占用身份真实 PostgreSQL 多连接验证，仅使用隔离测试库。"""

import asyncio
import os
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy

from app.core.errors import AppError
from app.models import Chapter, ProjectCollaborator, Task
from app.services import chapter_claim, project_access, project_lifecycle
from tests.helpers import create_project, register
from tests.test_chapter_claim_migration import _config
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture

postgres_chapter_apps = _postgres_fixture


def test_0046_postgresql_preserves_children_and_refuses_lossy_downgrade(tmp_path, monkeypatch):
    dedicated = os.environ.get("AINOVEL_COLLAB_CHAPTER_POSTGRES_TEST_URL")
    url = dedicated or os.environ.get("AINOVEL_COLLAB_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("设置 AINOVEL_COLLAB_CHAPTER_POSTGRES_TEST_URL 或 AINOVEL_COLLAB_POSTGRES_TEST_URL")
    database = "collab_chapter" if dedicated else "collab_test"
    parsed = make_url(url)
    if (parsed.drivername, parsed.host, parsed.database, parsed.username) != (
        "postgresql+asyncpg", "127.0.0.1", database, "collab_test",
    ):
        raise ValueError("章节迁移测试仅允许本机指定的隔离数据库")
    schema = f"collab_chapter_migration_{uuid4().hex}"
    original = async_sqlalchemy.async_engine_from_config

    def isolated_factory(*args, **kwargs):
        return original(*args, **kwargs, connect_args={"server_settings": {"search_path": schema}})

    async def run(action, *, isolated=True):
        engine = async_sqlalchemy.create_async_engine(url, connect_args={"server_settings": {
            "search_path": schema if isolated else "public",
        }})
        try:
            async with engine.begin() as conn:
                assert await conn.scalar(text("SELECT current_database()")) == database
                await action(conn)
        finally:
            await engine.dispose()

    async def create_schema(conn):
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    async def drop_schema(conn):
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async def seed(conn):
        await conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
        await conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                                "VALUES(1,1,'旧书','','','','writing')"))
        await conn.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                                "VALUES(1,1,1,'第一章','原文','draft',2),"
                                "(2,1,2,'旧占用','旧正文','generating',3)"))
        await conn.execute(text("INSERT INTO scenes(id,chapter_id,title,content,sort) VALUES(1,1,'旧景','原文',0)"))
        await conn.execute(text("INSERT INTO chapter_versions(id,chapter_id,seq,source,content,word_count) "
                                "VALUES(1,1,1,'manual','原文',2)"))

    async def check_upgrade(conn):
        row = (await conn.execute(text("SELECT generation_claim_key,generation_claim_task_key,"
                                       "generation_claim_prev_status FROM chapters WHERE id=2"))).one()
        assert tuple(row) == (None, None, None)
        assert await conn.scalar(text("SELECT content FROM scenes WHERE id=1")) == "原文"
        assert await conn.scalar(text("SELECT content FROM chapter_versions WHERE id=1")) == "原文"
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0046"

    async def violate_shape(conn):
        await conn.execute(text("UPDATE chapters SET generation_claim_key=:key WHERE id=1"), {"key": str(uuid4())})

    async def add_claim(conn):
        await conn.execute(text("UPDATE chapters SET status='generating',generation_claim_key=:key,"
                                "generation_claim_task_key=:task,generation_claim_prev_status='draft' WHERE id=1"),
                           {"key": str(uuid4()), "task": str(uuid4())})

    async def clear_claim(conn):
        await conn.execute(text("UPDATE chapters SET status='draft',generation_claim_key=NULL,"
                                "generation_claim_task_key=NULL,generation_claim_prev_status=NULL WHERE id=1"))

    async def clear_legacy(conn):
        await conn.execute(text("UPDATE chapters SET status='done' WHERE id=2"))

    async def check_downgrade(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0045"
        assert await conn.scalar(text("SELECT content FROM scenes WHERE id=1")) == "原文"
        assert await conn.scalar(text("SELECT content FROM chapter_versions WHERE id=1")) == "原文"
        assert await conn.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_schema=:schema "
                                      "AND table_name='chapters' AND column_name LIKE 'generation_claim_%'"),
                                 {"schema": schema}) == 0

    config = _config(tmp_path / "unused.db")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    created = False
    try:
        asyncio.run(run(create_schema, isolated=False))
        created = True
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", isolated_factory)
        command.upgrade(config, "0045")
        asyncio.run(run(seed))
        command.upgrade(config, "0046")
        asyncio.run(run(check_upgrade))
        with pytest.raises(IntegrityError):
            asyncio.run(run(violate_shape))
        asyncio.run(run(add_claim))
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0045")
        asyncio.run(run(clear_claim))
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0045")
        asyncio.run(run(clear_legacy))
        command.downgrade(config, "0045")
        asyncio.run(run(check_downgrade))
    finally:
        if created:
            asyncio.run(run(drop_schema, isolated=False))


async def _batch(maker, *, actor_id, project_id, grant=None):
    queued_at = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    execution_key = str(uuid4())
    async with maker() as db:
        chapter = Chapter(project_id=project_id, title="章", content="章节原文", status="draft")
        db.add(chapter)
        await db.flush()
        payload = {"request": {"project_id": project_id}, "chapter_ids": [chapter.id]}
        if grant:
            payload["_collaboration_grant"] = {"invitation_id": grant[0], "revision": grant[1]}
        task = Task(user_id=actor_id, project_id=project_id, kind="chapter.batch", status="running",
                    resource_key=f"batch:{project_id}", queued_at=queued_at, execution_key=execution_key,
                    input=payload, checkpoint={"phase": "generate", "current": None, "items": []})
        db.add(task)
        await db.flush()
        identity = {"task_id": task.id, "task_key": task.statistics_key, "actor_id": actor_id,
                    "project_id": project_id, "chapter_id": chapter.id, "queued_at": queued_at,
                    "grant_identity": grant}
        await db.commit()
    return identity, execution_key


async def test_postgresql_old_cleanup_waits_and_cannot_release_new_batch_attempt(postgres_chapter_apps):
    app, _ = postgres_chapter_apps
    maker = app.state.sessionmaker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        actor = await register(client, "pg-chapter-owner")
        project = await create_project(client)
    identity, execution_key = await _batch(maker, actor_id=actor["id"], project_id=project["id"])
    async with maker() as db:
        first = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution_key)
        await db.commit()

    async def stale_cleanup():
        async with maker() as db:
            result = await chapter_claim.release_chapter_claim(db, first)
            await db.commit()
            return result

    pending = None
    try:
        async with maker() as holder:
            await project_access.require_project(holder, identity["actor_id"], identity["project_id"], "ai.use",
                                                 for_update=True, visibility_write=True)
            assert await chapter_claim.release_chapter_claim(holder, first)
            second = await chapter_claim.claim_batch_chapter(holder, **identity, execution_key=execution_key)
            assert first.claim_key != second.claim_key
            pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(stale_cleanup())
            await asyncio.wait_for(_wait_for_blocker(maker, pid), 6)
            await holder.commit()
        assert await asyncio.wait_for(pending, 6) is False
        async with maker() as db:
            chapter = await db.get(Chapter, identity["chapter_id"])
            assert (chapter.status, chapter.content, chapter.generation_claim_key) == (
                "generating", "章节原文", second.claim_key,
            )
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


async def test_postgresql_claim_rechecks_revoke_after_waiting_on_project(postgres_chapter_apps):
    owner_app, guest_app = postgres_chapter_apps
    maker = owner_app.state.sessionmaker
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, _ = await _setup(owner, guest)
    async with maker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
        ))
        grant = (member.invitation_id, member.revision)
    identity, execution_key = await _batch(maker, actor_id=user["id"], project_id=project["id"], grant=grant)

    async def claim():
        async with maker() as db:
            with pytest.raises(AppError):
                await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution_key)
            await db.commit()

    pending = None
    try:
        async with maker() as holder:
            await project_lifecycle.lock_record(holder, project["id"])
            await holder.execute(update(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
            ).values(status="revoked", revision=ProjectCollaborator.revision + 1))
            pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(claim())
            await asyncio.wait_for(_wait_for_blocker(maker, pid), 6)
            await holder.commit()
        await asyncio.wait_for(pending, 6)
        async with maker() as db:
            chapter = await db.get(Chapter, identity["chapter_id"])
            task = await db.get(Task, identity["task_id"])
            assert (chapter.status, chapter.content, chapter.generation_claim_key) == ("draft", "章节原文", None)
            assert task.checkpoint["current"] is None
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
