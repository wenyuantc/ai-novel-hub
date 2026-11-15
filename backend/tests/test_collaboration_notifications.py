"""协作变更提交后及时唤醒任务流；通知只触发数据库复验。"""

import asyncio
import json
import socket
from contextlib import suppress

import httpx
import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ACCESS_TOKEN_COOKIE
from app.models import Chapter, CollaborationAuditEvent, ProjectCollaborator, Task, User
from app.services import collaboration_service
from tests.helpers import Gate, create_project, register, wait_until
from tests.test_collaboration_chapter_entry_e4f import _member, _world


async def _accepted_editor(client, make_client):
    await register(client, "notify-owner")
    guest = make_client()
    user = await register(guest, "notify-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    response = await client.post(f"{base}/collaborators", json={"username": "notify-editor", "role": "editor"})
    assert response.status_code == 201, response.text
    invitation = response.json()
    response = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert response.status_code == 200, response.text
    return guest, user, project, response.json()


async def _frame(lines):
    event = None
    data = None
    async for line in lines:
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = line.removeprefix("data: ")
        elif not line and event is not None:
            return event, json.loads(data)
    raise AssertionError("流在事件前关闭")


async def _frames_until_error(lines):
    """读到第一帧 error 为止；之前的帧（如运行中的 progress）原样收集。"""
    frames = []
    while True:
        frames.append(await _frame(lines))
        if frames[-1][0] in ("error", "done", "paused"):
            return frames


async def _settled(app, task_id, within=15.0):
    async with asyncio.timeout(within):
        while True:
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task_id)
            if row.status not in ("queued", "running") and task_id not in app.state.task_engine._running:
                return row
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}])
@pytest.mark.parametrize("action", ["revoke", "downgrade", "model_permission"])
async def test_http_stream_wakes_after_project_grant_change(app, client, make_client, fake_llm, monkeypatch, action):
    """E5-f：协作者经生产入口受控订阅真实运行的章节任务；协作变更提交后的进程内通知立即唤醒复核，
    以 permission_denied 结束（心跳 120 秒，不靠心跳发现），撤权后的正文既不发出也不保存。"""
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    engine.heartbeat_seconds = 120
    engine.collaboration_scan_interval = 3600

    async def keep_running(*_args):
        # 不让通知取消执行体：订阅侧的唤醒复核必须独立承重（执行体仍阻塞在 Gate、任务仍 running）。
        return None

    monkeypatch.setattr(engine, "_check_collaboration_runs", keep_running)
    gate = Gate()
    fake_llm.push(["已发送快照", gate, "撤权后不得发送"])
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    task_id, task_key = created.json()["id"], created.json()["task_key"]
    base = f"/api/v1/projects/{world.project['id']}"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await gate.wait_reached()
        await wait_until(lambda: (hub := engine.hubs.get(task_id)) is not None and hub.text == "已发送快照")
        member = await _member(app, world)
        await wait_until(lambda: server.started, within=10)
        headers = {"Authorization": f"Bearer {guest.cookies.get(ACCESS_TOKEN_COOKIE)}"}
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10, headers=headers) as net:
            async with net.stream("GET", f"/api/v1/tasks/{task_id}/stream", params={"task_key": task_key}) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                event, data = await asyncio.wait_for(_frame(lines), 5)
                assert event == "snapshot" and data["text"] == "已发送快照"
                assert data["task"]["status"] == "running"
                pending = asyncio.create_task(_frames_until_error(lines))
                try:
                    url = f"{base}/collaborators/{world.guest['id']}"
                    if action == "revoke":
                        changed = await client.delete(url, params={"expected_revision": member.revision})
                        assert changed.status_code == 204, changed.text
                    else:
                        body = {"expected_revision": member.revision}
                        body.update({"role": "viewer"} if action == "downgrade" else {"allow_owner_ai": True})
                        changed = await client.patch(url, json=body)
                        assert changed.status_code == 200, changed.text
                    frames = await asyncio.wait_for(pending, 3)
                    assert frames[-1] == ("error", {
                        "task": None, "detail": "任务访问权限已失效", "code": "permission_denied",
                    }), frames
                    assert all(event not in ("delta", "done") for event, _ in frames), frames
                    assert [line async for line in lines if line.startswith("event: ")] == []
                    async with app.state.sessionmaker() as db:
                        assert (await db.get(Task, task_id)).status == "running"
                finally:
                    if not pending.done():
                        pending.cancel()
                        with suppress(asyncio.CancelledError):
                            await pending
    finally:
        gate.release()
        server.should_exit = True
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(runner, 10)
        sock.close()
    row = await _settled(app, task_id)
    assert row.status != "succeeded"
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, world.chapter["id"])
        assert chapter.content == "原正文内容" and "撤权后不得发送" not in (row.output_text or "")


