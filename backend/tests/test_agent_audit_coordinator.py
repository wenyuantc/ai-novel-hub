"""Agent 保存点审计：预览、暂存成功与根事务事实不能混淆。"""

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from app.core.errors import ForbiddenError, NotFoundError, UnauthorizedError
from app.models import AdminAuditLog, Chapter, Project
from app.schemas.auth import Credentials
from app.schemas.chapter import ChapterUpdate
from app.services import auth_service, chapter_service, project_lifecycle
from app.services.agent import transactions
from app.services.agent.tools.base import EmptyArgs, Tool, ToolCtx, TransactionSession
from app.services.audit_writer import AuditOperation, AuditOperationError
from tests.helpers import create_project, register

pytest_plugins = ("tests.test_agent_authenticated_actor",)


async def _seed(app, client, admin):
    await register(client, "agent-audit-owner")
    project = await create_project(client)
    manager = (await admin.get("/api/v1/auth/me")).json()["id"]
    return manager, project["id"], project["title"]


async def _write_project(proxy, manager, project_id, operation_id, *, failure=False):
    async with AuditOperation(
        proxy, manager, "project.update", "project", project_id, project_id=project_id, operation_id=operation_id,
    ) as audit:
        project = await proxy.get(Project, project_id)
        audit.target(project)
        project.title = "未经提交的内容"
        if failure:
            raise ForbiddenError("PRIVATE-NOT-FOR-AUDIT")
        await audit.commit()


async def _events(app, operation_id):
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(AdminAuditLog).where(AdminAuditLog.operation_id == operation_id)))


async def _check_preview_and_execute(app, client, admin):
    manager, project_id, original = await _seed(app, client, admin)
    preview_id, execute_id = str(uuid4()), str(uuid4())
    coordinator = transactions.AgentAuditCoordinator()
    async with app.state.sessionmaker() as db:
        await project_lifecycle.lock_record(db, project_id)
        nested = await db.begin_nested()
        preview = TransactionSession(db, audit_coordinator=coordinator, audit_mode="preview")
        await _write_project(preview, manager, project_id, preview_id)
        await nested.rollback()
        await coordinator.flush_failures(db)
        await db.commit()
        coordinator.committed()
    assert await _events(app, preview_id) == []
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project_id)).title == original

    async with app.state.sessionmaker() as db:
        await project_lifecycle.lock_record(db, project_id)
        async with db.begin_nested():
            execute = TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute")
            await _write_project(execute, manager, project_id, execute_id)
        await coordinator.flush_failures(db)
        await db.commit()
        coordinator.committed()
    rows = await _events(app, execute_id)
    assert len(rows) == 1 and rows[0].result == "success"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project_id)).title == "未经提交的内容"


async def test_preview_discards_success_and_execute_commits_atomically(app, client, system_admin):
    await _check_preview_and_execute(app, client, system_admin)


