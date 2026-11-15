"""E5-c1 真实 PostgreSQL：协作者章节运行中帧的发送（帧租约共享锁）与撤权（写锁）串行化，及跨实例无通知撤权。

真实引擎（只启动订阅所在实例）经真实中央校验器与 TCP 发送，不替换证明；用 pg_blocking_pids 证明后到的一方
确实在数据库层等待。撤权由另一实例提交，本实例收不到进程内通知，只能靠发送时锁内复查。
"""

import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import uvicorn
from sqlalchemy import func, select

from app.services.task_engine import stream_evidence
from tests.helpers import Gate, wait_until
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_chapter_entry_e4f import _member, _world
from tests.test_collaboration_paused_task_postgresql import _gate_revoke
from tests.test_collaboration_rewrite_identity_postgresql import _assert_no_idle_output_transaction
from tests.test_collaboration_stream_replay_e5b import DENIED
from tests.test_collaboration_stream_running_e5c1 import _Hold, _mount, _Stream

postgres_running_apps = _postgres_fixture
MARKER, AFTER = "撤权前已发出的帧", "撤权后不得生成"


def _clients(owner_app, guest_app):
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver"),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver"),
    )


@asynccontextmanager
async def _serve(app, *, hold_body: str | None = None, entered=None, release=None, streams=None):
    """可选：某帧已通过证明、在实际 ASGI send 内（仍持帧租约全部锁）时停住。"""

    async def wrapped(scope, receive, send):
        async def delayed(message):
            body = message.get("body", b"")
            if hold_body is not None and hold_body.encode() in body and not entered.is_set():
                entered.set()
                await release.wait()
            await send(message)

        await app(scope, receive, delayed)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    server = uvicorn.Server(uvicorn.Config(wrapped, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await wait_until(lambda: server.started, within=10)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        if release is not None:
            release.set()
        # 先关闭仍在读取的流，否则服务端关停会一直等待长连接，掩盖原始失败。
        for item in streams or ():
            await item.aclose()
        server.should_exit = True
        await asyncio.wait_for(runner, 15)
        sock.close()


async def _start(guest_app, owner, guest, fake_llm):
    guest_app.state.settings.allow_private_ai_endpoints = True
    world = await _world(owner, guest, allow_owner_ai=True)
    gate1, gate2 = Gate(), Gate()
    fake_llm.push(["撤权前的快照", gate1, MARKER, gate2, AFTER])
    # 只启动订阅所在实例的引擎：另一实例的撤权通知到不了这里。
    await guest_app.state.task_engine.start()
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    task = created.json()
    await gate1.wait_reached(within=10)
    engine = guest_app.state.task_engine
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text, within=10)
    return world, task, gate1, gate2


async def _revoke(owner, world, revision):
    return await owner.delete(f"/api/v1/projects/{world.project['id']}/collaborators/{world.guest['id']}",
                              params={"expected_revision": revision})


async def _cancel(*tasks):
    for item in tasks:
        if item is not None and not item.done():
            item.cancel()
    await asyncio.gather(*(item for item in tasks if item is not None), return_exceptions=True)


async def _running(app, task_id):
    from app.models import Task

    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        return row.status == "running" and row.execution_key is not None


async def test_postgres_running_frame_lease_first_blocks_revoke_then_denies_next(postgres_running_apps, fake_llm,
                                                                                monkeypatch):
    owner_app, guest_app = postgres_running_apps
    owner, guest = _clients(owner_app, guest_app)
    entered, release = asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    original = stream_evidence.verify_stream_frame

    async def recording(db, task, project, event, data, evidence):
        result = await original(db, task, project, event, data, evidence)
        if result and event == "delta" and MARKER in str(data.get("text")) and not pid.done():
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))
        return result

    # 只记录持锁连接，仍调用真实中央校验器。
    monkeypatch.setattr(stream_evidence, "verify_stream_frame", recording)
    async with owner, guest:
        world, task, gate1, gate2 = await _start(guest_app, owner, guest, fake_llm)
        member = await _member(guest_app, world)
        _mount(guest_app)
        stream, revoke = None, None
        try:
            streams = []
            async with _serve(guest_app, hold_body=MARKER, entered=entered, release=release,
                              streams=streams) as base:
                stream = _Stream(base, guest, task)
                streams.append(stream)
                await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                gate1.release()
                # 带标记的运行帧已通过章节占用只读核对，正在实际 send 内持 FOR SHARE。
                await asyncio.wait_for(entered.wait(), 10)
                backend_pid = await asyncio.wait_for(pid, 5)
                await gate2.wait_reached(within=10)
                revoke = asyncio.create_task(_revoke(owner, world, member.revision))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, backend_pid), 10)
                assert not revoke.done()
                release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                gate2.release()
                events = await stream.finish()
                await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, backend_pid)
        finally:
            release.set()
            gate1.release()
            gate2.release()
            if stream is not None:
                await stream.aclose()
            await _cancel(revoke)
            await guest_app.state.task_engine.stop()
    assert events[0][0] == "snapshot" and events[0][1]["text"] == "撤权前的快照"
    # 撤权前已发出的帧不撤回；撤权提交后的后续帧不得发出、不带结果。
    assert any(event == "delta" and data["text"] == MARKER for event, data in events)
    assert events[-1] == ("error", DENIED), events
    assert AFTER not in stream.text and '"result"' not in stream.text
    assert not any(event == "done" for event, _ in events)


