import asyncio
import sqlite3
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import AgentChange, AnalysisChange, Chapter, ChapterAnalysis, OrganizationMember, Project, User
from app.schemas.transfer import BackupOptions
from app.services import admin_service, organization_service, project_lifecycle, project_service, recycle_bin_service
from app.services.agent import effects
from app.services.agent.tools.base import TransactionSession, lock_project
from app.services.project_transfer import exporter
from tests.helpers import create_project, register
from tests.test_project_transfer import _seed


async def test_stale_identity_map_cannot_reopen_deleted_project(app, client):
    owner = await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as old:
        await old.get(Project, project["id"])
        async with app.state.sessionmaker() as db:
            (await db.get(Project, project["id"])).deleted_at = project_lifecycle.now_utc()
            await db.commit()
        with pytest.raises(NotFoundError):
            await project_service.get_owned_project(old, owner["id"], project["id"])


async def test_accepted_content_write_finishes_before_delete_barrier(app, client, monkeypatch):
    owner = await register(client)
    project = await create_project(client)
    pid = project["id"]
    chapter = (await client.post(f"/api/v1/projects/{pid}/chapters", json={"content": "原文"})).json()
    attempted = asyncio.Event()
    original = project_lifecycle.lock_active
    deletion = None

    async def observe(db, target):
        if asyncio.current_task() is deletion:
            attempted.set()
        return await original(db, target)

    monkeypatch.setattr(project_lifecycle, "lock_active", observe)
    async with app.state.sessionmaker() as writer:
        await project_service.get_owned_project(writer, owner["id"], pid, lock=True)
        row = await writer.get(Chapter, chapter["id"])
        row.content, row.word_count = "屏障前提交", 5
        deletion = asyncio.create_task(client.delete(f"/api/v1/projects/{pid}"))
        await asyncio.wait_for(attempted.wait(), 3)
        assert not deletion.done()
        await writer.commit()
    assert (await asyncio.wait_for(deletion, 5)).status_code == 204
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter["id"])).content == "屏障前提交"
        assert (await db.get(Project, pid)).deleted_at is not None


async def test_database_restrict_prevents_raw_user_cascade(app, client):
    owner = await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(User, owner["id"]))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
    assert (await client.get(f"/api/v1/projects/{project['id']}")).status_code == 200


async def test_concurrent_project_creation_after_empty_count_blocks_user_deletion(
    app, client, recycle_admin, monkeypatch
):
    """故障注入遗漏父User屏障时，count后并发创建仍由RESTRICT兜底，不级联新作品。"""
    owner = await register(client)
    counted, release = asyncio.Event(), asyncio.Event()
    original = admin_service._flush_user_delete
    actual_execute = AsyncSession.execute
    deletion = None
    bypassed = []

    async def omit_parent_barrier(db, statement, *args, **kwargs):
        if (asyncio.current_task() is deletion and getattr(statement, "is_update", False)
                and statement.table.name == User.__tablename__):
            # 仅本例绕过新增真实写锁，保留原RESTRICT末层防御的全部断言。
            bypassed.append(True)
            return await actual_execute(db, select(User.id).where(User.id == owner["id"]))
        return await actual_execute(db, statement, *args, **kwargs)

    async def before_flush(db):
        counted.set()
        await release.wait()
        await original(db)

    monkeypatch.setattr(admin_service, "_flush_user_delete", before_flush)
    monkeypatch.setattr(AsyncSession, "execute", omit_parent_barrier)
    deletion = asyncio.create_task(recycle_admin.delete(f"/api/v1/admin/users/{owner['id']}"))
    try:
        await asyncio.wait_for(counted.wait(), 3)
        project = await create_project(client, title="计数后并发创建")
    finally:
        release.set()
    result = await asyncio.wait_for(deletion, 5)
    assert bypassed == [True]
    assert result.status_code == 409 and result.json()["code"] == "user_has_projects"
    assert (await client.get(f"/api/v1/projects/{project['id']}")).status_code == 200
    async with app.state.sessionmaker() as db:
        assert await db.get(User, owner["id"]) is not None
        assert await db.get(Project, project["id"]) is not None


