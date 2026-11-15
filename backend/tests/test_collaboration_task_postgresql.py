"""真实 PostgreSQL 及 HTTP 流上的协作任务授权。"""

import asyncio
import socket
from contextlib import suppress

import httpx
import pytest
import uvicorn
from sqlalchemy import select

from app.models import Chapter, ProjectCollaborator, Task, User
from app.services import personal_projection, task_service
from tests import pg_support
from tests.helpers import Gate, create_project, register, wait_until
from tests.test_collaboration_chapter_entry_e4f import _member, _world


@pytest.fixture
async def postgres_collaboration_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url("AINOVEL_COLLAB_TASKS_POSTGRES_TEST_URL", "collab_tasks", purpose="协作任务测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


async def _stream_frame(lines):
    event = None
    data = None
    async for line in lines:
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = line.removeprefix("data: ")
        elif not line and event is not None:
            return event, data
    raise AssertionError("流在事件前关闭")


async def test_postgres_http_stream_rechecks_after_cross_instance_revoke(postgres_collaboration_apps, fake_llm,
                                                                       monkeypatch):
    """E5-f：协作者经生产入口受控订阅真实运行的章节任务；另一实例撤权（本实例收不到进程内通知），
    下一帧在发送前按数据库复核被拒为 permission_denied，撤权后的内容不发出。"""
    owner_app, guest_app = postgres_collaboration_apps
    guest_app.state.settings.allow_private_ai_endpoints = True
    guest_app.state.settings.auto_analyze_enabled = False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(guest_app, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    gate1, gate2 = Gate(), Gate()
    try:
        await wait_until(lambda: server.started, within=10)
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as guest,
        ):
            world = await _world(owner, guest)
            owner_user, guest_user, project = world.owner, world.guest, world.project
            base = f"/api/v1/projects/{project['id']}"
            engine = guest_app.state.task_engine
            engine.collaboration_scan_interval = 3600
            await engine.start()
            fake_llm.push(["快照", gate1, "撤权后不能收到的内容", gate2, "其余"])
            created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
            assert created.status_code == 202, created.text
            task_id, task_key = created.json()["id"], created.json()["task_key"]
            await gate1.wait_reached(within=10)
            await wait_until(lambda: (hub := engine.hubs.get(task_id)) is not None and hub.text == "快照", within=10)
            member = await _member(guest_app, world)
            async with owner_app.state.sessionmaker() as db:
                # 运行中但授权轮次非规范（字符串 revision）：不得被列出或读取。
                invalid = Task(user_id=guest_user["id"], project_id=project["id"], kind="chapter.generate",
                               status="running", title="bad", input={"_collaboration_grant": {
                                   "invitation_id": member.invitation_id, "revision": str(member.revision)}})
                owner_task = Task(user_id=owner_user["id"], project_id=project["id"], kind="chapter.generate",
                                  status="succeeded", title="owner")
                db.add_all([invalid, owner_task])
                await db.commit()
            listing = await guest.get("/api/v1/tasks", params={"limit": 1})
            assert listing.status_code == 200 and [row["id"] for row in listing.json()] == [task_id], listing.text
            denied = await guest.get(f"/api/v1/tasks/{invalid.id}", params={"task_key": invalid.statistics_key})
            assert denied.status_code == 404
            denied = await guest.get(f"/api/v1/tasks/{owner_task.id}", params={"task_key": owner_task.statistics_key})
            assert denied.status_code == 404
            async with guest.stream(
                "GET", f"/api/v1/tasks/{task_id}/stream", params={"task_key": task_key},
            ) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                event, data = await asyncio.wait_for(_stream_frame(lines), 10)
                assert event == "snapshot" and "快照" in data
                reached, release = asyncio.Event(), asyncio.Event()
                original = engine._visible_task

                async def before_check(db, checked_id, **kwargs):
                    # 只拦订阅侧对该任务的帧复核（带 actor_id），不影响执行体。
                    if checked_id == task_id and kwargs.get("actor_id") is not None and not reached.is_set():
                        reached.set()
                        await release.wait()
                    return await original(db, checked_id, **kwargs)

                monkeypatch.setattr(engine, "_visible_task", before_check)
                gate1.release()
                await asyncio.wait_for(reached.wait(), 10)
                try:
                    revoked = await owner.delete(f"{base}/collaborators/{guest_user['id']}",
                                                 params={"expected_revision": member.revision})
                    assert revoked.status_code == 204, revoked.text
                finally:
                    release.set()
                event, data = await asyncio.wait_for(_stream_frame(lines), 10)
                assert event == "error" and "permission_denied" in data and "撤权后不能收到的内容" not in data
                assert [item async for item in lines if item.startswith("event: ")] == []
            assert (await guest.get(f"/api/v1/tasks/{task_id}", params={"task_key": task_key})).status_code == 404
            async with owner_app.state.sessionmaker() as db:
                actual = await db.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"],
                ))
                assert actual.status == "revoked" and actual.revision == member.revision + 1
            gate2.release()
            await wait_until(lambda: task_id not in engine._running, within=15)
            async with owner_app.state.sessionmaker() as db:
                row = await db.get(Task, task_id)
                chapter = await db.get(Chapter, world.chapter["id"])
            assert row.status != "succeeded" and chapter.content == "原正文内容"
    finally:
        gate1.release()
        gate2.release()
        server.should_exit = True
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(runner, 10)
        sock.close()