async def test_postgres_revoke_first_holds_lock_and_produced_frame_is_denied(postgres_running_apps, fake_llm,
                                                                           monkeypatch):
    owner_app, guest_app = postgres_running_apps
    owner, guest = _clients(owner_app, guest_app)
    hold = _Hold(lambda item: item[0] == "delta" and MARKER in item[1].get("text", ""))
    async with owner, guest:
        world, task, gate1, gate2 = await _start(guest_app, owner, guest, fake_llm)
        member = await _member(guest_app, world)
        _mount(guest_app, [hold])
        holder, reached, release = {}, asyncio.Event(), asyncio.Event()
        _gate_revoke(monkeypatch, holder, reached, release)
        stream, revoke = None, None
        try:
            streams = []
            async with _serve(guest_app, streams=streams) as base:
                stream = _Stream(base, guest, task)
                streams.append(stream)
                await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                gate1.release()
                # 引擎已核验并产出该帧，订阅侧尚未发送；运行体阻塞在 gate2。
                await asyncio.wait_for(hold.reached.wait(), 10)
                await gate2.wait_reached(within=10)
                revoke = asyncio.create_task(_revoke(owner, world, member.revision))
                # 撤权已在共同锁序内持锁、尚未提交。
                await asyncio.wait_for(reached.wait(), 10)
                before = len(stream.events())
                hold.proceed.set()
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 10)
                assert len(stream.events()) == before and MARKER not in stream.text
                release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                events = await stream.finish()
                # 等锁后按已提交的撤权复查：该帧不发出；运行体仍阻塞，拒绝来自发送侧。
                assert await _running(guest_app, task["id"])
        finally:
            release.set()
            hold.proceed.set()
            gate1.release()
            gate2.release()
            if stream is not None:
                await stream.aclose()
            await _cancel(revoke)
            await guest_app.state.task_engine.stop()
    assert events[0][0] == "snapshot"
    assert events[1:] == [("error", DENIED)], events
    assert MARKER not in stream.text and AFTER not in stream.text


async def test_postgres_cross_instance_revoke_without_notification_denies_next_frame(
    postgres_running_apps, fake_llm, monkeypatch,
):
    owner_app, guest_app = postgres_running_apps
    owner, guest = _clients(owner_app, guest_app)
    hold = _Hold(lambda item: item[0] == "delta" and MARKER in item[1].get("text", ""))
    notified = []
    original_notify = guest_app.state.task_engine.notify_collaboration_change

    def recording_notify(*args, **kwargs):
        notified.append(args)
        return original_notify(*args, **kwargs)

    async with owner, guest:
        world, task, gate1, gate2 = await _start(guest_app, owner, guest, fake_llm)
        # 建立协作（本人接受邀请经本实例）之后才开始记录：之后的撤权只经另一实例提交。
        monkeypatch.setattr(guest_app.state.task_engine, "notify_collaboration_change", recording_notify)
        member = await _member(guest_app, world)
        _mount(guest_app, [hold])
        stream = None
        try:
            streams = []
            async with _serve(guest_app, streams=streams) as base:
                stream = _Stream(base, guest, task)
                streams.append(stream)
                await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                gate1.release()
                await asyncio.wait_for(hold.reached.wait(), 10)
                await gate2.wait_reached(within=10)
                # 撤权经另一实例提交：本实例引擎收不到进程内通知，运行体也未被停止。
                assert (await _revoke(owner, world, member.revision)).status_code == 204
                assert notified == [] and await _running(guest_app, task["id"])
                hold.proceed.set()
                events = await stream.finish()
                assert await _running(guest_app, task["id"])
        finally:
            hold.proceed.set()
            gate1.release()
            gate2.release()
            if stream is not None:
                await stream.aclose()
            await guest_app.state.task_engine.stop()
    assert events[0][0] == "snapshot"
    assert events[1:] == [("error", DENIED)], events
    assert MARKER not in stream.text and AFTER not in stream.text


