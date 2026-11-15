"""E5-e 真实 PostgreSQL：早订阅（排队 / 领取瞬间 / 首个事件前）与撤权、领取事务的加锁顺序，跨实例零通知，
以及两个订阅者与引擎领取并发时不死锁。

真实引擎（只启动订阅所在实例）经真实中央校验器与 TCP 发送，不替换证明；用 pg_blocking_pids 证明后到的一方
确实在数据库层等待。撤权由另一实例提交，本实例收不到进程内通知，只能靠领取锁内复核或发送时锁内复查。
"""

import asyncio

from sqlalchemy import func, select, text

from app.models import Task
from app.services import task_statistics_service
from app.services.task_engine import engine as engine_module
from app.services.task_engine import stream_evidence
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_paused_task_postgresql import _gate_revoke
from tests.test_collaboration_rewrite_identity_postgresql import _assert_no_idle_output_transaction
from tests.test_collaboration_stream_early_subscribe_e5e import (
    ANSWERS,
    _gate_build,
    _member,
    _QuotaBlock,
    _source_is,
    _start,
    _world,
)
from tests.test_collaboration_stream_replay_e5b import DENIED
from tests.test_collaboration_stream_running_e5c1 import _Hold, _mount, _Stream
from tests.test_collaboration_stream_running_e5c1_postgresql import _cancel, _clients, _revoke, _running, _serve

postgres_early_apps = _postgres_fixture


async def _row(app, task_id):
    async with app.state.sessionmaker() as db:
        return await db.get(Task, task_id)


async def _settled(app, task_id, within=20.0):
    async with asyncio.timeout(within):
        while True:
            row = await _row(app, task_id)
            if row.status not in ("queued", "running") and task_id not in app.state.task_engine._running:
                return row
            await asyncio.sleep(0.02)


def _gate_claim_transaction(monkeypatch, target):
    """领取事务内（running CAS 已执行、提交前，仍持门禁/User/作品/Task 锁）停住，并记录该连接。"""
    reached, release, holder = asyncio.Event(), asyncio.Event(), {}
    original = task_statistics_service.record_state_change

    async def gated(db, task, status, **kwargs):
        await original(db, task, status, **kwargs)
        if status == "running" and task.id == target["id"] and not reached.is_set():
            holder["pid"] = await db.scalar(select(func.pg_backend_pid()))
            reached.set()
            await release.wait()

    monkeypatch.setattr(task_statistics_service, "record_state_change", gated)
    return reached, release, holder


async def _blocked_by(maker, holder_pid):
    async with maker() as db:
        return list(await db.scalars(text(
            "SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND :holder=ANY(pg_blocking_pids(pid))"
        ), {"holder": holder_pid}))


async def test_postgres_claim_transaction_first_blocks_revoke_then_claim_frame_denied(
    postgres_early_apps, fake_llm, monkeypatch,
):
    owner_app, guest_app = postgres_early_apps
    owner, guest = _clients(owner_app, guest_app)
    guest_app.state.settings.allow_private_ai_endpoints = True
    engine = guest_app.state.task_engine
    hold = _Hold(lambda item: item[0] == "progress" and _source_is(stream_evidence.ClaimSource)(item))
    async with owner, guest:
        world = await _world(owner, guest)
        member = await _member(guest_app, world)
        block = _QuotaBlock(monkeypatch, engine)
        build_reached, build_release = _gate_build(monkeypatch, "chapter")
        _mount(guest_app, [hold])
        await engine.start()
        target = {}
        claim_reached, claim_release, holder = _gate_claim_transaction(monkeypatch, target)
        # 撤权取得锁后、提交前也停住：确定领取帧在撤权提交前已由引擎产出。
        revoke_holder, revoke_reached, revoke_release = {}, asyncio.Event(), asyncio.Event()
        _gate_revoke(monkeypatch, revoke_holder, revoke_reached, revoke_release)
        task = await _start(fake_llm, guest, world, "chapter")
        target["id"] = task["id"]
        stream, revoke = None, None
        try:
            streams = []
            async with _serve(guest_app, streams=streams) as base:
                stream = _Stream(base, guest, task)
                streams.append(stream)
                await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                assert stream.events()[0][1]["task"]["status"] == "queued"
                block.release()
                # 领取事务已执行 running CAS、尚未提交，仍持门禁/User/作品/Task 锁。
                await asyncio.wait_for(claim_reached.wait(), 10)
                revoke = asyncio.create_task(_revoke(owner, world, member.revision))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 10)
                assert not revoke.done() and not revoke_reached.is_set()
                claim_release.set()
                # 领取先提交：撤权随后才取得锁；引擎登记领取来源并产出领取帧（被订阅侧持住）。
                await asyncio.wait_for(revoke_reached.wait(), 10)
                await asyncio.wait_for(hold.reached.wait(), 10)
                assert (await _row(guest_app, task["id"])).status == "running"
                revoke_release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                hold.proceed.set()
                events = await stream.finish()
                assert await _running(guest_app, task["id"])
        finally:
            claim_release.set()
            revoke_release.set()
            hold.proceed.set()
            build_release.set()
            block.release()
            if stream is not None:
                await stream.aclose()
            await _cancel(revoke)
            await engine.stop()
    assert events[0][0] == "snapshot" and events[1:] == [("error", DENIED)], events
    assert ANSWERS["chapter"] not in stream.text


