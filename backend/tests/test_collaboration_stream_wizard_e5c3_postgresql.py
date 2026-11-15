"""E5-c3 真实 PostgreSQL：协作者向导运行中帧 / done 帧的发送（帧租约共享锁）与撤权（写锁）串行化、两个订阅者与
运行体发送 lease 互不阻塞，以及另一实例撤权、本实例无通知时的失败关闭。

真实引擎（只启动订阅所在实例）经真实中央校验器与 TCP 发送，不替换证明；用 pg_blocking_pids 证明后到的一方
确实在数据库层等待。撤权由另一实例提交，本实例收不到进程内通知，只能靠发送时锁内复查。
"""

import asyncio

from sqlalchemy import func, select

from app.services import collaboration_ai_guard
from app.services.task_engine import stream_evidence
from tests.helpers import Gate, wait_until
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_entity_entry_e4g3b import _member, _world
from tests.test_collaboration_paused_task_postgresql import _gate_revoke
from tests.test_collaboration_rewrite_identity_postgresql import _assert_no_idle_output_transaction
from tests.test_collaboration_stream_replay_e5b import DENIED
from tests.test_collaboration_stream_running_e5c1 import _Hold, _mount, _Stream
from tests.test_collaboration_stream_running_e5c1_postgresql import _cancel, _clients, _revoke, _running, _serve
from tests.test_collaboration_stream_wizard_e5c3 import (
    PARTIAL,
    RESUME_HEAD,
    RESUME_MARKER,
    RESUME_TAIL,
    _caught_up,
    _resume_source,
    _text,
)
from tests.test_collaboration_wizard_entry_e4g3c import ANSWERS

postgres_wizard_stream_apps = _postgres_fixture
HEAD, MARKER, AFTER = '[{"name":"快照', '标记甲"},', '{"name":"撤权后不得生成"}]'


async def _begin(guest_app, owner, guest, fake_llm, chunks):
    guest_app.state.settings.allow_private_ai_endpoints = True
    guest_app.state.settings.auto_analyze_enabled = False
    world = await _world(owner, guest, allow_owner_ai=True)
    fake_llm.push(chunks)
    # 只启动订阅所在实例的引擎：另一实例的撤权通知到不了这里。
    await guest_app.state.task_engine.start()
    created = await guest.post(f"{world.base}/wizard/characters", json={})
    assert created.status_code == 202, created.text
    return world, created.json()


async def test_postgres_wizard_frame_lease_first_blocks_revoke_then_denies_next(
    postgres_wizard_stream_apps, fake_llm, monkeypatch,
):
    owner_app, guest_app = postgres_wizard_stream_apps
    owner, guest = _clients(owner_app, guest_app)
    entered, release = asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    original = stream_evidence.verify_stream_frame

    async def recording(db, task, project, event, data, evidence):
        result = await original(db, task, project, event, data, evidence)
        if result and event == "delta" and data.get("text") == MARKER and not pid.done():
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))
        return result

    # 只记录持锁连接，仍调用真实中央校验器。
    monkeypatch.setattr(stream_evidence, "verify_stream_frame", recording)
    gate1, gate2 = Gate(), Gate()
    async with owner, guest:
        world, task = await _begin(guest_app, owner, guest, fake_llm, [HEAD, gate1, MARKER, gate2, AFTER])
        await gate1.wait_reached(within=10)
        engine = guest_app.state.task_engine
        await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text, within=10)
        member = await _member(guest_app, world)
        _mount(guest_app)
        stream, revoke = None, None
        try:
            streams = []
            async with _serve(guest_app, hold_body="标记甲", entered=entered, release=release,
                              streams=streams) as base:
                stream = _Stream(base, guest, task)
                streams.append(stream)
                await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                gate1.release()
                # 带标记的运行帧已通过未决凭据只读核对，正在实际 send 内持 FOR SHARE。
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
    assert events[0][0] == "snapshot" and events[0][1]["text"] == HEAD
    # 撤权前已发出的帧不撤回；撤权提交后的后续帧不得发出、不带结果。
    assert any(event == "delta" and data["text"] == MARKER for event, data in events)
    assert events[-1] == ("error", DENIED), events
    assert "撤权后不得生成" not in stream.text and '"result"' not in stream.text
    assert not any(event == "done" for event, _ in events)