async def test_postgres_two_subscribers_and_runner_send_lease_do_not_block_each_other(
    postgres_running_apps, fake_llm, monkeypatch,
):
    """复核补充：只读核对与帧租约同为共享锁，不升锁。

    运行体在第二轮真实发送 lease 内（已持可见性共享门禁、作品/Task/章节 FOR SHARE）停住时，订阅者 A 的帧租约
    取锁并进入实际 send 停住；此时订阅者 B 的快照帧租约也能取锁发出；随后放行运行体，其发送 lease 照常完成
    （模型流开始）而 A 仍持锁。三方都不互相等待（不会死锁，也不会被 PG 死锁检测打断成 permission_denied）。
    """
    from app.services import chapter_claim

    owner_app, guest_app = postgres_running_apps
    owner, guest = _clients(owner_app, guest_app)
    guest_app.state.settings.allow_private_ai_endpoints = True
    first_hold = _Hold(lambda item: item[0] == "delta" and MARKER in item[1].get("text", ""))
    entered, release = asyncio.Event(), asyncio.Event()
    send_entered, send_release = asyncio.Event(), asyncio.Event()
    armed, send_pid = {"on": False}, {}
    original_sending = chapter_claim.require_sending_chapter

    async def gated_sending(db, claim):
        await original_sending(db, claim)
        if armed["on"] and not send_entered.is_set():
            armed["on"] = False
            send_pid["pid"] = await db.scalar(select(func.pg_backend_pid()))
            send_entered.set()
            await send_release.wait()

    monkeypatch.setattr(chapter_claim, "require_sending_chapter", gated_sending)
    async with owner, guest:
        world = await _world(owner, guest, allow_owner_ai=True)
        gate1, gate3 = Gate(), Gate()
        # 首轮字数不足 → 自动续写：第二轮的真实发送 lease 发生在 A 的帧已产出之后。
        fake_llm.push(["快照", gate1, MARKER])
        fake_llm.push([gate3, "续写第一轮"])
        fake_llm.push("续写第二轮")
        await guest_app.state.task_engine.start()
        created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate",
                                   json={"auto_continue": True, "target_words": 300})
        assert created.status_code == 202, created.text
        task = created.json()
        await gate1.wait_reached(within=10)
        engine = guest_app.state.task_engine
        await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text, within=10)
        _mount(guest_app, [first_hold])
        stream_a = stream_b = None
        try:
            streams = []
            async with _serve(guest_app, hold_body=MARKER, entered=entered, release=release,
                              streams=streams) as base:
                stream_a = _Stream(base, guest, task)
                streams.append(stream_a)
                await stream_a.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                armed["on"] = True
                gate1.release()
                # A 已取得带标记的帧（尚未取帧租约）；运行体已进入第二轮发送 lease 并持共享锁停住。
                await asyncio.wait_for(first_hold.reached.wait(), 10)
                await asyncio.wait_for(send_entered.wait(), 10)
                first_hold.proceed.set()
                # A 的帧租约在运行体持发送 lease 时取得全部共享锁，并在实际 send 内停住。
                await asyncio.wait_for(entered.wait(), 10)
                # B 的快照帧租约与 A、运行体同时持共享锁。
                stream_b = _Stream(base, guest, task)
                streams.append(stream_b)
                await stream_b.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                assert MARKER in stream_b.events()[0][1]["text"]
                # 运行体的发送 lease 在 A 仍持锁时完成：模型流开始（到达 gate3）。
                send_release.set()
                await gate3.wait_reached(within=10)
                assert not stream_a.closed.is_set() and "permission_denied" not in stream_b.text
                release.set()
                gate3.release()
                events_a = await stream_a.finish(within=20)
                events_b = await stream_b.finish(within=20)
                await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, send_pid["pid"])
        finally:
            release.set()
            first_hold.proceed.set()
            send_release.set()
            gate1.release()
            gate3.release()
            for item in (stream_a, stream_b):
                if item is not None:
                    await item.aclose()
            await guest_app.state.task_engine.stop()
    for events in (events_a, events_b):
        assert events[-1][0] == "done", events[-1]
        assert events[-1][1]["task"]["status"] == "succeeded"
    assert "permission_denied" not in stream_a.text + stream_b.text
    assert any(event == "delta" and MARKER in data["text"] for event, data in events_a)