async def test_postgres_old_invitation_stream_remains_invalid_after_reinvite(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        await register(owner, "pg-reinvite-owner")
        user = await register(guest, "pg-reinvite-guest")
        project = await create_project(owner)
        base = f"/api/v1/projects/{project['id']}"
        old = (await owner.post(f"{base}/collaborators", json={
            "username": "pg-reinvite-guest", "role": "editor",
        })).json()
        assert (await guest.post(f"/api/v1/collaboration/invitations/project/{old['invitation_id']}/accept", json={
            "expected_revision": 1,
        })).status_code == 200
        async with owner_app.state.sessionmaker() as db:
            task = Task(user_id=user["id"], project_id=project["id"], kind="chapter.generate", status="running",
                        input={"_collaboration_grant": {"invitation_id": old["invitation_id"], "revision": 2}})
            db.add(task)
            await db.commit()
            task_id, task_key = task.id, task.statistics_key
        revoked = await owner.delete(f"{base}/collaborators/{user['id']}", params={"expected_revision": 2})
        assert revoked.status_code == 204
        new = (await owner.post(f"{base}/collaborators", json={
            "username": "pg-reinvite-guest", "role": "editor",
        })).json()
        assert new["invitation_id"] != old["invitation_id"]
        assert (await guest.post(f"/api/v1/collaboration/invitations/project/{new['invitation_id']}/accept", json={
            "expected_revision": new["revision"],
        })).status_code == 200
        assert (await guest.get(f"/api/v1/tasks/{task_id}", params={"task_key": task_key})).status_code == 404


@pytest.mark.parametrize("endpoint", ["detail", "list", "output", "stream"])
async def test_postgres_downgrade_after_projection_fails_closed(postgres_collaboration_apps, monkeypatch, endpoint):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        await register(owner, "pg-final-owner")
        user = await register(guest, "pg-final-guest")
        project = await create_project(owner)
        base = f"/api/v1/projects/{project['id']}"
        invited = await owner.post(f"{base}/collaborators", json={"username": "pg-final-guest", "role": "editor"})
        assert invited.status_code == 201
        invitation = invited.json()
        accepted = await guest.post(
            f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
            json={"expected_revision": 1},
        )
        assert accepted.status_code == 200
        async with owner_app.state.sessionmaker() as db:
            task = Task(user_id=user["id"], project_id=project["id"], kind="chapter.generate", status="running",
                        output_text="权限收紧后不得发出",
                        input={"_collaboration_grant": {"invitation_id": invitation["invitation_id"], "revision": 2}})
            db.add(task)
            await db.commit()
            task_id, key = task.id, task.statistics_key
        stream = None
        if endpoint == "stream":
            engine = guest_app.state.task_engine
            hub, _ = engine.ensure_hub(task_id, statistics_key=key, status="running", text="允许的旧快照")
            stream = engine.subscribe(task_id, statistics_key=key, actor_id=user["id"], token_version=0,
                                      active_grant_required=True)
            assert (await anext(stream))[0] == "snapshot"

        reached, release = asyncio.Event(), asyncio.Event()
        if endpoint == "output":
            original_check = task_service._require_visible_task_result

            async def blocked_check(*args, **kwargs):
                reached.set()
                await release.wait()
                return await original_check(*args, **kwargs)

            monkeypatch.setattr(task_service, "_require_visible_task_result", blocked_check)
        else:
            original_projection = personal_projection.project_payload

            async def blocked_projection(*args, **kwargs):
                projected = await original_projection(*args, **kwargs)
                reached.set()
                await release.wait()
                return projected

            monkeypatch.setattr(personal_projection, "project_payload", blocked_projection)
        if stream is not None:
            hub.append("权限收紧后不得发出")
            pending = asyncio.create_task(anext(stream))
        else:
            path = {"detail": f"/api/v1/tasks/{task_id}", "list": "/api/v1/tasks",
                    "output": f"/api/v1/tasks/{task_id}/output"}[endpoint]
            params = {"task_key": key} if endpoint != "list" else None
            pending = asyncio.create_task(guest.get(path, params=params))
        try:
            await asyncio.wait_for(reached.wait(), 10)
            changed = await owner.patch(f"{base}/collaborators/{user['id']}", json={
                "expected_revision": 2, "role": "viewer",
            })
            assert changed.status_code == 200, changed.text
            async with owner_app.state.sessionmaker() as db:
                member = await db.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
                ))
                assert (member.status, member.role, member.revision) == ("accepted", "viewer", 3)
            release.set()
            result = await asyncio.wait_for(pending, 10)
            if stream is not None:
                assert result[0] == "error" and result[1]["code"] == "permission_denied"
                assert "权限收紧后" not in str(result[1])
            elif endpoint == "list":
                assert result.status_code == 200 and task_id not in [item["id"] for item in result.json()]
            else:
                assert result.status_code == 404 and "权限收紧后" not in result.text
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            if stream is not None:
                await stream.aclose()


