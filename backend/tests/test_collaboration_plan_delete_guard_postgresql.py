"""细纲隐式删章的权限、任务与发送门禁真实 PostgreSQL 竞争。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text

from app.models import Chapter, ChapterPlan, ProjectCollaborator, Task
from app.services import ai_dispatch, plan_service, project_lifecycle
from tests import pg_support
from tests.helpers import create_project, register
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_rowlock_postgresql import _assert_blocked_on_project_row


@pytest.fixture
async def postgres_plan_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url("AINOVEL_COLLAB_PLANNING_POSTGRES_TEST_URL", "collab_planning", purpose="细纲测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


async def _named_setup(owner, guest, suffix):
    await register(owner, f"plan-owner-{suffix}")
    user = await register(guest, f"plan-guest-{suffix}")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    invitation = await owner.post(f"{base}/collaborators", json={
        "username": f"plan-guest-{suffix}", "role": "editor",
    })
    assert invitation.status_code == 201, invitation.text
    accept = f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept"
    assert (await guest.post(accept, json={"expected_revision": 1})).status_code == 200
    return project, user, base


async def _plan(owner, base, *, built=True):
    outline = await owner.post(f"{base}/outlines", json={"title": "大纲", "chapter_no": 1})
    assert outline.status_code == 201
    path = f"{base}/outlines/{outline.json()['id']}/plans"
    plans = await owner.put(path, json={"plans": [{"title": "细纲"}]})
    assert plans.status_code == 200
    if built:
        response = await owner.post(f"{path}/build")
        assert response.status_code == 200, response.text
        return path, response.json()["plans"][0]
    return path, plans.json()[0]


async def _membership_race(apps, revoke):
    owner_app, guest_app = apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _named_setup(owner, guest, "revoke" if revoke else "downgrade")
        path, plan = await _plan(owner, base)
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
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
                pending = asyncio.create_task(guest.put(path, json={"plans": []}))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await _assert_blocked_on_project_row(guest_app.state.sessionmaker, pid)
                await holder.commit()
            result = await asyncio.wait_for(pending, 6)
            assert result.status_code == (404 if revoke else 403), result.text
            async with owner_app.state.sessionmaker() as db:
                assert await db.get(Chapter, plan["chapter_id"]) is not None
                assert (await db.get(ChapterPlan, plan["id"])).chapter_id == plan["chapter_id"]
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def _task_race(apps):
    owner_app, guest_app = apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, _, base = await _named_setup(owner, guest, "task")
        owner_id = (await owner.get("/api/v1/auth/me")).json()["id"]
        path, plan = await _plan(owner, base)
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project["id"])
                task = Task(user_id=owner_id, project_id=project["id"], chapter_id=plan["chapter_id"],
                            kind="chapter.analyze", status="queued", input={"chapter_id": plan["chapter_id"]})
                holder.add(task)
                await holder.flush()
                task_id = task.id
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(guest.put(path, json={"plans": []}))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await _assert_blocked_on_project_row(guest_app.state.sessionmaker, pid)
                await holder.commit()
            result = await asyncio.wait_for(pending, 6)
            assert result.status_code == 409 and result.json()["code"] == "chapter_in_use", result.text
            async with owner_app.state.sessionmaker() as db:
                assert await db.get(Chapter, plan["chapter_id"]) is not None
                saved = await db.get(Task, task_id)
                assert saved.status == "queued" and saved.chapter_id == plan["chapter_id"]
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def _send_gate(apps, monkeypatch):
    owner_app, guest_app = apps
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.25)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, _, base = await _named_setup(owner, guest, "send")
        path, plan = await _plan(owner, base)
        async with owner_app.state.sessionmaker() as send:
            await ai_dispatch._lock(send, project["id"], write=False)
            plain = await guest.put(path, json={"plans": [{"id": plan["id"], "title": "整理细纲"}]})
            assert plain.status_code == 200, plain.text
            blocked = await guest.put(path, json={"plans": []})
            assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", blocked.text
            async with owner_app.state.sessionmaker() as db:
                assert await db.get(Chapter, plan["chapter_id"]) is not None
            await send.rollback()
        deleted = await guest.put(path, json={"plans": []})
        assert deleted.status_code == 200, deleted.text


async def _newly_built(apps, monkeypatch):
    owner_app, guest_app = apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        _, user, base = await _named_setup(owner, guest, "built")
        path, plan = await _plan(owner, base, built=False)
        reached, release = asyncio.Event(), asyncio.Event()
        original = plan_service.require_project

        async def wait_before_lock(db, actor_id, project_id, capability, **kwargs):
            if actor_id == user["id"] and kwargs.get("for_update"):
                assert not kwargs.get("visibility_write")
                reached.set()
                await release.wait()
            return await original(db, actor_id, project_id, capability, **kwargs)

        monkeypatch.setattr(plan_service, "require_project", wait_before_lock)
        pending = asyncio.create_task(guest.put(path, json={"plans": []}))
        try:
            await asyncio.wait_for(reached.wait(), 6)
            built = await owner.post(f"{path}/build")
            assert built.status_code == 200, built.text
            chapter_id = built.json()["chapters"][0]["id"]
            release.set()
            result = await asyncio.wait_for(pending, 6)
            assert result.status_code == 409 and result.json()["code"] == "collaboration_busy", result.text
            async with owner_app.state.sessionmaker() as db:
                assert await db.get(Chapter, chapter_id) is not None
                assert (await db.get(ChapterPlan, plan["id"])).chapter_id == chapter_id
            monkeypatch.setattr(plan_service, "require_project", original)
            retried = await guest.put(path, json={"plans": []})
            assert retried.status_code == 200, retried.text
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_postgres_sync_delete_guards(postgres_plan_apps, monkeypatch):
    await _membership_race(postgres_plan_apps, True)
    await _membership_race(postgres_plan_apps, False)
    await _task_race(postgres_plan_apps)
    await _send_gate(postgres_plan_apps, monkeypatch)
    await _newly_built(postgres_plan_apps, monkeypatch)
