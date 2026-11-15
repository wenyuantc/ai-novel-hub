"""隔离全局授权门禁，验证真实项目父行屏障和锁后授权重读。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CollaborationAuditEvent, Project, ProjectCollaborator
from app.services import collaboration_service, project_lifecycle
from tests.helpers import create_project, register
from tests.test_collaboration_access_postgresql import (
    _setup,
    _wait_for_blocker,
)

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def _assert_blocked_on_project_row(maker, holder_pid):
    async with maker() as db:
        queries = (await db.scalars(text(
            "SELECT query FROM pg_stat_activity WHERE datname=current_database() "
            "AND :holder=ANY(pg_blocking_pids(pid))"
        ), {"holder": holder_pid})).all()
        assert any("update projects" in query.lower() for query in queries), queries


@pytest.mark.parametrize("revoke", [True, False])
async def test_project_lock_rechecks_revoke_or_downgrade_without_global_gate(
    postgres_collaboration_apps, revoke,
):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                # 测试持有者只锁项目行，不占用授权门禁或 User 行。
                await project_lifecycle.lock_active(holder, project["id"])
                member = await holder.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
                ))
                if revoke:
                    member.status = "revoked"
                else:
                    member.role = "viewer"
                member.revision += 1
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(guest.patch(base, json={"title": "不应提交的旧权限修改"}))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await _assert_blocked_on_project_row(guest_app.state.sessionmaker, pid)
                await holder.commit()
            result = await asyncio.wait_for(pending, 6)
            assert result.status_code == (404 if revoke else 403), result.text
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(select(Project.title).where(Project.id == project["id"])) == project["title"]
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_revoke_waits_for_project_row_without_global_gate(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        pending = None
        try:
            async with guest_app.state.sessionmaker() as holder:
                record = await project_lifecycle.lock_active(holder, project["id"])
                record.title = "持有项目行时先提交的修改"
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(owner.delete(
                    f"{base}/collaborators/{user['id']}", params={"expected_revision": 2},
                ))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 6)
                await _assert_blocked_on_project_row(owner_app.state.sessionmaker, pid)
                await holder.commit()
            result = await asyncio.wait_for(pending, 6)
            assert result.status_code == 204, result.text
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(select(Project.title).where(Project.id == project["id"])) == (
                    "持有项目行时先提交的修改"
                )
            assert (await guest.get(base)).status_code == 404
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


@pytest.mark.parametrize("committed", [False, True])
async def test_cancelled_invite_reconciles_on_postgresql(postgres_collaboration_apps, monkeypatch, committed):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        await register(owner, "pg-cancel-owner")
        user = await register(guest, "pg-cancel-guest")
        project = await create_project(owner)
        entered, cleanup_entered, cleanup_release, never = (asyncio.Event() for _ in range(4))
        original_add = collaboration_service.add_success
        original_commit = AsyncSession.commit
        original_failure = collaboration_service.record_failure

        async def gated_add(db, snapshot):
            if committed:
                result = await original_add(db, snapshot)
                db.info["pg_cancel_after_commit"] = True
                return result
            entered.set()
            await never.wait()

        async def gated_commit(db):
            await original_commit(db)
            if db.info.pop("pg_cancel_after_commit", False):
                entered.set()
                await never.wait()

        async def gated_failure(*args, **kwargs):
            cleanup_entered.set()
            await cleanup_release.wait()
            return await original_failure(*args, **kwargs)

        monkeypatch.setattr(collaboration_service, "add_success", gated_add)
        monkeypatch.setattr(AsyncSession, "commit", gated_commit)
        monkeypatch.setattr(collaboration_service, "record_failure", gated_failure)
        pending = asyncio.create_task(owner.post(f"/api/v1/projects/{project['id']}/collaborators", json={
            "username": "pg-cancel-guest", "role": "viewer",
        }))
        try:
            await asyncio.wait_for(entered.wait(), 6)
            pending.cancel()
            await asyncio.wait_for(cleanup_entered.wait(), 6)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            cleanup_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 6)
            async with owner_app.state.sessionmaker() as db:
                member = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.user_id == user["id"]))
                events = list(await db.scalars(select(CollaborationAuditEvent).where(
                    CollaborationAuditEvent.target_user_id == user["id"],
                )))
                assert len(events) == 1
                if committed:
                    assert member is not None and events[0].result == "success"
                else:
                    assert member is None and events[0].result == "failure"
                    assert events[0].error_code == "cancelled"
        finally:
            cleanup_release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
