"""真实 PostgreSQL 跨实例、跨连接验证协作写与撤权的提交顺序。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models import Project, ProjectCollaborator, SeriesCollaborator
from app.services import collaboration_service
from app.services.audit_writer import AuditOperation
from tests import pg_support
from tests.helpers import create_project, register


@pytest.fixture
async def postgres_collaboration_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="协作测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


async def _wait_for_blocker(maker, holder_pid: int) -> None:
    query = text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                 "WHERE datname=current_database() AND :holder=ANY(pg_blocking_pids(pid)))")
    while True:
        async with maker() as db:
            if await db.scalar(query, {"holder": holder_pid}):
                return
        await asyncio.sleep(0.01)


async def _assert_project_row_locked(maker, project_id: int) -> None:
    async with maker() as db:
        with pytest.raises(DBAPIError) as error:
            await db.execute(select(Project.id).where(Project.id == project_id).with_for_update(nowait=True))
        original = error.value.orig
        assert any(getattr(item, "sqlstate", None) == "55P03"
                   for item in (original, getattr(original, "__cause__", None)))
        await db.rollback()


async def _setup(owner, guest):
    await register(owner, "pg-owner")
    guest_user = await register(guest, "pg-guest")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    invitation = await owner.post(f"{base}/collaborators", json={"username": "pg-guest", "role": "editor"})
    assert invitation.status_code == 201, invitation.text
    accept = f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept"
    assert (await guest.post(accept, json={"expected_revision": 1})).status_code == 200
    return project, guest_user, base


async def test_revoke_commits_before_waiting_editor_write(postgres_collaboration_apps, monkeypatch):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = collaboration_service.add_success

        async def gated(db, snapshot):
            event = await original(db, snapshot)
            if snapshot.action == "revoke":
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return event

        monkeypatch.setattr(collaboration_service, "add_success", gated)
        revoke = asyncio.create_task(owner.delete(
            f"{base}/collaborators/{user['id']}", params={"expected_revision": 2},
        ))
        pending = None
        try:
            await asyncio.wait_for(reached.wait(), 6)
            await _assert_project_row_locked(guest_app.state.sessionmaker, project["id"])
            pending = asyncio.create_task(guest.patch(base, json={"title": "撤权后不应写入"}))
            await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, holder["pid"]), 6)
            release.set()
            assert (await asyncio.wait_for(revoke, 6)).status_code == 204
            assert (await asyncio.wait_for(pending, 6)).status_code == 404
            async with owner_app.state.sessionmaker() as db:
                assert (await db.get(Project, project["id"])).title == project["title"]
        finally:
            release.set()
            for task in (revoke, pending):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


async def test_editor_write_commits_before_waiting_revoke(postgres_collaboration_apps, monkeypatch):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = AuditOperation.commit

        async def gated(self, *args, **kwargs):
            if self.values["action"] == "project.update" and self.actor_id == user["id"]:
                await self.db.flush()
                holder["pid"] = await self.db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(AuditOperation, "commit", gated)
        # 写提交释放行锁后，撤权在锁内、提交前停住：让写请求在撤权提交前完成响应投影。
        # 响应投影提交后会按最新授权重验（撤权已提交时失败关闭为 404，不回传作品数据），
        # 不控制这个窗口时两者先后由调度决定；本用例只验证“写先提交”一侧。
        revoke_locked, revoke_release = asyncio.Event(), asyncio.Event()
        original_add = collaboration_service.add_success

        async def hold_revoke(db, snapshot):
            result = await original_add(db, snapshot)
            if snapshot.action == "revoke":
                revoke_locked.set()
                await revoke_release.wait()
            return result

        monkeypatch.setattr(collaboration_service, "add_success", hold_revoke)
        write = asyncio.create_task(guest.patch(base, json={"title": "先提交的合法修改"}))
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 6)
            await _assert_project_row_locked(owner_app.state.sessionmaker, project["id"])
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{user['id']}", params={
                "expected_revision": 2,
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 6)
            assert not revoke_locked.is_set()
            release.set()
            assert (await asyncio.wait_for(write, 6)).status_code == 200
            await asyncio.wait_for(revoke_locked.wait(), 6)
            revoke_release.set()
            assert (await asyncio.wait_for(revoke, 6)).status_code == 204
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(select(Project.title).where(Project.id == project["id"])) == "先提交的合法修改"
            assert (await guest.get(base)).status_code == 404
        finally:
            release.set()
            revoke_release.set()
            for task in (write, revoke):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


async def test_concurrent_duplicate_invites_have_one_success(postgres_collaboration_apps):
    owner_app, second_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://testserver") as owner2,
    ):
        await register(owner, "pg-owner")
        guest_user = await register(guest, "pg-guest")
        project = await create_project(owner)
        owner2.cookies.update(owner.cookies)
        url = f"/api/v1/projects/{project['id']}/collaborators"
        results = await asyncio.gather(*[
            actor.post(url, json={"username": "pg-guest", "role": "viewer"}) for actor in (owner, owner2)
        ])
        assert sorted(result.status_code for result in results) == [201, 409]
        assert next(result for result in results if result.status_code == 409).json()["code"] == "already_collaborator"
        async with owner_app.state.sessionmaker() as db:
            rows = list(await db.scalars(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == guest_user["id"],
            )))
            assert len(rows) == 1 and rows[0].status == "pending"


@pytest.mark.parametrize("scope", ["project", "series"])
async def test_postgres_duplicate_membership_constraint_is_identified(postgres_collaboration_apps, scope):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        owner_user = await register(owner, "pg-constraint-owner")
        guest_user = await register(guest, "pg-constraint-guest")
        if scope == "project":
            resource = await create_project(owner)
            model, prefix = ProjectCollaborator, "projects"
        else:
            response = await owner.post("/api/v1/series", json={"name": "约束测试"})
            assert response.status_code == 201, response.text
            resource, model, prefix = response.json(), SeriesCollaborator, "series"
        payload = {"username": "pg-constraint-guest"}
        if scope == "project":
            payload["role"] = "viewer"
        invited = await owner.post(f"/api/v1/{prefix}/{resource['id']}/collaborators", json=payload)
        assert invited.status_code == 201, invited.text
        async with owner_app.state.sessionmaker() as db:
            db.add(model(**{
                f"{scope}_id": resource["id"], "user_id": guest_user["id"], "role": "editor",
                "invitation_id": str(uuid4()), "invited_by_user_id": owner_user["id"],
            }))
            with pytest.raises(IntegrityError) as duplicate:
                await db.flush()
            assert collaboration_service._membership_conflict(duplicate.value, scope)
            await db.rollback()