async def test_postgres_task_round_and_personal_clear_barriers(postgres_collaboration_apps, monkeypatch):
    from app.services.task_engine.registry import get_handler

    first_app, second_app = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first_app), base_url="http://testserver") as client:
        user = await register(client, "pg-task-round")
        async with first_app.state.sessionmaker() as db:
            task = Task(user_id=user["id"], kind="book.import", status="paused")
            db.add(task)
            await db.commit()
            task_id, task_key, initial_round = task.id, task.statistics_key, task.queued_at

        reached, release = asyncio.Event(), asyncio.Event()
        original_get = task_service.get_owned_task
        blocked_once = False

        async def gated_get(*args, **kwargs):
            nonlocal blocked_once
            task = await original_get(*args, **kwargs)
            if not blocked_once:
                blocked_once = True
                reached.set()
                await release.wait()
            return task

        async def no_op(*_args, **_kwargs):
            return None

        monkeypatch.setattr(task_service, "get_owned_task", gated_get)
        monkeypatch.setattr(task_service, "_abort_loaded_task", no_op)
        monkeypatch.setattr(get_handler("book.import"), "revalidate_resume", no_op)
        pending = asyncio.create_task(client.post(f"/api/v1/tasks/{task_id}/cancel", params={"task_key": task_key}))
        try:
            await asyncio.wait_for(reached.wait(), 10)
            resumed = await client.post(f"/api/v1/tasks/{task_id}/resume", params={"task_key": task_key})
            assert resumed.status_code == 202, resumed.text
            release.set()
            response = await asyncio.wait_for(pending, 10)
            assert response.status_code == 409 and response.json()["code"] == "task_round_changed", response.text
            async with second_app.state.sessionmaker() as db:
                current = await db.get(Task, task_id)
                assert current.statistics_key == task_key and current.status == "queued"
                assert current.queued_at != initial_round
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
        monkeypatch.setattr(task_service, "get_owned_task", original_get)

        async with first_app.state.sessionmaker() as db:
            finished = Task(user_id=user["id"], kind="book.import", status="succeeded")
            db.add(finished)
            await db.commit()
            finished_id = finished.id
        reached, release = asyncio.Event(), asyncio.Event()
        original_lock = task_service._lock_accessible_history_projects

        async def before_lock(*args, **kwargs):
            reached.set()
            await release.wait()
            return await original_lock(*args, **kwargs)

        monkeypatch.setattr(task_service, "_lock_accessible_history_projects", before_lock)
        pending = asyncio.create_task(client.delete("/api/v1/tasks", params={"finished": True}))
        try:
            await asyncio.wait_for(reached.wait(), 10)
            async with second_app.state.sessionmaker() as db:
                (await db.get(User, user["id"])).token_version += 1
                await db.commit()
            release.set()
            response = await asyncio.wait_for(pending, 10)
            assert response.status_code == 401, response.text
            async with first_app.state.sessionmaker() as db:
                assert await db.get(Task, finished_id) is not None
        finally:
            release.set()
            if not pending.done():
                pending.cancel()

        other = await register(client, "pg-task-clear-first")
        async with first_app.state.sessionmaker() as db:
            finished = Task(user_id=other["id"], kind="book.import", status="succeeded")
            db.add(finished)
            await db.commit()
            finished_id = finished.id
        reached, release, attempting = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def after_lock(*args, **kwargs):
            result = await original_lock(*args, **kwargs)
            reached.set()
            await release.wait()
            return result

        async def revoke():
            async with second_app.state.sessionmaker() as db:
                attempting.set()
                await db.execute(
                    User.__table__.update().where(User.id == other["id"]).values(token_version=User.token_version + 1)
                )
                await db.commit()

        monkeypatch.setattr(task_service, "_lock_accessible_history_projects", after_lock)
        pending = asyncio.create_task(client.delete("/api/v1/tasks", params={"finished": True}))
        revocation = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            revocation = asyncio.create_task(revoke())
            await asyncio.wait_for(attempting.wait(), 10)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(revocation), 0.2)
            release.set()
            response = await asyncio.wait_for(pending, 10)
            assert response.status_code == 200 and response.json()["deleted"] == 1, response.text
            await asyncio.wait_for(revocation, 10)
            async with first_app.state.sessionmaker() as db:
                assert await db.get(Task, finished_id) is None
                assert (await db.get(User, other["id"])).token_version == 1
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            if revocation is not None and not revocation.done():
                revocation.cancel()
