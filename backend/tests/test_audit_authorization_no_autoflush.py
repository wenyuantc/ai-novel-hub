"""审计初始化不得在人工授权前 flush，初始化异常不泄露 SQL。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ForbiddenError, NotFoundError
from app.models import Chapter, Project, ProjectCollaborator, User
from app.schemas.chapter import ChapterUpdate
from app.services import chapter_service, project_lifecycle
from app.services.agent import transactions
from app.services.agent.tools import ToolCtx, get_tool
from app.services.agent.tools.base import TransactionSession
from app.services.audit_writer import AuditOperation, AuditOperationError
from tests.helpers import create_project, register
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


@pytest.mark.parametrize("enabled", [False, True])
async def test_admin_audit_entry_does_not_flush_pending_content(app, client, system_admin, enabled):
    await register(client, "audit-init-owner")
    project = await create_project(client)
    admin_id = (await system_admin.get("/api/v1/auth/me")).json()["id"]
    async with app.state.sessionmaker() as db:
        pending = Chapter(project_id=project["id"], title="审计初始化前暂存")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        async with AuditOperation(
            db, admin_id, "project.update", "project", project["id"], project_id=project["id"], enabled=enabled,
        ) as audit:
            assert audit.enabled is enabled
            if enabled:
                assert audit.values["owner_username"] == "audit-init-owner"
            assert flushes == [] and pending.id is None and pending in db.new
        assert db.autoflush
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Chapter).where(
            Chapter.project_id == project["id"], Chapter.title == "审计初始化前暂存",
        )) == 0


class _SecretDatabaseError(Exception):
    sqlstate = "40001"


@pytest.mark.parametrize("serialization", [False, True])
async def test_audit_initialization_database_error_is_safe_and_classified(
    app, client, monkeypatch, caplog, serialization,
):
    actor = await register(client, "audit-error-owner")
    project = await create_project(client)
    original_get = AsyncSession.get
    secret = "SQL-PARAMS-PRIVATE-CONTENT"

    async def fail_actor_lookup(db, model, ident, **kwargs):
        if model is User and ident == actor["id"]:
            original = _SecretDatabaseError(secret) if serialization else RuntimeError(secret)
            raise DBAPIError("SELECT secret FROM users", {"private": secret}, original)
        return await original_get(db, model, ident, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", fail_actor_lookup)
    async with app.state.sessionmaker() as db:
        expected = DBAPIError if serialization else AuditOperationError
        with pytest.raises(expected) as error:
            await chapter_service.update_chapter(
                db, actor["id"], project["id"], 1, ChapterUpdate(title="不应写入"), manual=True,
            )
        assert secret not in str(error.value) and secret not in caplog.text
        assert error.value.__context__ is None and error.value.__cause__ is None
        if serialization:
            assert transactions.is_serialization_error(error.value)
        await db.rollback()


@pytest.mark.parametrize("failed_lookup", ["project", "owner"])
async def test_admin_audit_project_snapshot_errors_are_safe(
    app, client, system_admin, monkeypatch, caplog, failed_lookup,
):
    await register(client, "audit-snapshot-owner")
    project = await create_project(client)
    admin_id = (await system_admin.get("/api/v1/auth/me")).json()["id"]
    original_get, original_scalar = AsyncSession.get, AsyncSession.scalar
    secret = "ADMIN-SNAPSHOT-PRIVATE-DATA"

    async def fail_project_lookup(db, model, ident, **kwargs):
        if failed_lookup == "project" and model is Project and ident == project["id"]:
            raise DBAPIError("SELECT project_secret", {"private": secret}, RuntimeError(secret))
        return await original_get(db, model, ident, **kwargs)

    async def fail_owner_lookup(db, statement, *args, **kwargs):
        if failed_lookup == "owner" and str(statement).startswith("SELECT users.username"):
            raise DBAPIError("SELECT owner_secret", {"private": secret}, RuntimeError(secret))
        return await original_scalar(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", fail_project_lookup)
    monkeypatch.setattr(AsyncSession, "scalar", fail_owner_lookup)
    async with app.state.sessionmaker() as db:
        with pytest.raises(AuditOperationError) as error:
            await chapter_service.update_chapter(
                db, admin_id, project["id"], 1, ChapterUpdate(title="不应写入"), manual=True,
            )
        assert secret not in str(error.value) and secret not in caplog.text
        assert error.value.__context__ is None and error.value.__cause__ is None
        await db.rollback()


async def test_audit_initialization_preserves_normal_application_errors(app, client, monkeypatch):
    actor = await register(client, "audit-app-error-owner")
    project = await create_project(client)
    original_get = AsyncSession.get

    async def forbidden(db, model, ident, **kwargs):
        if model is User and ident == actor["id"]:
            raise ForbiddenError("已有业务错误")
        return await original_get(db, model, ident, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", forbidden)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ForbiddenError, match="已有业务错误"):
            await chapter_service.update_chapter(
                db, actor["id"], project["id"], 1, ChapterUpdate(title="不应写入"), manual=True,
            )


async def test_agent_tool_40001_keeps_safe_conflict_and_nested_transaction(app, client, monkeypatch):
    actor = await register(client, "agent-audit-owner")
    project = await create_project(client)
    response = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "原始章节"})
    assert response.status_code == 201, response.text
    chapter_id = response.json()["id"]
    original_get = AsyncSession.get
    secret = "AGENT-PRIVATE-SQL-CONTENT"

    async def fail_actor_lookup(db, model, ident, **kwargs):
        if model is User and ident == actor["id"]:
            raise DBAPIError("SELECT secret FROM users", {"private": secret}, _SecretDatabaseError(secret))
        return await original_get(db, model, ident, **kwargs)

    async with app.state.sessionmaker() as db:
        record = await db.get(Project, project["id"])
        ctx = ToolCtx(db, record, actor["id"], app.state.task_engine.ctx, visibility_locked=True)
        monkeypatch.setattr(AsyncSession, "get", fail_actor_lookup)
        with pytest.raises(DBAPIError) as error:
            await get_tool("update_chapter_meta").execute(ctx, {"id": chapter_id, "title": "未提交"})
        assert transactions.is_serialization_error(error.value)
        assert secret not in str(error.value) and error.value.__context__ is None
        with pytest.raises(AppError, match=transactions.CONCURRENT_CHANGE):
            await transactions.rollback_conflict(db, error.value)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter_id)).title == "原始章节"


async def test_agent_catches_error_only_after_savepoint_discards_inner_work(app, client, monkeypatch):
    actor = await register(client, "agent-savepoint-owner")
    project = await create_project(client)
    response = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "已有章节"})
    assert response.status_code == 201, response.text
    original_get = AsyncSession.get

    async def fail_actor_lookup(db, model, ident, **kwargs):
        if model is User and ident == actor["id"]:
            db.add(Chapter(project_id=project["id"], title="内层不得提交"))
            raise ForbiddenError("权限已失效")
        return await original_get(db, model, ident, **kwargs)

    async with app.state.sessionmaker() as db:
        record = await db.get(Project, project["id"])
        db.add(Chapter(project_id=project["id"], title="外层已授权工作"))
        await db.flush()
        ctx = ToolCtx(db, record, actor["id"], app.state.task_engine.ctx, visibility_locked=True)
        monkeypatch.setattr(AsyncSession, "get", fail_actor_lookup)
        result = await get_tool("update_chapter_meta").execute(ctx, {"id": response.json()["id"], "title": "越权"})
        assert result == {"error": "权限已失效"}
        await db.commit()
    async with app.state.sessionmaker() as db:
        titles = set(await db.scalars(select(Chapter.title).where(Chapter.project_id == project["id"])))
        assert titles == {"已有章节", "外层已授权工作"}


async def test_proxy_savepoint_failure_preserves_only_outer_work(app, client, make_client):
    await register(client, "savepoint-owner")
    guest = make_client()
    viewer = await register(guest, "savepoint-viewer")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = await client.post(f"{base}/chapters", json={"title": "已有章节"})
    assert chapter.status_code == 201, chapter.text
    invitation = await client.post(f"{base}/collaborators", json={"username": "savepoint-viewer", "role": "viewer"})
    assert invitation.status_code == 201, invitation.text
    accept = f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept"
    assert (await guest.post(accept, json={"expected_revision": 1})).status_code == 200
    async with app.state.sessionmaker() as db:
        db.add(Chapter(project_id=project["id"], title="外层已有工作"))
        flushes = []
        with pytest.raises(ForbiddenError):
            async with db.begin_nested():
                db.add(Chapter(project_id=project["id"], title="内层未经授权工作"))
                event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
                await chapter_service.update_chapter(
                    TransactionSession(db), viewer["id"], project["id"], chapter.json()["id"],
                    ChapterUpdate(title="不得修改"), manual=True,
                )
        assert flushes == []
        await db.commit()
    async with app.state.sessionmaker() as db:
        titles = set(await db.scalars(select(Chapter.title).where(Chapter.project_id == project["id"])))
        assert titles == {"已有章节", "外层已有工作"}


async def test_postgres_revoke_before_manual_chapter_service_does_not_flush(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        response = await owner.post(f"{base}/chapters", json={"title": "原始章节"})
        assert response.status_code == 201, response.text
        chapter_id = response.json()["id"]
        task = None
        try:
            async with owner_app.state.sessionmaker() as holder, guest_app.state.sessionmaker() as writer:
                await project_lifecycle.lock_active(holder, project["id"])
                member = await holder.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
                ))
                member.status, member.revision = "revoked", member.revision + 1
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = Chapter(project_id=project["id"], title="撤权后不得写入")
                writer.add(pending)
                flushes = []
                event.listen(writer.sync_session, "before_flush", lambda *_: flushes.append(True))
                task = asyncio.create_task(chapter_service.update_chapter(
                    writer, user["id"], project["id"], chapter_id, ChapterUpdate(title="撤权后修改"), manual=True,
                ))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                async with guest_app.state.sessionmaker() as observer:
                    queries = (await observer.scalars(text(
                        "SELECT query FROM pg_stat_activity WHERE datname=current_database() "
                        "AND :holder=ANY(pg_blocking_pids(pid))"
                    ), {"holder": pid})).all()
                    assert any("update projects" in query.lower() for query in queries), queries
                assert flushes == [] and pending.id is None
                await holder.commit()
                with pytest.raises(NotFoundError):
                    await asyncio.wait_for(task, 6)
                assert flushes == [] and pending.id is None and pending in writer.new
                await writer.rollback()
            async with owner_app.state.sessionmaker() as db:
                assert (await db.get(Chapter, chapter_id)).title == "原始章节"
                assert await db.scalar(select(func.count()).select_from(Chapter).where(
                    Chapter.project_id == project["id"], Chapter.title == "撤权后不得写入",
                )) == 0
        finally:
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