async def _accept_editor(owner, admin, project_id):
    """管理员兼 accepted editor：Agent 写入只认原邀请轮次，不认管理员人工来源。"""
    invitation = await owner.post(
        f"/api/v1/projects/{project_id}/collaborators", json={"username": "settings_admin", "role": "editor"},
    )
    assert invitation.status_code == 201, invitation.text
    accepted = await admin.post(
        f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept",
        json={"expected_revision": invitation.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return invitation.json()["invitation_id"], accepted.json()["revision"]


async def test_tool_propagates_explicit_preview_and_execute_scopes(app, client, system_admin):
    manager, project_id, original = await _seed(app, client, system_admin)
    grant = await _accept_editor(client, system_admin, project_id)
    operation_ids = iter((str(uuid4()), str(uuid4())))
    ids = []
    coordinator = transactions.AgentAuditCoordinator()

    async def operation(ctx, _args):
        operation_id = next(operation_ids)
        ids.append(operation_id)
        await _write_project(ctx.db, manager, project_id, operation_id)
        return {"ok": True}

    tool = Tool("test_audit", "测试", EmptyArgs, "small", operation, preview_operation=operation)
    async with app.state.sessionmaker() as db:
        project = await project_lifecycle.lock_record(db, project_id)
        ctx = ToolCtx(db, project, manager, app.state.task_engine.ctx, visibility_locked=True,
                      audit_coordinator=coordinator, grant=grant)
        assert await tool.preview(ctx, {}) == {"ok": True}
        assert project.title == original
        await coordinator.flush_failures(db)
        await db.commit()
        coordinator.committed()
        assert await _events(app, ids[0]) == []
        await project_lifecycle.lock_record(db, project_id)
        assert await tool.execute(ctx, {}) == {"ok": True}
        await coordinator.flush_failures(db)
        await db.commit()
        coordinator.committed()
    rows = await _events(app, ids[1])
    assert len(rows) == 1 and rows[0].result == "success"


async def test_nested_failure_survives_both_savepoint_rollbacks(app, client, system_admin):
    manager, project_id, original = await _seed(app, client, system_admin)
    operation_id = str(uuid4())
    coordinator = transactions.AgentAuditCoordinator()
    async with app.state.sessionmaker() as db:
        # 保存点外合法工作不可因内层拒绝丢失。
        db.add(Chapter(project_id=project_id, title="外层合法工作"))
        await db.flush()
        outer = await db.begin_nested()
        with pytest.raises(ForbiddenError):
            async with db.begin_nested():
                proxy = TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute")
                await _write_project(proxy, manager, project_id, operation_id, failure=True)
        await outer.rollback()
        await coordinator.flush_failures(db)
        await db.commit()
        coordinator.committed()
    rows = await _events(app, operation_id)
    assert len(rows) == 1 and rows[0].result == "failure" and rows[0].error_code == "forbidden"
    assert "PRIVATE-NOT-FOR-AUDIT" not in str(rows[0].summary)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project_id)).title == original
        assert await db.scalar(select(Chapter.id).where(Chapter.title == "外层合法工作"))


async def test_rolled_back_staged_success_becomes_one_safe_failure(app, client, system_admin):
    manager, project_id, original = await _seed(app, client, system_admin)
    operation_id = str(uuid4())
    coordinator = transactions.AgentAuditCoordinator()
    async with app.state.sessionmaker() as db:
        await project_lifecycle.lock_record(db, project_id)
        async with db.begin_nested():
            proxy = TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute")
            await _write_project(proxy, manager, project_id, operation_id)
        outcomes = await coordinator.reconcile_after_failure(db, app.state.sessionmaker)
    assert outcomes[operation_id] == "failure"
    rows = await _events(app, operation_id)
    assert len(rows) == 1 and rows[0].result == "failure" and rows[0].error_code == "system_error"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project_id)).title == original


async def test_outer_savepoint_rollback_turns_staged_success_into_failure(app, client, system_admin):
    manager, project_id, original = await _seed(app, client, system_admin)
    operation_id = str(uuid4())
    coordinator = transactions.AgentAuditCoordinator()
    async with app.state.sessionmaker() as db:
        await project_lifecycle.lock_record(db, project_id)
        outer = await db.begin_nested()
        async with db.begin_nested():
            proxy = TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute")
            await _write_project(proxy, manager, project_id, operation_id)
        with pytest.raises(RuntimeError, match="保存点"):
            await coordinator.flush_failures(db)
        await outer.rollback()
        with pytest.raises(RuntimeError, match="提交前"):
            coordinator.committed()
        await coordinator.flush_failures(db)
        await db.commit()
        coordinator.committed()
    rows = await _events(app, operation_id)
    assert len(rows) == 1 and rows[0].result == "failure"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project_id)).title == original


async def test_expired_authentication_is_not_audited_as_administrator_action(app, client, system_admin):
    manager, project_id, _ = await _seed(app, client, system_admin)
    operation_id = str(uuid4())
    coordinator = transactions.AgentAuditCoordinator()
    async with app.state.sessionmaker() as db:
        nested = await db.begin_nested()
        proxy = TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute")
        with pytest.raises(UnauthorizedError):
            async with AuditOperation(
                proxy, manager, "project.update", "project", project_id,
                project_id=project_id, operation_id=operation_id,
            ):
                raise UnauthorizedError("登录已失效")
        await nested.rollback()
        await coordinator.reconcile_after_failure(db, app.state.sessionmaker)
    assert await _events(app, operation_id) == []


