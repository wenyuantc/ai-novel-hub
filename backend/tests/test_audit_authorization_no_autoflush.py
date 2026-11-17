"""审计初始化不得在人工授权前 flush，初始化异常不泄露 SQL。"""


import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ForbiddenError
from app.models import Chapter, Project, User
from app.schemas.chapter import ChapterUpdate
from app.services import chapter_service
from app.services.agent import transactions
from app.services.agent.tools import ToolCtx, get_tool
from app.services.agent.tools.base import TransactionSession
from app.services.audit_writer import AuditOperation, AuditOperationError
from tests.helpers import create_project, register


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