async def test_postgres_revoke_first_holds_lock_and_produced_wizard_frame_is_denied(
    postgres_wizard_stream_apps, fake_llm, monkeypatch,
):
    owner_app, guest_app = postgres_wizard_stream_apps
    owner, guest = _clients(owner_app, guest_app)
    hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == MARKER)
    gate1, gate2 = Gate(), Gate()
    async with owner, guest:
        world, task = await _begin(guest_app, owner, guest, fake_llm, [HEAD, gate1, MARKER, gate2, AFTER])
        await gate1.wait_reached(within=10)
        engine = guest_app.state.task_engine
        await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text, within=10)
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
    assert MARKER not in stream.text and "撤权后不得生成" not in stream.text


async def test_postgres_revoke_first_blocks_done_frame_lease_then_denies(
    postgres_wizard_stream_apps, fake_llm, monkeypatch,
):
    """done 帧（保存后的仅发布 guard、终态只读核对）：撤权先持锁 → 帧租约等待 → 撤权提交后 done 不发出。"""
    owner_app, guest_app = postgres_wizard_stream_apps
    owner, guest = _clients(owner_app, guest_app)
    hold = _Hold(lambda item: item[0] == "done")
    answer = ANSWERS["characters"]
    gate1, end = Gate(), Gate()
    async with owner, guest:
        world, task = await _begin(guest_app, owner, guest, fake_llm, [answer[:8], gate1, answer[8:], end])
        await gate1.wait_reached(within=10)
        engine = guest_app.state.task_engine
        await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text, within=10)
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
                await end.wait_reached(within=10)
                await stream.wait_for(lambda events: _caught_up(events, engine.hubs[task["id"]], answer))
                end.release()
                await asyncio.wait_for(hold.reached.wait(), 15)
                revoke = asyncio.create_task(_revoke(owner, world, member.revision))
                await asyncio.wait_for(reached.wait(), 10)
                hold.proceed.set()
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 10)
                assert '"result"' not in stream.text
                release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                events = await stream.finish()
        finally:
            release.set()
            hold.proceed.set()
            gate1.release()
            end.release()
            if stream is not None:
                await stream.aclose()
            await _cancel(revoke)
            await guest_app.state.task_engine.stop()
    assert _text(events[:-1]) == answer
    assert events[-1] == ("error", DENIED), events
    assert '"result"' not in stream.text and not any(event == "done" for event, _ in events)


async def test_postgres_cross_instance_revoke_without_notification_denies_wizard_frame(
    postgres_wizard_stream_apps, fake_llm, monkeypatch,
):
    owner_app, guest_app = postgres_wizard_stream_apps
    owner, guest = _clients(owner_app, guest_app)
    hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == MARKER)
    notified = []
    gate1, gate2 = Gate(), Gate()
    async with owner, guest:
        world, task = await _begin(guest_app, owner, guest, fake_llm, [HEAD, gate1, MARKER, gate2, AFTER])
        original_notify = guest_app.state.task_engine.notify_collaboration_change

        def recording_notify(*args, **kwargs):
            notified.append(args)
            return original_notify(*args, **kwargs)

        # 建立协作之后才开始记录：之后的撤权只经另一实例提交。
        monkeypatch.setattr(guest_app.state.task_engine, "notify_collaboration_change", recording_notify)
        await gate1.wait_reached(within=10)
        engine = guest_app.state.task_engine
        await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text, within=10)
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
    assert MARKER not in stream.text and "撤权后不得生成" not in stream.text