async def test_successful_commit_with_lost_ack_is_never_recorded_as_failure(app, client, system_admin):
    manager, project_id, _ = await _seed(app, client, system_admin)
    operation_id = str(uuid4())
    coordinator = transactions.AgentAuditCoordinator()
    async with app.state.sessionmaker() as db:
        await project_lifecycle.lock_record(db, project_id)
        async with db.begin_nested():
            proxy = TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute")
            await _write_project(proxy, manager, project_id, operation_id)
        await db.commit()  # 模拟真实提交完成后确认丢失，不能盲补失败。
        outcomes = await coordinator.reconcile_after_failure(db, app.state.sessionmaker)
    assert outcomes[operation_id] == "success"
    rows = await _events(app, operation_id)
    assert len(rows) == 1 and rows[0].result == "success"


@pytest.mark.parametrize("committed_before_error", [False, True])
async def test_root_commit_error_records_only_verified_transaction_fact(
    app, client, system_admin, monkeypatch, committed_before_error,
):
    manager, project_id, original = await _seed(app, client, system_admin)
    operation_id = str(uuid4())
    coordinator = transactions.AgentAuditCoordinator()
    async with app.state.sessionmaker() as db:
        await project_lifecycle.lock_record(db, project_id)
        async with db.begin_nested():
            proxy = TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute")
            await _write_project(proxy, manager, project_id, operation_id)
        real_commit = db.commit

        async def uncertain_commit():
            if committed_before_error:
                await real_commit()
            raise DBAPIError("PRIVATE-SQL", {"content": "PRIVATE-CONTENT"}, RuntimeError("PRIVATE-ERROR"))

        monkeypatch.setattr(db, "commit", uncertain_commit)
        with pytest.raises(DBAPIError):
            await coordinator.commit(db, app.state.sessionmaker)
        assert not coordinator.pending
    rows = await _events(app, operation_id)
    assert len(rows) == 1
    assert rows[0].result == ("success" if committed_before_error else "failure")
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project_id)).title == (
            "未经提交的内容" if committed_before_error else original
        )