async def test_user_delete_real_parent_barrier_serializes_concurrent_project_insert(
    app, client, recycle_admin, monkeypatch
):
    """真实User写屏障先赢时，插入等待删除提交，再由FK拒绝，不生成孤儿作品。"""
    owner = await register(client)
    counted, release, inserting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = admin_service._flush_user_delete

    async def before_flush(db):
        counted.set()
        await release.wait()
        await original(db)

    async def create_after_count():
        async with app.state.sessionmaker() as db:
            try:
                await db.execute(insert(Project).values(user_id=owner["id"], title="父屏障后的并发插入"))
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                assert str(exc.orig) == "FOREIGN KEY constraint failed"
                return "foreign_key_rejected"
            raise AssertionError("User删除已获真实写屏障，不能在删除提交后生成孤儿作品")

    def observe_insert(conn, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO projects"):
            inserting.set()

    monkeypatch.setattr(admin_service, "_flush_user_delete", before_flush)
    deletion = asyncio.create_task(recycle_admin.delete(f"/api/v1/admin/users/{owner['id']}"))
    creator = None
    event.listen(app.state.engine.sync_engine, "before_cursor_execute", observe_insert)
    try:
        await asyncio.wait_for(counted.wait(), 3)
        creator = asyncio.create_task(create_after_count())
        await asyncio.wait_for(inserting.wait(), 3)
        assert not creator.done()
        release.set()
        assert (await asyncio.wait_for(deletion, 5)).status_code == 204
        assert await asyncio.wait_for(creator, 5) == "foreign_key_rejected"
        async with app.state.sessionmaker() as db:
            assert await db.get(User, owner["id"]) is None
            assert await db.scalar(select(Project.id).where(Project.user_id == owner["id"])) is None
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", observe_insert)
        release.set()
        await asyncio.gather(deletion, *([creator] if creator is not None else []), return_exceptions=True)


async def test_user_delete_does_not_translate_unrelated_sqlite_trigger_failures():
    original = sqlite3.IntegrityError("非外键触发器错误")
    original.sqlite_errorcode = 1811
    failure = IntegrityError("DELETE", {}, original)
    db = AsyncMock()
    db.flush.side_effect = failure
    with pytest.raises(IntegrityError) as caught:
        await admin_service._flush_user_delete(db)
    assert caught.value is failure


async def test_restart_clears_only_pending_intents_without_undeleting_trash(app, client):
    await register(client)
    pending = await create_project(client)
    trashed = await create_project(client, title="已删除")
    assert (await client.delete(f"/api/v1/projects/{trashed['id']}")).status_code == 204
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, pending["id"])
        row.deletion_started_at, row.deletion_token = project_lifecycle.now_utc() - timedelta(hours=1), "old-process"
        await db.commit()
    await recycle_bin_service.recover_pending_deletions(app.state.sessionmaker)
    await app.state.task_engine.start()
    async with app.state.sessionmaker() as db:
        rows = {p.id: p for p in await db.scalars(select(Project))}
        assert rows[pending["id"]].deletion_started_at is None
        assert rows[pending["id"]].deletion_token is None
        assert rows[trashed["id"]].deleted_at is not None


async def test_repeated_write_locks_preserve_unflushed_project_scalars_and_json(app, client):
    owner = await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        row = await project_service.get_owned_project(db, owner["id"], project["id"], lock=True)
        row.title = "事务内的新书名"
        row.wizard_progress = {"done": ["world"], "current": "characters"}
        row.reading_progress = {"chapter_id": 42, "ratio": 0.2}
        await lock_project(TransactionSession(db), project["id"], owner["id"])
        again = await project_service.get_owned_project(db, owner["id"], project["id"], lock=True)
        assert again is row and row.title == "事务内的新书名"
        assert row.wizard_progress["done"] == ["world"]
        await db.commit()
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        assert row.title == "事务内的新书名"
        assert row.wizard_progress["current"] == "characters"
        assert row.reading_progress["chapter_id"] == 42
        snapshot = await effects.snapshot(db, project["id"])
        assert not project_lifecycle.LIFECYCLE_FIELDS.intersection(snapshot["projects"][str(project["id"])])


