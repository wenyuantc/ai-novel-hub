"""设定类向导「同执行戳后继」（2026-11-13）真实 PostgreSQL：保存前产出、在本任务收紧可见性的保存提交后才发出的
在途帧，其帧租约（共享锁）与撤权（写锁）两种先后顺序的串行化、跨实例撤权零通知时的失败关闭，以及两个订阅者
在租约外等待完成来源时运行体照常核验与收尾（不死锁）。

真实引擎（只启动订阅所在实例）经真实中央校验器与 TCP 发送，不替换证明；pg_blocking_pids 证明后到的一方确实在
数据库层等待。作品自带角色甲、乙，角色向导整体替换会收紧可见性（递增 revision/epoch）。
"""

import asyncio
from contextlib import aclosing

from sqlalchemy import func, select

from app.services.ai_dispatch import read_visibility_stamp
from app.services.task_engine import stream_evidence
from app.services.task_engine.handlers.wizard import WizardHandler
from tests.helpers import Gate, wait_until
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_entity_entry_e4g3b import _member
from tests.test_collaboration_paused_task_postgresql import _gate_revoke
from tests.test_collaboration_rewrite_identity_postgresql import _assert_no_idle_output_transaction
from tests.test_collaboration_stream_replay_e5b import DENIED
from tests.test_collaboration_stream_running_e5c1 import _Hold, _mount, _settled, _Stream, _task_row
from tests.test_collaboration_stream_running_e5c1_postgresql import _cancel, _clients, _revoke, _serve
from tests.test_collaboration_stream_wizard_e5c3_postgresql import AFTER, HEAD, MARKER, _begin

postgres_wizard_successor_apps = _postgres_fixture
FULL = HEAD + MARKER + AFTER


async def _epoch(app, project_id):
    async with app.state.sessionmaker() as db:
        return (await read_visibility_stamp(db, project_id)).epoch


async def _running_with_text(guest_app, world, task, gate1):
    """运行体已输出快照正文、停在第一个闸前；返回引擎与保存前的 epoch。"""
    await gate1.wait_reached(within=10)
    engine = guest_app.state.task_engine
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text, within=10)
    before = await _epoch(guest_app, world.project["id"])
    return engine, before


async def test_postgres_successor_frame_lease_first_blocks_revoke_then_denies_next(
    postgres_wizard_successor_apps, fake_llm, monkeypatch,
):
    owner_app, guest_app = postgres_wizard_successor_apps
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
    hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == MARKER)
    gate1, gate2 = Gate(), Gate()
    async with owner, guest:
        world, task = await _begin(guest_app, owner, guest, fake_llm, [HEAD, gate1, MARKER, gate2, AFTER])
        engine, before = await _running_with_text(guest_app, world, task, gate1)
        member = await _member(guest_app, world)
        _mount(guest_app, [hold])
        stream, revoke = None, None
        try:
            streams = []
            async with _serve(guest_app, hold_body="标记甲", entered=entered, release=release,
                              streams=streams) as base:
                stream = _Stream(base, guest, task)
                streams.append(stream)
                await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                gate1.release()
                await asyncio.wait_for(hold.reached.wait(), 10)
                gate2.release()
                assert (await _settled(guest_app, task["id"])).status == "succeeded"
                assert await _epoch(guest_app, world.project["id"]) > before
                hold.proceed.set()
                # 在途标记帧经同执行戳后继证明，正在实际 send 内持 FOR SHARE。
                await asyncio.wait_for(entered.wait(), 10)
                backend_pid = await asyncio.wait_for(pid, 5)
                revoke = asyncio.create_task(_revoke(owner, world, member.revision))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, backend_pid), 10)
                assert not revoke.done()
                release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                events = await stream.finish()
                await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, backend_pid)
        finally:
            release.set()
            hold.proceed.set()
            gate1.release()
            gate2.release()
            if stream is not None:
                await stream.aclose()
            await _cancel(revoke)
            await guest_app.state.task_engine.stop()
    assert events[0][0] == "snapshot" and events[0][1]["text"] == HEAD
    # 撤权前已发出的在途帧不撤回；撤权提交后的后续帧（其余正文、done）不得发出。
    assert any(event == "delta" and data["text"] == MARKER for event, data in events)
    assert events[-1] == ("error", DENIED), events
    assert "撤权后不得生成" not in stream.text and '"result"' not in stream.text


async def test_postgres_revoke_first_blocks_successor_frame_lease_then_denies(
    postgres_wizard_successor_apps, fake_llm, monkeypatch,
):
    owner_app, guest_app = postgres_wizard_successor_apps
    owner, guest = _clients(owner_app, guest_app)
    hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == MARKER)
    gate1, gate2 = Gate(), Gate()
    async with owner, guest:
        world, task = await _begin(guest_app, owner, guest, fake_llm, [HEAD, gate1, MARKER, gate2, AFTER])
        engine, before = await _running_with_text(guest_app, world, task, gate1)
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
                gate2.release()
                assert (await _settled(guest_app, task["id"])).status == "succeeded"
                assert await _epoch(guest_app, world.project["id"]) > before
                revoke = asyncio.create_task(_revoke(owner, world, member.revision))
                # 撤权已在共同锁序内持锁、尚未提交。
                await asyncio.wait_for(reached.wait(), 10)
                before_events = len(stream.events())
                hold.proceed.set()
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 10)
                assert len(stream.events()) == before_events and MARKER not in stream.text
                release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                events = await stream.finish()
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
    assert MARKER not in stream.text and '"result"' not in stream.text