async def test_postgres_revoke_first_blocks_claim_and_queued_task_fails_closed(
    postgres_early_apps, fake_llm, monkeypatch,
):
    owner_app, guest_app = postgres_early_apps
    owner, guest = _clients(owner_app, guest_app)
    guest_app.state.settings.allow_private_ai_endpoints = True
    engine = guest_app.state.task_engine
    issued = []
    original_issue = stream_evidence.issue_claim

    def recording_issue(execution, *, hub):
        issued.append(execution.task_id)
        return original_issue(execution, hub=hub)

    monkeypatch.setattr(stream_evidence, "issue_claim", recording_issue)
    async with owner, guest:
        world = await _world(owner, guest)
        member = await _member(guest_app, world)
        block = _QuotaBlock(monkeypatch, engine)
        _mount(guest_app)
        await engine.start()
        holder, reached, release = {}, asyncio.Event(), asyncio.Event()
        _gate_revoke(monkeypatch, holder, reached, release)
        task = await _start(fake_llm, guest, world, "chapter")
        stream, revoke = None, None
        try:
            streams = []
            async with _serve(guest_app, streams=streams) as base:
                stream = _Stream(base, guest, task)
                streams.append(stream)
                await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                revoke = asyncio.create_task(_revoke(owner, world, member.revision))
                # 撤权已在共同锁序内持锁、尚未提交。
                await asyncio.wait_for(reached.wait(), 10)
                block.release()
                # 领取事务在数据库层等待撤权。
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 10)
                assert (await _row(guest_app, task["id"])).status == "queued"
                release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                events = await stream.finish()
        finally:
            release.set()
            block.release()
            if stream is not None:
                await stream.aclose()
            await _cancel(revoke)
            row = await _settled(guest_app, task["id"])
            await engine.stop()
    # 领取在锁内按已提交的撤权复核：受限收尾为失败，从未进入 running、从未登记领取来源，模型零调用。
    assert row.status == "failed" and row.error == engine_module.COLLABORATION_FAILURE_MESSAGES["collaboration_revoked"]
    assert task["id"] not in issued and fake_llm.calls == []
    assert events[0][0] == "snapshot" and events[1:] == [("error", DENIED)], events


async def test_postgres_cross_instance_revoke_without_notification_denies_claim_snapshot(
    postgres_early_apps, fake_llm, monkeypatch,
):
    owner_app, guest_app = postgres_early_apps
    owner, guest = _clients(owner_app, guest_app)
    guest_app.state.settings.allow_private_ai_endpoints = True
    engine = guest_app.state.task_engine
    hold = _Hold(lambda item: item[0] == "snapshot" and _source_is(stream_evidence.ClaimSource)(item))
    notified = []
    original_notify = engine.notify_collaboration_change

    def recording_notify(*args, **kwargs):
        notified.append(args)
        return original_notify(*args, **kwargs)

    async with owner, guest:
        world = await _world(owner, guest)
        member = await _member(guest_app, world)
        monkeypatch.setattr(engine, "notify_collaboration_change", recording_notify)
        build_reached, build_release = _gate_build(monkeypatch, "chapter")
        _mount(guest_app, [hold])
        await engine.start()
        task = await _start(fake_llm, guest, world, "chapter")
        stream = None
        try:
            await asyncio.wait_for(build_reached.wait(), 10)
            assert type(engine.hubs[task["id"]].source) is stream_evidence.ClaimSource
            streams = []
            async with _serve(guest_app, streams=streams) as base:
                stream = _Stream(base, guest, task)
                streams.append(stream)
                await asyncio.wait_for(hold.reached.wait(), 10)
                # 撤权经另一实例提交：本实例引擎收不到进程内通知，运行体也未被停止。
                assert (await _revoke(owner, world, member.revision)).status_code == 204
                assert notified == [] and await _running(guest_app, task["id"])
                hold.proceed.set()
                events = await stream.finish()
                assert await _running(guest_app, task["id"])
        finally:
            hold.proceed.set()
            build_release.set()
            if stream is not None:
                await stream.aclose()
            await engine.stop()
    assert events == [("error", DENIED)], events
    assert ANSWERS["chapter"] not in stream.text


