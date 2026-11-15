"""E5-c2 真实 PostgreSQL：协作者批量运行中帧的发送（帧租约共享锁）与撤权（写锁）串行化、两个订阅者与运行体
发送 lease 互不阻塞，以及另一实例撤权、本实例无通知时的失败关闭。

真实引擎（只启动订阅所在实例）经真实中央校验器与 TCP 发送，不替换证明；用 pg_blocking_pids 证明后到的一方
确实在数据库层等待。撤权由另一实例提交，本实例收不到进程内通知，只能靠发送时锁内复查。
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.services import batch_service
from app.services.task_engine import stream_evidence
from tests.helpers import Gate, wait_until
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_batch_entry_e4g2 import _member, _world
from tests.test_collaboration_batch_entry_e4g2 import _start as _batch_start
from tests.test_collaboration_paused_task_postgresql import _gate_revoke
from tests.test_collaboration_rewrite_identity_postgresql import _assert_no_idle_output_transaction
from tests.test_collaboration_stream_batch_e5c2 import ANALYZING, _gate_between, _split_analysis
from tests.test_collaboration_stream_replay_e5b import DENIED
from tests.test_collaboration_stream_running_e5c1 import _Hold, _mount, _Stream
from tests.test_collaboration_stream_running_e5c1_postgresql import _cancel, _clients, _revoke, _running, _serve

postgres_batch_stream_apps = _postgres_fixture
SECOND = "第二章不得生成"
# chapter_start 帧载荷（json.dumps 保持键序）：第二章开始。
CHAPTER_TWO_START = '"index": 1, "total"'


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))


def _settings(app, *, analyze=False):
    app.state.settings.allow_private_ai_endpoints = True
    app.state.settings.auto_analyze_enabled = analyze


async def _begin(guest_app, owner, guest, fake_llm, chunks, *, extra=(), analyze=False):
    _settings(guest_app, analyze=analyze)
    world = await _world(owner, guest, allow_owner_ai=True)
    gate0 = Gate()
    fake_llm.push(["开头", gate0, *chunks])
    for item in extra:
        fake_llm.push(item)
    # 只启动订阅所在实例的引擎：另一实例的撤权通知到不了这里。
    await guest_app.state.task_engine.start()
    created = await _batch_start(guest, world)
    assert created.status_code == 202, created.text
    task = created.json()
    await gate0.wait_reached(within=10)
    engine = guest_app.state.task_engine
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text == "开头", within=10)
    return world, task, gate0


async def test_postgres_batch_frame_lease_first_blocks_revoke_then_denies_next(
    postgres_batch_stream_apps, fake_llm, monkeypatch,
):
    """章与章之间：第二章开始帧已通过证明、在实际 send 内持 FOR SHARE → 撤权被阻塞 → 放行后撤权提交 →
    其后已产出的帧不得发出。"""
    owner_app, guest_app = postgres_batch_stream_apps
    owner, guest = _clients(owner_app, guest_app)
    entered, release = asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    original = stream_evidence.verify_stream_frame

    async def recording(db, task, project, event, data, evidence):
        result = await original(db, task, project, event, data, evidence)
        if result and event == "chapter_start" and data.get("index") == 1 and not pid.done():
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))
        return result

    # 只记录持锁连接，仍调用真实中央校验器。
    monkeypatch.setattr(stream_evidence, "verify_stream_frame", recording)
    between_entered, between_release = _gate_between(monkeypatch)
    async with owner, guest:
        world, task, gate0 = await _begin(guest_app, owner, guest, fake_llm, ["第一章其余"], extra=(SECOND,))
        member = await _member(guest_app, world)
        _mount(guest_app)
        stream, revoke = None, None
        try:
            streams = []
            async with _serve(guest_app, hold_body=CHAPTER_TWO_START, entered=entered, release=release,
                              streams=streams) as base:
                stream = _Stream(base, guest, task)
                streams.append(stream)
                await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                gate0.release()
                await asyncio.wait_for(between_entered.wait(), 10)
                await asyncio.wait_for(entered.wait(), 10)
                backend_pid = await asyncio.wait_for(pid, 5)
                revoke = asyncio.create_task(_revoke(owner, world, member.revision))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, backend_pid), 10)
                assert not revoke.done()
                release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                events = await stream.finish()
                await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, backend_pid)
                # 拒绝来自发送侧：运行体仍停在第二章之前。
                assert await _running(guest_app, task["id"])
        finally:
            release.set()
            gate0.release()
            between_release.set()
            if stream is not None:
                await stream.aclose()
            await _cancel(revoke)
            await guest_app.state.task_engine.stop()
    assert events[0][0] == "snapshot"
    # 撤权前已发出的帧不撤回；撤权提交后的后续帧不得发出、不带结果。
    assert any(event == "chapter_start" and data["index"] == 1 for event, data in events)
    assert events[-1] == ("error", DENIED), events
    assert SECOND not in stream.text and '"result"' not in stream.text
    assert not any(event == "done" for event, _ in events)


async def test_postgres_revoke_first_holds_lock_and_analysis_frame_is_denied(
    postgres_batch_stream_apps, fake_llm, monkeypatch,
):
    """逐章分析中：撤权先在共同锁序内持锁未提交 → 已产出的「分析中」帧的帧租约被阻塞 → 撤权提交后该帧不发出。"""
    owner_app, guest_app = postgres_batch_stream_apps
    owner, guest = _clients(owner_app, guest_app)
    hold = _Hold(lambda item: item[0] == "progress" and ANALYZING in (item[1].get("stage") or ""))
    gate_a = Gate()
    first, second, rest = _split_analysis()
    async with owner, guest:
        world, task, gate0 = await _begin(guest_app, owner, guest, fake_llm, ["第一章其余"],
                                          extra=([first, gate_a, second, rest], SECOND), analyze=True)
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
                gate0.release()
                # 引擎已核验并产出「分析中」帧（分析 guard 来源），订阅侧尚未发送；运行体阻塞在分析模型。
                await asyncio.wait_for(hold.reached.wait(), 10)
                await gate_a.wait_reached(within=10)
                revoke = asyncio.create_task(_revoke(owner, world, member.revision))
                await asyncio.wait_for(reached.wait(), 10)
                before = len(stream.events())
                hold.proceed.set()
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 10)
                assert len(stream.events()) == before
                release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                events = await stream.finish()
                assert await _running(guest_app, task["id"])
        finally:
            release.set()
            hold.proceed.set()
            gate0.release()
            gate_a.release()
            if stream is not None:
                await stream.aclose()
            await _cancel(revoke)
            await guest_app.state.task_engine.stop()
    assert events[0][0] == "snapshot"
    assert events[-1] == ("error", DENIED), events
    assert not any(hold.predicate(item) for item in events)
    assert SECOND not in stream.text and '"result"' not in stream.text


async def test_postgres_cross_instance_revoke_without_notification_denies_chapter_frame(
    postgres_batch_stream_apps, fake_llm, monkeypatch,
):
    """章内：撤权经另一实例提交，本实例零通知、运行体未停；已产出帧在发送时被锁内复查拒绝。"""
    owner_app, guest_app = postgres_batch_stream_apps
    owner, guest = _clients(owner_app, guest_app)
    marker = "撤权前已产出的批量帧"
    hold = _Hold(lambda item: item[0] == "delta" and marker in item[1].get("text", ""))
    gate1 = Gate()
    notified = []
    async with owner, guest:
        world, task, gate0 = await _begin(guest_app, owner, guest, fake_llm, [marker, gate1, "撤权后不得生成"])
        original_notify = guest_app.state.task_engine.notify_collaboration_change

        def recording_notify(*args, **kwargs):
            notified.append(args)
            return original_notify(*args, **kwargs)

        # 建立协作之后才开始记录：之后的撤权只经另一实例提交。
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
                gate0.release()
                await asyncio.wait_for(hold.reached.wait(), 10)
                await gate1.wait_reached(within=10)
                assert (await _revoke(owner, world, member.revision)).status_code == 204
                assert notified == [] and await _running(guest_app, task["id"])
                hold.proceed.set()
                events = await stream.finish()
                assert await _running(guest_app, task["id"])
        finally:
            hold.proceed.set()
            gate0.release()
            gate1.release()
            if stream is not None:
                await stream.aclose()
            await guest_app.state.task_engine.stop()
    assert events[0][0] == "snapshot"
    assert events[1:] == [("error", DENIED)], events
    assert marker not in stream.text and "撤权后不得生成" not in stream.text


async def test_postgres_two_subscribers_and_batch_runner_send_lease_do_not_block_each_other(
    postgres_batch_stream_apps, fake_llm, monkeypatch,
):
    """只读核对与帧租约同为共享锁，不升锁：运行体在第二章真实发送 lease 内持共享锁停住时，订阅者 A 的帧租约
    （其帧来源已被第二章 attempt 取代，改以最新来源复核）取锁并在实际 send 内停住；订阅者 B 的快照帧租约也能
    取锁发出；放行后运行体的发送 lease 在 A 仍持锁时完成（模型流开始）。三方都不互相等待。"""
    from app.services import chapter_claim

    owner_app, guest_app = postgres_batch_stream_apps
    owner, guest = _clients(owner_app, guest_app)
    first_hold = _Hold(lambda item: item[0] == "chapter_start" and item[1].get("index") == 1)
    entered, release = asyncio.Event(), asyncio.Event()
    send_entered, send_release = asyncio.Event(), asyncio.Event()
    send_pid = {}
    original_sending = chapter_claim.require_sending_chapter
    gate3 = Gate()

    seen: list[int] = []

    async def gated_sending(db, claim):
        await original_sending(db, claim)
        if claim.kind == "batch" and claim.chapter_id not in seen:
            seen.append(claim.chapter_id)
        # 第二章（第二个不同章节）的首次真实发送 lease 内停住。
        if (claim.kind == "batch" and len(seen) == 2 and claim.chapter_id == seen[1]
                and not send_entered.is_set()):
            send_pid["pid"] = await db.scalar(select(func.pg_backend_pid()))
            send_entered.set()
            await send_release.wait()

    monkeypatch.setattr(chapter_claim, "require_sending_chapter", gated_sending)
    async with owner, guest:
        world, task, gate0 = await _begin(guest_app, owner, guest, fake_llm, ["第一章其余"],
                                          extra=([gate3, "第二章正文"],))
        _mount(guest_app, [first_hold])
        stream_a = stream_b = None
        try:
            streams = []
            async with _serve(guest_app, hold_body=CHAPTER_TWO_START, entered=entered, release=release,
                              streams=streams) as base:
                stream_a = _Stream(base, guest, task)
                streams.append(stream_a)
                await stream_a.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                gate0.release()
                # A 已取得第二章开始帧（尚未取帧租约）；运行体已提交第二章 attempt、进入真实发送 lease 并停住。
                await asyncio.wait_for(first_hold.reached.wait(), 10)
                await asyncio.wait_for(send_entered.wait(), 10)
                first_hold.proceed.set()
                # A 的帧租约在运行体持发送 lease 时取得全部共享锁，并在实际 send 内停住。
                await asyncio.wait_for(entered.wait(), 10)
                stream_b = _Stream(base, guest, task)
                streams.append(stream_b)
                await stream_b.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
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
            gate0.release()
            gate3.release()
            for item in (stream_a, stream_b):
                if item is not None:
                    await item.aclose()
            await guest_app.state.task_engine.stop()
    for events in (events_a, events_b):
        assert events[-1][0] == "done", events[-1]
        assert events[-1][1]["task"]["status"] == "succeeded"
    assert "permission_denied" not in stream_a.text + stream_b.text
    assert any(event == "chapter_start" and data["index"] == 1 for event, data in events_a)
    assert any(event == "delta" and "第二章正文" in data["text"] for event, data in events_b)
