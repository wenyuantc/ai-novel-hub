"""隔离 PostgreSQL 验证账号删除与协作任务受理的共同用户写屏障。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text

from app.core.errors import UnauthorizedError
from app.models import AdminAuditLog, Project, Task, User
from app.services import admin_service, project_access
from app.services.authorization_service import AuthenticatedActor
from tests import pg_support
from tests.helpers import create_project, register
from tests.test_collaboration_access_postgresql import _wait_for_blocker


@pytest.fixture
async def postgres_user_delete_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(
        "AINOVEL_COLLAB_USER_DELETE_POSTGRES_TEST_URL", "collab_user_delete", purpose="删号测试",
    )
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


async def _prepare(owner, guest, administrator, app):
    await register(owner, "delete-pg-owner")
    user = await register(guest, "delete-pg-guest")
    admin = await register(administrator, "delete-pg-admin")
    async with app.state.sessionmaker() as db:
        (await db.get(User, admin["id"])).is_admin = True
        await db.commit()
    project = await create_project(owner, title="不能泄露的作品")
    base = f"/api/v1/projects/{project['id']}"
    invitation = await owner.post(f"{base}/collaborators", json={"username": "delete-pg-guest", "role": "editor"})
    assert invitation.status_code == 201, invitation.text
    invite = invitation.json()
    accepted = await guest.post(f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept", json={
        "expected_revision": invite["revision"],
    })
    assert accepted.status_code == 200, accepted.text
    return user, project, accepted.json()


async def _create_shared_task(db, user, project, member):
    # 模拟未来受理方：原认证快照与项目权限在共享 User 写屏障内复验，任务同事务提交。
    # 当前公开的协作 AI 受理仍未开放，这个测试不替代端到端创建测试。
    db.info["authenticated_actor"] = AuthenticatedActor(user["id"], 0)
    await project_access.require_project(db, user["id"], project["id"], "ai.use", for_update=True)
    task = Task(user_id=user["id"], project_id=project["id"], kind="chapter.generate", status="queued",
                input={"_collaboration_grant": {
                    "invitation_id": member["invitation_id"], "revision": member["revision"],
                }})
    db.add(task)
    await db.flush()
    return task


async def test_task_commits_first_and_user_delete_sees_it(postgres_user_delete_apps):
    owner_app, guest_app = postgres_user_delete_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as admin,
    ):
        user, project, member = await _prepare(owner, guest, admin, owner_app)
        pending = None
        async with guest_app.state.sessionmaker() as db:
            task = await _create_shared_task(db, user, project, member)
            task_id = task.id
            holder = await db.scalar(text("SELECT pg_backend_pid()"))
            try:
                pending = asyncio.create_task(admin.delete(f"/api/v1/admin/users/{user['id']}"))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder), 15)
                await db.commit()
                response = await asyncio.wait_for(pending, 15)
                assert response.status_code == 409 and response.json()["code"] == "user_has_pending_tasks"
                assert "不能泄露的作品" not in response.text
                async with owner_app.state.sessionmaker() as verify:
                    assert await verify.get(User, user["id"]) is not None
                    assert (await verify.get(Task, task_id)).user_id == user["id"]
                    log = await verify.scalar(select(AdminAuditLog).where(
                        AdminAuditLog.action == "user.delete", AdminAuditLog.target_id == user["id"],
                    ))
                    assert log.result == "failure" and log.project_id is None
            finally:
                await db.rollback()
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending


async def test_user_delete_commits_first_and_old_task_admission_fails(postgres_user_delete_apps, monkeypatch):
    owner_app, guest_app = postgres_user_delete_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as admin,
    ):
        user, project, member = await _prepare(owner, guest, admin, owner_app)
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original_flush = admin_service._flush_user_delete

        async def hold_after_delete(db):
            await original_flush(db)
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()

        async def admit():
            async with guest_app.state.sessionmaker() as db:
                try:
                    await _create_shared_task(db, user, project, member)
                    await db.commit()
                    return "unexpectedly admitted"
                except UnauthorizedError:
                    await db.rollback()
                    return "authentication expired"

        monkeypatch.setattr(admin_service, "_flush_user_delete", hold_after_delete)
        deletion = asyncio.create_task(admin.delete(f"/api/v1/admin/users/{user['id']}"))
        pending = None
        try:
            await asyncio.wait_for(reached.wait(), 15)
            pending = asyncio.create_task(admit())
            await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, holder["pid"]), 15)
            release.set()
            assert (await asyncio.wait_for(deletion, 15)).status_code == 204
            assert await asyncio.wait_for(pending, 15) == "authentication expired"
            async with owner_app.state.sessionmaker() as verify:
                assert await verify.get(User, user["id"]) is None
                assert await verify.get(Project, project["id"]) is not None
                assert await verify.scalar(select(Task.id).where(Task.user_id == user["id"])) is None
                log = await verify.scalar(select(AdminAuditLog).where(
                    AdminAuditLog.action == "user.delete", AdminAuditLog.target_id == user["id"],
                ))
                assert log.result == "success" and log.project_id is None
        finally:
            release.set()
            for job in (deletion, pending):
                if job is not None and not job.done():
                    job.cancel()
                    with suppress(asyncio.CancelledError):
                        await job