async def test_repeated_cancellation_waits_for_failure_reconciliation(app, client, system_admin, monkeypatch):
    manager, project_id, _ = await _seed(app, client, system_admin)
    operation_id = str(uuid4())
    coordinator = transactions.AgentAuditCoordinator()
    entered, release = asyncio.Event(), asyncio.Event()
    original = transactions._record_failure

    async def gated(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(transactions, "_record_failure", gated)
    async with app.state.sessionmaker() as db:
        await project_lifecycle.lock_record(db, project_id)
        async with db.begin_nested():
            proxy = TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute")
            await _write_project(proxy, manager, project_id, operation_id)
        work = asyncio.create_task(coordinator.reconcile_after_failure(db, app.state.sessionmaker))
        await asyncio.wait_for(entered.wait(), 5)
        work.cancel()
        await asyncio.sleep(0)
        work.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(work, 5)
    rows = await _events(app, operation_id)
    assert len(rows) == 1 and rows[0].result == "failure"


async def test_unconfirmed_root_release_never_inserts_failure(app, client, system_admin, monkeypatch):
    manager, project_id, _ = await _seed(app, client, system_admin)
    operation_id = str(uuid4())
    coordinator = transactions.AgentAuditCoordinator()
    async with app.state.sessionmaker() as db:
        nested = await db.begin_nested()
        with pytest.raises(ForbiddenError):
            await _write_project(
                TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute"),
                manager, project_id, operation_id, failure=True,
            )
        await nested.rollback()
        await db.rollback()

    class UnknownSession:
        def in_nested_transaction(self):
            return False

        async def rollback(self):
            raise RuntimeError("PRIVATE-ROLLBACK")

        async def invalidate(self):
            raise RuntimeError("PRIVATE-INVALIDATE")

    async def must_not_insert(*_args):
        pytest.fail("事务结束未确认时不能补写失败审计")

    monkeypatch.setattr(transactions, "_record_failure", must_not_insert)
    with pytest.raises(transactions.AgentAuditReconciliationError) as failure:
        await coordinator.reconcile_after_failure(UnknownSession(), app.state.sessionmaker)
    assert "PRIVATE" not in str(failure.value) and failure.value.__context__ is None
    assert await _events(app, operation_id) == []


class _Sensitive40001(Exception):
    sqlstate = "40001"


@pytest.mark.parametrize("serialization", [False, True])
async def test_agent_body_database_error_is_safe_and_preserves_40001(app, client, system_admin, serialization):
    manager, project_id, _ = await _seed(app, client, system_admin)
    operation_id = str(uuid4())
    coordinator = transactions.AgentAuditCoordinator()
    async with app.state.sessionmaker() as db:
        expected = DBAPIError if serialization else AuditOperationError
        with pytest.raises(expected) as failure:
            async with db.begin_nested():
                proxy = TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute")
                async with AuditOperation(
                    proxy, manager, "project.update", "project", project_id,
                    project_id=project_id, operation_id=operation_id,
                ):
                    original = _Sensitive40001("secret") if serialization else RuntimeError("secret")
                    raise DBAPIError("SELECT PRIVATE-SQL", {"text": "PRIVATE-CONTENT"}, original)
        if serialization:
            assert transactions.is_serialization_error(failure.value)
        assert "PRIVATE" not in str(failure.value) and "secret" not in str(failure.value)
        assert failure.value.__cause__ is None and failure.value.__context__ is None
        await coordinator.reconcile_after_failure(db, app.state.sessionmaker)
    rows = await _events(app, operation_id)
    assert len(rows) == 1 and rows[0].error_code == "system_error"


@pytest.mark.parametrize("serialization", [False, True])
async def test_agent_database_error_discards_sensitive_exception_chain(app, serialization):
    original = _Sensitive40001("PRIVATE-ERROR") if serialization else RuntimeError("PRIVATE-ERROR")
    error = DBAPIError("SELECT PRIVATE-SQL", {"content": "PRIVATE-CONTENT"}, original)
    expected = transactions.ConflictError if serialization else transactions.AgentTransactionError
    async with app.state.sessionmaker() as db:
        with pytest.raises(expected) as failure:
            await transactions.rollback_conflict(db, error)
    assert "PRIVATE" not in str(failure.value)
    assert failure.value.__context__ is None and failure.value.__cause__ is None


async def test_unauthorized_nested_service_never_flushes_pending_content(app, client, system_admin):
    manager, project_id, _ = await _seed(app, client, system_admin)
    coordinator = transactions.AgentAuditCoordinator()
    async with app.state.sessionmaker() as db:
        nested = await db.begin_nested()
        pending = Chapter(project_id=project_id, title="不得提前写入")
        db.add(pending)
        proxy = TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute")
        with pytest.raises(NotFoundError):
            await chapter_service.update_chapter(
                proxy, manager, project_id, 2_000_000_000, ChapterUpdate(title="不存在"), manual=True,
            )
        assert pending.id is None
        # 失败的保存点由调用方回滚，不得把上述待写值一起提交。
        await nested.rollback()
        await coordinator.reconcile_after_failure(db, app.state.sessionmaker)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Chapter.id).where(Chapter.title == "不得提前写入")) is None


@pytest.mark.parametrize("method", ["run", "preview", "execute"])
async def test_agent_authorization_loss_is_not_an_ordinary_tool_error(app, client, method):
    user = await register(client, "agent-policy-owner")
    project = await create_project(client)

    async def denied(_ctx, _args):
        raise transactions.AgentAuthorizationChangedError()

    tool = Tool("test_fatal", "测试", EmptyArgs, "read" if method == "run" else "small", denied)
    async with app.state.sessionmaker() as db:
        row = await project_lifecycle.lock_record(db, project["id"])
        ctx = ToolCtx(db, row, user["id"], app.state.task_engine.ctx, visibility_locked=True)
        with pytest.raises(transactions.AgentAuthorizationChangedError):
            await getattr(tool, method)(ctx, {})


