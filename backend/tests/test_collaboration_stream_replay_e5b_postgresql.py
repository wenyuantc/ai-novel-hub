"""E5-b 真实 PostgreSQL：已结束任务的受控回放帧（帧租约 FOR SHARE）与撤权（FOR UPDATE）的两种先后。

真实引擎回放帧经真实中央校验器发送，不替换证明；用 pg_blocking_pids 证明后到的一方确实在数据库层等待。
"""

import asyncio

import httpx
from sqlalchemy import func, select

from app.models import Task
from app.services.task_engine import stream_evidence
from app.services.task_engine.stream_lease import task_sse_response
from tests.helpers import parse_sse
from tests.stream_helpers import mount_stream_response
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_paused_task_postgresql import _gate_revoke
from tests.test_collaboration_rewrite_identity_postgresql import _assert_no_idle_output_transaction
from tests.test_collaboration_stream_replay_e5b import DENIED, _guarded
from tests.test_collaboration_task_privacy import _setup
from tests.test_task_sse_scope import _binding
from tests.test_task_sse_scope_postgresql import _tcp_server

postgres_replay_apps = _postgres_fixture
RESULT_NOTE = "回放结果仅本人可见"


def _clients(owner_app, guest_app):
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver"),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver"),
    )


async def _world(owner_app, guest_app, owner, guest):
    _, editor, project, member = await _setup(guest_app, owner, guest)
    async with owner_app.state.sessionmaker() as db:
        task = Task(user_id=editor["id"], project_id=project["id"], kind="chapter.generate", status="succeeded",
                    title="pg-finished", output_text="回放前的正文", result={"note": RESULT_NOTE}, input={
                        "_collaboration_grant": {"invitation_id": member["invitation_id"],
                                                 "revision": member["revision"]},
                    })
        db.add(task)
        await db.commit()
        identity = (task.id, task.statistics_key)
    binding = await _binding(guest_app, editor["id"], *identity)
    return editor, project, member, binding


def _mount(app, binding, monkeypatch, pid=None):
    if pid is not None:
        original = stream_evidence.verify_stream_frame

        async def recording(db, *args):
            if not pid.done():
                pid.set_result(await db.scalar(select(func.pg_backend_pid())))
            return await original(db, *args)

        # 只记录持锁连接，仍调用真实中央校验器。
        monkeypatch.setattr(stream_evidence, "verify_stream_frame", recording)

    async def endpoint():
        # 先由真实引擎产出带证明的回放帧，再发送：撤权后的拒绝只能来自发送时的帧租约与中央校验。
        items = [item async for item in _guarded(app.state.task_engine, binding)]
        assert [item[0] for item in items] == ["snapshot", "done"]

        async def events():
            for item in items:
                yield item

        return task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding)

    mount_stream_response(app, "/_test/task-frame", endpoint)


async def _cancel(*tasks):
    for item in tasks:
        if item is not None and not item.done():
            item.cancel()
    await asyncio.gather(*(item for item in tasks if item is not None), return_exceptions=True)


async def test_postgres_replay_lease_first_blocks_revoke_then_denies_next_frame(postgres_replay_apps, monkeypatch):
    owner_app, guest_app = postgres_replay_apps
    owner, guest = _clients(owner_app, guest_app)
    entered, release = asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    async with owner, guest:
        editor, project, member, binding = await _world(owner_app, guest_app, owner, guest)
        _mount(guest_app, binding, monkeypatch, pid)
        async with _tcp_server(guest_app, entered, release) as tcp:
            pending = asyncio.create_task(tcp.get("/_test/task-frame"))
            revoke = None
            try:
                # 快照帧已通过证明、仍在实际 send 内持有 FOR SHARE。
                await asyncio.wait_for(entered.wait(), 10)
                backend_pid = await asyncio.wait_for(pid, 5)
                revoke = asyncio.create_task(owner.delete(
                    f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}",
                    params={"expected_revision": member["revision"]},
                ))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, backend_pid), 10)
                assert not revoke.done()
                release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                response = await asyncio.wait_for(pending, 10)
                assert response.status_code == 200
                events = parse_sse(response.text)
                # 撤权前发出的快照不撤回；撤权提交后的终态帧不得发出，且不带结果。
                assert events[0][0] == "snapshot" and events[0][1]["text"] == "回放前的正文"
                assert events[1:] == [("error", DENIED)]
                assert RESULT_NOTE not in response.text
                await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, backend_pid)
            finally:
                release.set()
                await _cancel(pending, revoke)


async def test_postgres_revoke_first_holds_lock_and_replay_is_denied(postgres_replay_apps, monkeypatch):
    owner_app, guest_app = postgres_replay_apps
    owner, guest = _clients(owner_app, guest_app)
    entered, release_send = asyncio.Event(), asyncio.Event()
    release_send.set()
    async with owner, guest:
        editor, project, member, binding = await _world(owner_app, guest_app, owner, guest)
        holder, reached, release = {}, asyncio.Event(), asyncio.Event()
        _gate_revoke(monkeypatch, holder, reached, release)
        _mount(guest_app, binding, monkeypatch)
        revoke = asyncio.create_task(owner.delete(
            f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}",
            params={"expected_revision": member["revision"]},
        ))
        pending = None
        try:
            # 撤权已在共同锁序内持锁、尚未提交。
            await asyncio.wait_for(reached.wait(), 10)
            async with _tcp_server(guest_app, entered, release_send) as tcp:
                pending = asyncio.create_task(tcp.get("/_test/task-frame"))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 10)
                assert not pending.done() and not entered.is_set()
                release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                response = await asyncio.wait_for(pending, 10)
            assert response.status_code == 200
            # 等锁后按已提交的撤权复查：任何回放帧（含快照）都不发出。
            assert parse_sse(response.text) == [("error", DENIED)]
            assert "回放前的正文" not in response.text and RESULT_NOTE not in response.text
            assert not entered.is_set()
        finally:
            release.set()
            await _cancel(pending, revoke)