async def _subscriber_b_state(app, stream, claim_pids):
    while True:
        if stream.events():
            return "sent"
        for claim_pid in claim_pids:
            if await _blocked_by(app.state.sessionmaker, claim_pid):
                return "blocked"
        await asyncio.sleep(0.01)


async def test_postgres_two_subscribers_and_engine_claim_do_not_deadlock(postgres_early_apps, fake_llm, monkeypatch):
    """订阅者 A 的排队快照在实际 send 内持帧租约共享锁时，引擎领取事务在数据库层等待 A；订阅者 B 随后订阅，
    其帧租约排在领取之后（或直接取得共享锁）。放行 A 后领取提交，B 的排队快照按「排队快照恰逢领取」证明，
    三方最终都完成：两条流都到 done，没有 permission_denied，也没有遗留空闲事务。"""
    owner_app, guest_app = postgres_early_apps
    owner, guest = _clients(owner_app, guest_app)
    guest_app.state.settings.allow_private_ai_endpoints = True
    engine = guest_app.state.task_engine
    entered, release = asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    original_verify = stream_evidence.verify_stream_frame

    async def recording(db, task, project, event, data, evidence):
        result = await original_verify(db, task, project, event, data, evidence)
        if result and event == "snapshot" and not pid.done():
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))
        return result

    # 只记录持锁连接，仍调用真实中央校验器。
    monkeypatch.setattr(stream_evidence, "verify_stream_frame", recording)
    async with owner, guest:
        world = await _world(owner, guest)
        block = _QuotaBlock(monkeypatch, engine)
        _mount(guest_app)
        await engine.start()
        task = await _start(fake_llm, guest, world, "chapter")
        stream_a = stream_b = None
        try:
            streams = []
            async with _serve(guest_app, hold_body='"status": "queued"', entered=entered, release=release,
                              streams=streams) as base:
                stream_a = _Stream(base, guest, task)
                streams.append(stream_a)
                # A 的排队快照已通过证明，正在实际 send 内持帧租约全部共享锁。
                await asyncio.wait_for(entered.wait(), 10)
                a_pid = await asyncio.wait_for(pid, 5)
                block.release()
                # 领取事务在数据库层等待 A。
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, a_pid), 10)
                claim_pids = await _blocked_by(owner_app.state.sessionmaker, a_pid)
                assert claim_pids and (await _row(guest_app, task["id"])).status == "queued"
                stream_b = _Stream(base, guest, task)
                streams.append(stream_b)
                # B 读到的仍是排队行（领取未提交）；其帧租约在数据库层排在领取之后。
                # 实测 PostgreSQL 对已仅被共享锁定的行直接授予 B 的共享锁（B 不排在领取之后）；两种结果都不得死锁。
                outcome = await asyncio.wait_for(_subscriber_b_state(owner_app, stream_b, claim_pids), 10)
                assert outcome in ("sent", "blocked"), outcome
                assert (await _row(guest_app, task["id"])).status == "queued"
                release.set()
                events_a = await stream_a.finish(within=30)
                events_b = await stream_b.finish(within=30)
                for claim_pid in claim_pids:
                    await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, claim_pid)
                await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, a_pid)
        finally:
            release.set()
            block.release()
            for item in (stream_a, stream_b):
                if item is not None:
                    await item.aclose()
            await engine.stop()
    for events, stream in ((events_a, stream_a), (events_b, stream_b)):
        names = [event for event, _ in events]
        assert names[0] == "snapshot" and names[-1] == "done", events
        assert events[0][1]["task"]["status"] == "queued"
        assert "permission_denied" not in stream.text
        assert "".join(data["text"] for event, data in events if event == "delta") == ANSWERS["chapter"]
    assert (await _row(guest_app, task["id"])).status == "succeeded"
