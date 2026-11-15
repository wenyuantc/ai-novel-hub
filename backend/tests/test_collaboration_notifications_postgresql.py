"""PostgreSQL 事务提交与真实 HTTP 任务流的协作唤醒顺序。"""

import asyncio
import socket
from contextlib import suppress

import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chapter, Task
from app.services import collaboration_service
from tests.helpers import Gate, wait_until
from tests.test_collaboration_chapter_entry_e4f import _member, _world
from tests.test_collaboration_notifications import _frame

pytest_plugins = ("tests.test_collaboration_task_postgresql",)


@pytest.mark.parametrize("action", ["revoke", "downgrade"])
async def test_postgres_stream_wakes_only_after_commit(postgres_collaboration_apps, fake_llm, monkeypatch, action):
    """E5-f：协作者经生产入口受控订阅真实运行的章节任务；变更事务提交前不唤醒、不发帧，
    提交后进程内通知唤醒订阅复核并以 permission_denied 结束。执行体不被通知取消（订阅唤醒独立承重）。"""
    app, _other_app = postgres_collaboration_apps
    app.state.settings.allow_private_ai_endpoints = True
    app.state.settings.auto_analyze_enabled = False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    gate = Gate()
    try:
        await wait_until(lambda: server.started, within=10)
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as guest,
        ):
            world = await _world(owner, guest)
            user = world.guest
            base = f"/api/v1/projects/{world.project['id']}"
            engine = app.state.task_engine
            engine.heartbeat_seconds = 120
            engine.collaboration_scan_interval = 3600

            async def keep_running(*_args):
                return None

            monkeypatch.setattr(engine, "_check_collaboration_runs", keep_running)
            await engine.start()
            fake_llm.push(["原快照", gate, "撤权后不得发送"])
            created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
            assert created.status_code == 202, created.text
            task_id, key = created.json()["id"], created.json()["task_key"]
            await gate.wait_reached(within=10)
            await wait_until(lambda: (hub := engine.hubs.get(task_id)) is not None and hub.text == "原快照",
                             within=10)
            member = await _member(app, world)
            notified = []
            original_notify = engine.notify_collaboration_change

            def observe(project_id, actor_id):
                notified.append((project_id, actor_id))
                original_notify(project_id, actor_id)

            monkeypatch.setattr(engine, "notify_collaboration_change", observe)
            entered, release = asyncio.Event(), asyncio.Event()
            original_add, original_commit = collaboration_service.add_success, AsyncSession.commit

            async def mark(db, snapshot):
                result = await original_add(db, snapshot)
                if snapshot.action == ("revoke" if action == "revoke" else "modify"):
                    db.info["wait_notify_commit"] = True
                return result

            async def gated_commit(db):
                if db.info.pop("wait_notify_commit", False):
                    entered.set()
                    await release.wait()
                return await original_commit(db)

            monkeypatch.setattr(collaboration_service, "add_success", mark)
            monkeypatch.setattr(AsyncSession, "commit", gated_commit)
            async with guest.stream("GET", f"/api/v1/tasks/{task_id}/stream", params={"task_key": key}) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                event, data = await asyncio.wait_for(_frame(lines), 10)
                assert event == "snapshot" and data["text"] == "原快照"
                next_frame = asyncio.create_task(_frame(lines))
                change = asyncio.create_task(
                    owner.delete(f"{base}/collaborators/{user['id']}", params={"expected_revision": member.revision})
                    if action == "revoke" else owner.patch(f"{base}/collaborators/{user['id']}", json={
                        "expected_revision": member.revision, "role": "viewer",
                    })
                )
                try:
                    await asyncio.wait_for(entered.wait(), 10)
                    assert notified == []
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(next_frame), 0.15)
                    release.set()
                    changed = await asyncio.wait_for(change, 10)
                    assert changed.status_code == (204 if action == "revoke" else 200), changed.text
                    event, data = await asyncio.wait_for(next_frame, 10)
                    assert notified == [(world.project["id"], user["id"])]
                    assert event == "error" and data["code"] == "permission_denied" and data["task"] is None
                    async with app.state.sessionmaker() as db:
                        assert (await db.get(Task, task_id)).status == "running"
                finally:
                    release.set()
                    for pending in (change, next_frame):
                        if not pending.done():
                            pending.cancel()
                            with suppress(asyncio.CancelledError):
                                await pending
            gate.release()
            await wait_until(lambda: task_id not in engine._running, within=15)
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task_id)
                chapter = await db.get(Chapter, world.chapter["id"])
            assert row.status != "succeeded" and chapter.content == "原正文内容"
    finally:
        gate.release()
        server.should_exit = True
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(runner, 10)
        sock.close()