async def test_notification_is_project_scoped_and_failures_do_not_notify(app, client, make_client, monkeypatch):
    guest, user, project, member = await _accepted_editor(client, make_client)
    engine = app.state.task_engine
    calls = []
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *args: calls.append(args))
    base = f"/api/v1/projects/{project['id']}"
    stale = await client.patch(f"{base}/collaborators/{user['id']}", json={"expected_revision": 1, "role": "viewer"})
    assert stale.status_code == 409 and calls == []
    changed = await client.patch(f"{base}/collaborators/{user['id']}", json={
        "expected_revision": member["revision"], "role": "viewer",
    })
    assert changed.status_code == 200 and calls == [(project["id"], user["id"])]
    series = await client.post("/api/v1/series", json={"name": "不相关系列"})
    invitation = await client.post(f"/api/v1/series/{series.json()['id']}/collaborators", json={
        "username": "notify-editor",
    })
    assert invitation.status_code == 201
    accepted = await guest.post(f"/api/v1/collaboration/invitations/series/{invitation.json()['invitation_id']}/accept",
                                json={"expected_revision": 1})
    assert accepted.status_code == 200 and calls == [(project["id"], user["id"])]


async def test_invitation_lifecycle_notifies_only_real_project_target(app, client, make_client, monkeypatch):
    await register(client, "notify-life-owner")
    guest, other = make_client(), make_client()
    user = await register(guest, "notify-life-guest")
    await register(other, "notify-life-other")
    project = await create_project(client)
    calls = []
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *args: calls.append(args))
    base = f"/api/v1/projects/{project['id']}"
    invited = await client.post(f"{base}/collaborators", json={"username": "notify-life-guest", "role": "viewer"})
    assert invited.status_code == 201
    member = invited.json()
    expected = (project["id"], user["id"])
    assert calls == [expected]
    path = f"/api/v1/collaboration/invitations/project/{member['invitation_id']}"
    assert (await other.post(f"{path}/accept", json={"expected_revision": 1})).status_code == 404
    assert calls == [expected]
    assert (await guest.post(f"{path}/decline", json={"expected_revision": 1})).status_code == 200
    assert calls == [expected, expected]
    reinvited = await client.post(f"{base}/collaborators", json={"username": "notify-life-guest", "role": "editor"})
    assert reinvited.status_code == 201
    member = reinvited.json()
    path = f"/api/v1/collaboration/invitations/project/{member['invitation_id']}"
    assert (await guest.post(f"{path}/accept", json={"expected_revision": member["revision"]})).status_code == 200
    assert calls == [expected] * 4


async def test_internal_project_write_requires_notifier_before_mutation(app, client, make_client):
    owner = await register(client, "notify-internal-owner")
    guest = await register(make_client(), "notify-internal-guest")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        actor = await db.get(User, owner["id"])
        with pytest.raises(ValueError, match="通知"):
            await collaboration_service.invite(
                db, app.state.sessionmaker, actor, "project", project["id"], "notify-internal-guest", "viewer",
            )
        assert await db.scalar(select(ProjectCollaborator.id).where(ProjectCollaborator.user_id == guest["id"])) is None


async def test_audit_failure_rolls_back_without_notification(app, client, make_client, monkeypatch):
    _guest, user, project, member = await _accepted_editor(client, make_client)
    calls = []
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *args: calls.append(args))

    async def fail(*_args):
        raise RuntimeError("审计写失败")

    monkeypatch.setattr(collaboration_service, "add_success", fail)
    response = await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{user['id']}",
                                   params={"expected_revision": member["revision"]})
    assert response.status_code == 500 and calls == []
    async with app.state.sessionmaker() as db:
        actual = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.user_id == user["id"]))
        assert actual.status == "accepted" and actual.revision == member["revision"]


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_committed_change_is_not_undone_by_notification_error(
    app, client, make_client, monkeypatch, caplog, failure,
):
    _guest, user, project, member = await _accepted_editor(client, make_client)

    def fail(*_args):
        raise failure("不应写入日志的敏感内容")

    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", fail)
    response = await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{user['id']}",
                                   params={"expected_revision": member["revision"]})
    assert response.status_code == 204, response.text
    assert "不应写入日志的敏感内容" not in caplog.text
    async with app.state.sessionmaker() as db:
        actual = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.user_id == user["id"]))
        events = list(await db.scalars(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.target_user_id == user["id"], CollaborationAuditEvent.action == "revoke",
        )))
        assert actual.status == "revoked"
        assert len(events) == 1 and events[0].result == "success"