async def test_postgres_cross_instance_revoke_without_notification_denies_successor_frame(
    postgres_wizard_successor_apps, fake_llm, monkeypatch,
):
    owner_app, guest_app = postgres_wizard_successor_apps
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
        engine, before = await _running_with_text(guest_app, world, task, gate1)
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
                gate2.release()
                assert (await _settled(guest_app, task["id"])).status == "succeeded"
                assert await _epoch(guest_app, world.project["id"]) > before
                assert (await _revoke(owner, world, member.revision)).status_code == 204
                assert notified == []
                hold.proceed.set()
                events = await stream.finish()
        finally:
            hold.proceed.set()
            gate1.release()
            gate2.release()
            if stream is not None:
                await stream.aclose()
            await guest_app.state.task_engine.stop()
    assert events[0][0] == "snapshot"
    assert events[1:] == [("error", DENIED)], events
    assert MARKER not in stream.text and '"result"' not in stream.text


async def test_postgres_two_subscribers_wait_outside_lease_while_runner_finalizes(
    postgres_wizard_successor_apps, fake_llm, monkeypatch,
):
    """保存已提交、done 尚未交给引擎核验时，订阅者 A 的在途标记帧与新订阅者 B 的快照都不能补签，只能退出
    租约等待同一 hub 的完成来源；等待期间不持锁，运行体的 done 核验与终态写入（写锁）照常完成，
    随后两者经后继证明发出并收到 done。"""
    owner_app, guest_app = postgres_wizard_successor_apps
    owner, guest = _clients(owner_app, guest_app)
    waits: list[str] = []
    original_verify = stream_evidence.verify_stream_frame

    async def spy(db, task, project, event, data, evidence):
        try:
            return await original_verify(db, task, project, event, data, evidence)
        except stream_evidence.PublicationSourcePending:
            waits.append(event)
            raise

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", spy)
    done_reached, done_release = asyncio.Event(), asyncio.Event()
    original_run = WizardHandler.run

    def run(self, job, ctx):
        async def events():
            async with aclosing(original_run(self, job, ctx)) as source:
                async for event, data in source:
                    if event == "done":
                        done_reached.set()
                        await done_release.wait()
                    yield event, data

        return events()

    monkeypatch.setattr(WizardHandler, "run", run)
    hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == MARKER)
    gate1, gate2 = Gate(), Gate()
    async with owner, guest:
        world, task = await _begin(guest_app, owner, guest, fake_llm, [HEAD, gate1, MARKER, gate2, AFTER])
        engine, before = await _running_with_text(guest_app, world, task, gate1)
        _mount(guest_app, [hold])
        stream_a = stream_b = None
        try:
            streams = []
            async with _serve(guest_app, streams=streams) as base:
                stream_a = _Stream(base, guest, task)
                streams.append(stream_a)
                await stream_a.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                gate1.release()
                await asyncio.wait_for(hold.reached.wait(), 10)
                gate2.release()
                await asyncio.wait_for(done_reached.wait(), 10)
                row = await _task_row(guest_app, task["id"])
                assert row.status == "running" and "_wizard_completion" in row.checkpoint
                assert await _epoch(guest_app, world.project["id"]) > before
                hold.proceed.set()
                await wait_until(lambda: "delta" in waits, within=10)
                stream_b = _Stream(base, guest, task)
                streams.append(stream_b)
                await wait_until(lambda: "snapshot" in waits, within=10)
                assert MARKER not in stream_a.text and stream_b.events() == []
                # 两个订阅者都在租约外等待；运行体的 done 核验与终态写入不被任何帧锁阻塞。
                done_release.set()
                assert (await _settled(guest_app, task["id"])).status == "succeeded"
                events_a = await stream_a.finish(within=20)
                events_b = await stream_b.finish(within=20)
        finally:
            hold.proceed.set()
            done_release.set()
            gate1.release()
            gate2.release()
            for item in (stream_a, stream_b):
                if item is not None:
                    await item.aclose()
            await guest_app.state.task_engine.stop()
    for events in (events_a, events_b):
        assert events[-1][0] == "done", events[-1]
        assert events[-1][1]["task"]["status"] == "succeeded"
    snapshot = next(data["text"] for event, data in events_a if event == "snapshot")
    assert snapshot + "".join(data["text"] for event, data in events_a if event == "delta") == FULL
    assert next(data["text"] for event, data in events_b if event == "snapshot") == FULL
    assert "permission_denied" not in stream_a.text + stream_b.text