async def _postgres_admin(app, admin):
    async with app.state.sessionmaker() as db:
        await auth_service.create_user(
            db, Credentials(username="agent_manager", password="secret123"), is_admin=True,
        )
    response = await admin.post(
        "/api/v1/auth/login", json={"username": "agent_manager", "password": "secret123"},
    )
    assert response.status_code == 200, response.text


async def test_postgresql_preview_and_execute_transaction_facts(postgres_agent_app):
    transport = httpx.ASGITransport(app=postgres_agent_app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin,
    ):
        await _postgres_admin(postgres_agent_app, admin)
        await _check_preview_and_execute(postgres_agent_app, client, admin)


async def test_postgresql_nested_failure_is_invisible_until_root_commit(postgres_agent_app):
    transport = httpx.ASGITransport(app=postgres_agent_app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin,
    ):
        await _postgres_admin(postgres_agent_app, admin)
        manager, project_id, original = await _seed(postgres_agent_app, client, admin)
        operation_id = str(uuid4())
        coordinator = transactions.AgentAuditCoordinator()
        async with postgres_agent_app.state.sessionmaker() as writer:
            await project_lifecycle.lock_record(writer, project_id)
            outer = await writer.begin_nested()
            with pytest.raises(ForbiddenError):
                async with writer.begin_nested():
                    proxy = TransactionSession(writer, audit_coordinator=coordinator, audit_mode="execute")
                    await _write_project(proxy, manager, project_id, operation_id, failure=True)
            await outer.rollback()
            async with postgres_agent_app.state.sessionmaker() as observer:
                assert await observer.scalar(select(AdminAuditLog.id).where(
                    AdminAuditLog.operation_id == operation_id,
                )) is None
                with pytest.raises(DBAPIError) as error:
                    await observer.execute(select(Project.id).where(
                        Project.id == project_id,
                    ).with_for_update(nowait=True))
                assert transactions.is_serialization_error(error.value) is False
                assert getattr(error.value.orig, "sqlstate", None) == "55P03"
                await observer.rollback()
            await coordinator.flush_failures(writer)
            await writer.commit()
            coordinator.committed()
        events = await _events(postgres_agent_app, operation_id)
        assert len(events) == 1 and events[0].result == "failure" and events[0].error_code == "forbidden"
        async with postgres_agent_app.state.sessionmaker() as observer:
            assert (await observer.get(Project, project_id)).title == original


async def test_postgresql_real_serialization_conflict_keeps_safe_failure(postgres_agent_app):
    transport = httpx.ASGITransport(app=postgres_agent_app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin,
    ):
        await _postgres_admin(postgres_agent_app, admin)
        manager, project_id, _ = await _seed(postgres_agent_app, client, admin)
        operation_id = str(uuid4())
        coordinator = transactions.AgentAuditCoordinator()
        async with transactions.session(postgres_agent_app.state.sessionmaker) as writer:
            # RR 先固定旧快照，另一个真实连接提交后再更新同一行，触发真正的 40001。
            assert await writer.scalar(select(Project.id).where(Project.id == project_id)) == project_id
            async with postgres_agent_app.state.sessionmaker() as concurrent:
                await concurrent.execute(update(Project).where(Project.id == project_id).values(title="先提交"))
                await concurrent.commit()
            with pytest.raises(DBAPIError) as failure:
                async with writer.begin_nested():
                    proxy = TransactionSession(writer, audit_coordinator=coordinator, audit_mode="execute")
                    async with AuditOperation(
                        proxy, manager, "project.update", "project", project_id,
                        project_id=project_id, operation_id=operation_id,
                    ):
                        await proxy.execute(update(Project).where(Project.id == project_id).values(title="不得提交"))
            assert transactions.is_serialization_error(failure.value)
            assert failure.value.__context__ is None and failure.value.__cause__ is None
            assert "不得提交" not in str(failure.value)
            outcomes = await coordinator.reconcile_after_failure(writer, postgres_agent_app.state.sessionmaker)
        assert outcomes[operation_id] == "failure"
        events = await _events(postgres_agent_app, operation_id)
        assert len(events) == 1 and events[0].error_code == "system_error"
        async with postgres_agent_app.state.sessionmaker() as observer:
            assert (await observer.get(Project, project_id)).title == "先提交"