@pytest.mark.parametrize("released", [True, False])
async def test_unknown_commit_wakes_only_after_connection_is_released(app, client, make_client, monkeypatch, released):
    _guest, user, project, member = await _accepted_editor(client, make_client)
    calls = []
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *args: calls.append(args))
    original_add = collaboration_service.add_success
    original_commit = AsyncSession.commit
    original_rollback = AsyncSession.rollback
    original_invalidate = AsyncSession.invalidate

    async def mark(db, snapshot):
        result = await original_add(db, snapshot)
        if snapshot.action == "revoke":
            db.info["lose_confirmation"] = True
        return result

    async def commit(db):
        await original_commit(db)
        if db.info.pop("lose_confirmation", False):
            db.info["lose_release"] = not released
            raise ConnectionError("提交确认丢失")

    async def rollback(db):
        if db.info.get("lose_release"):
            raise ConnectionError("无法回滚")
        return await original_rollback(db)

    async def invalidate(db):
        if db.info.pop("lose_release", False):
            raise ConnectionError("无法确认连接失效")
        return await original_invalidate(db)

    async def unavailable(*_args):
        raise ConnectionError("审计查询不可用")

    monkeypatch.setattr(collaboration_service, "add_success", mark)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(AsyncSession, "rollback", rollback)
    monkeypatch.setattr(AsyncSession, "invalidate", invalidate)
    monkeypatch.setattr(collaboration_service, "record_failure" if released else "get_result", unavailable)
    response = await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{user['id']}",
                                   params={"expected_revision": member["revision"]})
    assert response.status_code == 500
    assert calls == ([(project["id"], user["id"])] if released else [])
    async with app.state.sessionmaker() as db:
        actual = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.user_id == user["id"]))
        assert actual.status == "revoked"


async def test_repeated_cancellation_cannot_skip_confirmed_commit_notification(app, client, make_client, monkeypatch):
    _guest, user, project, member = await _accepted_editor(client, make_client)
    calls = []
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *args: calls.append(args))
    entered, release = asyncio.Event(), asyncio.Event()
    original_add, original_commit = collaboration_service.add_success, AsyncSession.commit
    original_failure = collaboration_service.record_failure

    async def mark(db, snapshot):
        result = await original_add(db, snapshot)
        if snapshot.action == "revoke":
            db.info["cancel_after_commit"] = True
        return result

    async def commit(db):
        await original_commit(db)
        if db.info.pop("cancel_after_commit", False):
            raise asyncio.CancelledError()

    async def gated_failure(*args):
        entered.set()
        await release.wait()
        return await original_failure(*args)

    monkeypatch.setattr(collaboration_service, "add_success", mark)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(collaboration_service, "record_failure", gated_failure)
    pending = asyncio.create_task(client.delete(f"/api/v1/projects/{project['id']}/collaborators/{user['id']}",
                                                params={"expected_revision": member["revision"]}))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
        assert calls == [(project["id"], user["id"])]
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


@pytest.mark.parametrize("cancelled", [False, True])
async def test_notification_on_committed_but_unconfirmed_change(app, client, make_client, monkeypatch, cancelled):
    _guest, user, project, member = await _accepted_editor(client, make_client)
    calls = []
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *args: calls.append(args))
    original_add = collaboration_service.add_success
    original_commit = AsyncSession.commit

    async def mark(db, snapshot):
        result = await original_add(db, snapshot)
        if snapshot.action == "revoke":
            db.info["lose_confirmation"] = True
        return result

    async def commit(db):
        await original_commit(db)
        if db.info.pop("lose_confirmation", False):
            if cancelled:
                raise asyncio.CancelledError()
            raise ConnectionError("提交确认丢失")

    monkeypatch.setattr(collaboration_service, "add_success", mark)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    request = client.delete(f"/api/v1/projects/{project['id']}/collaborators/{user['id']}",
                            params={"expected_revision": member["revision"]})
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await request
    else:
        response = await request
        assert response.status_code == 409 and response.json()["code"] == "operation_confirmed"
    assert calls == [(project["id"], user["id"])]
    async with app.state.sessionmaker() as db:
        actual = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.user_id == user["id"]))
        events = list(await db.scalars(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.target_user_id == user["id"], CollaborationAuditEvent.action == "revoke",
        )))
        assert actual.status == "revoked"
        assert len(events) == 1 and events[0].result == "success"