async def test_direct_analysis_and_agent_ids_cannot_read_or_write_trash(app, client):
    await register(client)
    project = await create_project(client)
    pid = project["id"]
    await _seed(app, pid)
    conversation = (await client.post(f"/api/v1/projects/{pid}/agent/conversations")).json()
    async with app.state.sessionmaker() as db:
        analysis_id = await db.scalar(select(ChapterAnalysis.id).where(ChapterAnalysis.project_id == pid))
        change_id = await db.scalar(select(AnalysisChange.id).where(AnalysisChange.project_id == pid))
        agent_change = AgentChange(
            conversation_id=conversation["id"],
            project_id=pid,
            tool_call_id="direct-id",
            tool_name="update_project",
            kind="small",
            arguments={},
            status="executed",
        )
        db.add(agent_change)
        await db.commit()
        agent_id = agent_change.id
    assert (await client.delete(f"/api/v1/projects/{pid}")).status_code == 204
    assert (await client.get(f"/api/v1/analyses/{analysis_id}")).status_code == 404
    for suffix in ("confirm", "reject", "revert"):
        assert (await client.post(f"/api/v1/analysis-changes/{change_id}/{suffix}")).status_code == 404
    base = f"/api/v1/projects/{pid}/agent"
    assert (await client.get(f"{base}/conversations/{conversation['id']}")).status_code == 404
    assert (await client.patch(f"{base}/conversations/{conversation['id']}", json={"title": "禁止"})).status_code == 404
    for suffix in ("confirm", "reject", "undo"):
        assert (await client.post(f"{base}/changes/{agent_id}/{suffix}")).status_code == 404


@pytest.mark.parametrize("operation", ["add", "remove"])
async def test_membership_writes_hold_parent_barrier_until_commit(app, client, monkeypatch, operation):
    await register(client)
    project = await create_project(client)
    pid = project["id"]
    base = f"/api/v1/projects/{pid}"
    character = (await client.post(f"{base}/characters", json={"name": "角色"})).json()
    organization = (await client.post(f"{base}/organizations", json={"name": "组织"})).json()
    endpoint = f"{base}/organizations/{organization['id']}/members"
    if operation == "remove":
        member = (await client.post(endpoint, json={"character_id": character["id"]})).json()
        endpoint += f"/{member['id']}"
    authorized, release, deleting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_get = organization_service.get_owned_org
    original_lock = project_lifecycle.lock_active
    mutation = deletion = None

    async def get_org(*args, **kwargs):
        result = await original_get(*args, **kwargs)
        if asyncio.current_task() is mutation:
            authorized.set()
            await release.wait()
        return result

    async def parent_lock(db, project_id):
        if asyncio.current_task() is deletion:
            deleting.set()
        return await original_lock(db, project_id)

    monkeypatch.setattr(organization_service, "get_owned_org", get_org)
    monkeypatch.setattr(project_lifecycle, "lock_active", parent_lock)
    mutation = asyncio.create_task(
        client.request(
            "POST" if operation == "add" else "DELETE",
            endpoint,
            **({"json": {"character_id": character["id"]}} if operation == "add" else {}),
        )
    )
    try:
        await asyncio.wait_for(authorized.wait(), 3)
        deletion = asyncio.create_task(client.delete(base))
        await asyncio.wait_for(deleting.wait(), 3)
        assert not deletion.done()
    finally:
        release.set()
    assert (await mutation).status_code == (201 if operation == "add" else 204)
    assert (await asyncio.wait_for(deletion, 5)).status_code == 204
    async with app.state.sessionmaker() as db:
        members = list(
            await db.scalars(select(OrganizationMember).where(OrganizationMember.organization_id == organization["id"]))
        )
        assert len(members) == (1 if operation == "add" else 0)


async def test_backup_stream_stops_if_project_is_deleted_between_chunks(app, client):
    owner = await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        plan = await exporter.prepare_export(db, owner["id"], project["id"], BackupOptions())
    stream = exporter.stream_export(app.state.sessionmaker, plan)
    first = await anext(stream)
    assert b'"project"' in first
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    with pytest.raises(NotFoundError):
        await anext(stream)
    await stream.aclose()
