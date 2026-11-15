"""真实 PostgreSQL 双连接验证外部提案与账号、作品删除的提交顺序。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text

from app.core.errors import ConflictError, UnauthorizedError
from app.models import AgentChange, AgentConversation, Project, Task, User
from app.services import admin_service, project_access, project_lifecycle, task_service
from app.services.authorization_service import AuthenticatedActor
from tests.helpers import create_project
from tests.test_agent_external_deletion_guard import _seed_change
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_user_deletion_postgresql import _prepare

pytest_plugins = ("tests.test_collaboration_user_deletion_postgresql",)


async def _create_claim(db, user, project):
    # 模拟未来协作 Agent 的受理协议；当前协作 Agent HTTP 入口仍未开放。
    db.info["authenticated_actor"] = AuthenticatedActor(user["id"], 0)
    await project_access.require_project(db, user["id"], project["id"], "content.edit", for_update=True)
    _, task_id, change_id = await _seed_change(
        db, project_id=project["id"], conversation_user_id=user["id"], task_user_id=user["id"],
        task_project_id=project["id"],
    )
    return task_id, change_id


@pytest.mark.parametrize("target", ["project", "user"])
async def test_detached_claim_is_found_by_persisted_turn_key(postgres_user_delete_apps, target):
    owner_app, guest_app = postgres_user_delete_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as admin,
    ):
        user, project, _ = await _prepare(owner, guest, admin, owner_app)
        source = await create_project(owner, title="原始作品") if target == "project" else project
        async with owner_app.state.sessionmaker() as db:
            project_owner = (await db.get(Project, source["id"])).user_id
            _, task_id, change_id = await _seed_change(
                db, project_id=source["id"], conversation_user_id=project_owner,
                task_user_id=user["id"], task_project_id=project["id"], binding="turn_key",
            )
            await db.commit()
        response = (
            await owner.delete(f"/api/v1/projects/{project['id']}") if target == "project"
            else await admin.delete(f"/api/v1/admin/users/{user['id']}")
        )
        expected = "project_tasks_not_stopped" if target == "project" else "user_has_pending_tasks"
        assert response.status_code == 409 and response.json()["code"] == expected, response.text
        async with owner_app.state.sessionmaker() as db:
            assert await db.get(User, user["id"]) is not None
            assert (await db.get(Project, project["id"])).deleted_at is None
            assert (await db.get(Task, task_id)).status == "cancelled"
            assert (await db.get(AgentChange, change_id)).status == "executing"


@pytest.mark.parametrize("claim_settles", [False, True])
async def test_unrelated_local_work_cannot_prove_cross_instance_claim_stopped(
    postgres_user_delete_apps, claim_settles,
):
    owner_app, guest_app = postgres_user_delete_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as admin,
    ):
        user, project, _ = await _prepare(owner, guest, admin, owner_app)
        async with guest_app.state.sessionmaker() as db:
            _, task_id, change_id = await _seed_change(
                db, project_id=project["id"], conversation_user_id=user["id"],
                task_user_id=user["id"], task_project_id=project["id"],
            )
            unrelated = Task(user_id=user["id"], project_id=project["id"], kind="chapter.analyze", status="queued")
            db.add(unrelated)
            await db.commit()
            unrelated_id = unrelated.id
        entered, release = asyncio.Event(), asyncio.Event()
        engine = owner_app.state.task_engine

        async def local_work():
            with engine.cleanup_scope(unrelated):
                entered.set()
                await release.wait()

        work = asyncio.create_task(local_work())
        deletion = None
        try:
            await asyncio.wait_for(entered.wait(), 15)
            deletion = asyncio.create_task(owner.delete(f"/api/v1/projects/{project['id']}"))
            async with asyncio.timeout(15):
                while True:
                    assert not deletion.done()
                    async with guest_app.state.sessionmaker() as db:
                        row = await db.get(Project, project["id"])
                        if row.deletion_started_at is not None:
                            assert (await db.get(Task, unrelated_id)).status == "queued"
                            assert (await db.get(Task, task_id)).status == "cancelled"
                            assert (await db.get(AgentChange, change_id)).status == "executing"
                            break
                    await asyncio.sleep(0.01)
            if claim_settles:
                # 模拟另一实例的外部请求真正结束后，在项目锁下持久完成该 claim。
                async with guest_app.state.sessionmaker() as db:
                    await project_lifecycle.lock_record(db, project["id"])
                    claim = await db.get(AgentChange, change_id)
                    claim.status, claim.result = "failed", {"error": "远端已收尾"}
                    await db.commit()
            release.set()
            response = await asyncio.wait_for(deletion, 15)
            if claim_settles:
                assert response.status_code == 204, response.text
            else:
                assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped"
            async with guest_app.state.sessionmaker() as db:
                row = await db.get(Project, project["id"])
                assert row.deletion_started_at is None and row.deletion_token is None
                assert (row.deleted_at is not None) is claim_settles
                assert (await db.get(Task, unrelated_id)).status == ("cancelled" if claim_settles else "queued")
                assert (await db.get(AgentChange, change_id)).status == ("failed" if claim_settles else "executing")
        finally:
            release.set()
            await work
            if deletion is not None and not deletion.done():
                deletion.cancel()
                with suppress(asyncio.CancelledError):
                    await deletion


async def test_terminal_external_result_requires_matching_string_payload(postgres_user_delete_apps):
    owner_app, guest_app = postgres_user_delete_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as admin,
    ):
        user, project, _ = await _prepare(owner, guest, admin, owner_app)
        cases = [
            ("failed", {}, 409),
            ("executed", {}, 409),
            ("failed", {"error": "结果未知", "_external_outcome_unknown": True}, 409),
            ("failed", {"error": "失败已记录"}, 204),
            ("executed", {"text": "执行已记录"}, 204),
        ]
        for index, (status, result, expected) in enumerate(cases):
            target = project if index == 0 else await create_project(owner, title=f"结果案例 {index}")
            async with guest_app.state.sessionmaker() as db:
                _, _, change_id = await _seed_change(
                    db, project_id=target["id"], conversation_user_id=user["id"],
                    status=status, result=result,
                )
                await db.commit()
            response = await owner.delete(f"/api/v1/projects/{target['id']}")
            assert response.status_code == expected, response.text
            async with owner_app.state.sessionmaker() as db:
                row = await db.get(Project, target["id"])
                assert (row.deleted_at is not None) is (expected == 204)
                assert (await db.get(AgentChange, change_id)).status == status


@pytest.mark.parametrize("target", ["project", "user"])
async def test_committed_external_claim_blocks_waiting_delete(postgres_user_delete_apps, target):
    owner_app, guest_app = postgres_user_delete_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as admin,
    ):
        user, project, _ = await _prepare(owner, guest, admin, owner_app)
        pending = None
        async with guest_app.state.sessionmaker() as db:
            task_id, change_id = await _create_claim(db, user, project)
            holder = await db.scalar(text("SELECT pg_backend_pid()"))
            try:
                pending = asyncio.create_task(
                    owner.delete(f"/api/v1/projects/{project['id']}") if target == "project"
                    else admin.delete(f"/api/v1/admin/users/{user['id']}")
                )
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder), 15)
                await db.commit()
                response = await asyncio.wait_for(pending, 15)
                expected = "project_tasks_not_stopped" if target == "project" else "user_has_pending_tasks"
                assert response.status_code == 409 and response.json()["code"] == expected, response.text
                async with owner_app.state.sessionmaker() as verify:
                    assert await verify.get(User, user["id"]) is not None
                    assert (await verify.get(Project, project["id"])).deleted_at is None
                    assert (await verify.get(Task, task_id)).status == "cancelled"
                    assert (await verify.get(AgentChange, change_id)).status == "executing"
            finally:
                await db.rollback()
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending


async def test_project_delete_intent_prevents_new_external_claim(postgres_user_delete_apps, monkeypatch):
    owner_app, guest_app = postgres_user_delete_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as admin,
    ):
        user, project, _ = await _prepare(owner, guest, admin, owner_app)
        entered, release = asyncio.Event(), asyncio.Event()
        original_stop = task_service.stop_project_tasks

        async def pause_after_intent(db, engine, project_id):
            entered.set()
            await release.wait()
            return await original_stop(db, engine, project_id)

        monkeypatch.setattr(task_service, "stop_project_tasks", pause_after_intent)
        deletion = asyncio.create_task(owner.delete(f"/api/v1/projects/{project['id']}"))
        try:
            await asyncio.wait_for(entered.wait(), 15)
            async with guest_app.state.sessionmaker() as db:
                with pytest.raises(ConflictError) as denied:
                    await _create_claim(db, user, project)
                assert denied.value.extra["code"] == "project_deleting"
                await db.rollback()
            release.set()
            assert (await asyncio.wait_for(deletion, 15)).status_code == 204
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(select(AgentChange.id).where(AgentChange.project_id == project["id"])) is None
                assert (await db.get(Project, project["id"])).deleted_at is not None
        finally:
            release.set()
            if not deletion.done():
                deletion.cancel()
                with suppress(asyncio.CancelledError):
                    await deletion


async def test_user_delete_commit_prevents_waiting_external_claim(postgres_user_delete_apps, monkeypatch):
    owner_app, guest_app = postgres_user_delete_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as admin,
    ):
        user, project, _ = await _prepare(owner, guest, admin, owner_app)
        async with owner_app.state.sessionmaker() as db:
            db.add(AgentConversation(project_id=project["id"], user_id=user["id"]))
            await db.commit()
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = admin_service._flush_user_delete

        async def hold_deleted_user(db):
            await original(db)
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()

        async def start_claim():
            async with guest_app.state.sessionmaker() as db:
                try:
                    await _create_claim(db, user, project)
                    await db.commit()
                    return "unexpectedly admitted"
                except UnauthorizedError:
                    await db.rollback()
                    return "authentication expired"

        monkeypatch.setattr(admin_service, "_flush_user_delete", hold_deleted_user)
        deletion = asyncio.create_task(admin.delete(f"/api/v1/admin/users/{user['id']}"))
        pending = None
        try:
            await asyncio.wait_for(reached.wait(), 15)
            pending = asyncio.create_task(start_claim())
            await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, holder["pid"]), 15)
            release.set()
            assert (await asyncio.wait_for(deletion, 15)).status_code == 204
            assert await asyncio.wait_for(pending, 15) == "authentication expired"
            async with owner_app.state.sessionmaker() as db:
                assert await db.get(User, user["id"]) is None
                assert await db.scalar(select(AgentChange.id).where(AgentChange.project_id == project["id"])) is None
                assert (await db.get(Project, project["id"])).deleted_at is None
        finally:
            release.set()
            for job in (deletion, pending):
                if job is not None and not job.done():
                    job.cancel()
                    with suppress(asyncio.CancelledError):
                        await job