async def test_postgres_two_subscribers_and_wizard_send_lease_do_not_block_each_other(
    postgres_wizard_stream_apps, fake_llm, monkeypatch,
):
    """只读核对与帧租约同为共享锁，不升锁。

    续写向导在真实发送前已发布 stage 与续写前缀；运行体随后在真实发送 lease 内（已持可见性共享门禁、作品/Task/
    续写来源 FOR SHARE 与配置锁）停住时，订阅者 A 的快照帧租约取锁并在实际 send 内停住；订阅者 B 的快照帧租约
    同时取锁发出；放行运行体后其发送照常完成（模型流开始）而 A 仍持锁。三方最终都收到 done。
    """
    owner_app, guest_app = postgres_wizard_stream_apps
    owner, guest = _clients(owner_app, guest_app)
    guest_app.state.settings.allow_private_ai_endpoints = True
    guest_app.state.settings.auto_analyze_enabled = False
    entered, release = asyncio.Event(), asyncio.Event()
    send_entered, send_release = asyncio.Event(), asyncio.Event()
    armed, send_pid = {"on": False}, {}
    original_verify = collaboration_ai_guard.SendGuard.verify

    async def gated_verify(self, db):
        await original_verify(self, db)
        if armed["on"] and not send_entered.is_set():
            armed["on"] = False
            send_pid["pid"] = await db.scalar(select(func.pg_backend_pid()))
            send_entered.set()
            await send_release.wait()

    # 在构建 guard 之前替换（prepare_provider 绑定的是该方法），仍先执行真实发送核验。
    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "verify", gated_verify)
    async with owner, guest:
        world = await _world(owner, guest, allow_owner_ai=True)
        await guest_app.state.task_engine.start()
        source = await _resume_source(guest_app, guest, world, fake_llm)
        gate3, end = Gate(), Gate()
        fake_llm.push([gate3, RESUME_HEAD, RESUME_MARKER, RESUME_TAIL, end])
        armed["on"] = True
        created = await guest.post(f"{world.base}/wizard/outline", json={
            "continue_from_task_id": source.id, "continue_from_task_key": source.statistics_key,
        })
        assert created.status_code == 202, created.text
        task = created.json()
        engine = guest_app.state.task_engine
        # 运行体已发布 stage 与前缀，并在真实发送 lease 内持共享锁停住。
        await asyncio.wait_for(send_entered.wait(), 10)
        assert engine.hubs[task["id"]].text.startswith('[{"title":"续一"')
        _mount(guest_app)
        stream_a = stream_b = None
        try:
            streams = []
            async with _serve(guest_app, hold_body="续一", entered=entered, release=release,
                              streams=streams) as base:
                stream_a = _Stream(base, guest, task)
                streams.append(stream_a)
                # A 的快照帧租约在运行体持发送 lease 时取得全部共享锁，并在实际 send 内停住。
                await asyncio.wait_for(entered.wait(), 10)
                stream_b = _Stream(base, guest, task)
                streams.append(stream_b)
                await stream_b.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                assert stream_b.events()[0][1]["text"].startswith('[{"title":"续一"')
                # 运行体的发送 lease 在 A 仍持锁时完成：模型流开始（到达 gate3）。
                send_release.set()
                await gate3.wait_reached(within=10)
                assert not stream_a.closed.is_set() and "permission_denied" not in stream_b.text
                release.set()
                gate3.release()
                full = PARTIAL[:PARTIAL.rfind("}") + 1] + RESUME_HEAD + RESUME_MARKER + RESUME_TAIL
                await end.wait_reached(within=10)
                for item in (stream_a, stream_b):
                    await item.wait_for(lambda events: _caught_up(events, engine.hubs[task["id"]], full))
                end.release()
                events_a = await stream_a.finish(within=20)
                events_b = await stream_b.finish(within=20)
                await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, send_pid["pid"])
        finally:
            release.set()
            send_release.set()
            gate3.release()
            end.release()
            for item in (stream_a, stream_b):
                if item is not None:
                    await item.aclose()
            await guest_app.state.task_engine.stop()
    for events in (events_a, events_b):
        assert events[-1][0] == "done", events[-1]
        assert events[-1][1]["task"]["status"] == "succeeded"
    assert "permission_denied" not in stream_a.text + stream_b.text
