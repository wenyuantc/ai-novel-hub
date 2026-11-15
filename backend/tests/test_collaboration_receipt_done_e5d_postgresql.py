"""E5-d 真实 PostgreSQL：收据收尾的带快照 done 帧，以及该帧发送（帧租约共享锁）与撤权（写锁）的两种先后顺序。

真实引擎只在订阅所在实例（guest_app）启动；撤权经另一实例提交，本实例收不到进程内通知，只能靠发送时锁内复查。
用 pg_blocking_pids 证明后到的一方确实在数据库层等待；订阅者只会得到与提交顺序一致的一种结果（done 或失权）。
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.services import batch_service
from app.services.task_engine import stream_evidence
from tests.helpers import wait_until
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_paused_task_postgresql import _gate_revoke
from tests.test_collaboration_receipt_done_e5d import (
    KINDS,
    _arm_before_done,
    _assert_unpublished_done,
    _bump_visibility,
    _launch,
    _member_change,
    _Recorder,
)
from tests.test_collaboration_rewrite_identity_postgresql import _assert_no_idle_output_transaction
from tests.test_collaboration_stream_replay_e5b import DENIED
from tests.test_collaboration_stream_running_e5c1 import _Hold, _mount, _settled, _Stream
from tests.test_collaboration_stream_running_e5c1_postgresql import _clients, _serve

postgres_receipt_apps = _postgres_fixture


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))


async def _live(owner_app, guest_app, owner, guest, fake_llm, monkeypatch, kind, *, holds=None,
                serve_kwargs=None, during=None):
    """编辑者启动任务；done 核验前（订阅者已收齐）作者经另一实例递增可见性，使其按收据收尾。

    `during(stream)` 在放行运行体之后、等待流结束之前执行（用于在终态帧发送前后插入撤权）。
    """
    for app in (owner_app, guest_app):
        app.state.settings.allow_private_ai_endpoints = True
    recorder = _Recorder(monkeypatch)
    _mount(guest_app, holds)
    box: dict = {}

    async def wait():
        await wait_until(lambda: recorder.caught_up(box["task"]["id"], box["stream"].events()), within=15)

    async def change():
        await _bump_visibility(owner_app, box["world"].project["id"])

    fired = _arm_before_done(guest_app, monkeypatch, change, wait)
    await guest_app.state.task_engine.start()
    world, task, gate, end, _ = await _launch(kind, owner, guest, fake_llm, "editor")
    box["world"], box["task"] = world, task
    await gate.wait_reached(within=10)
    engine = guest_app.state.task_engine
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text, within=10)
    stream = None
    try:
        streams: list = []
        async with _serve(guest_app, streams=streams, **(serve_kwargs or {})) as base:
            stream = _Stream(base, guest, task)
            streams.append(stream)
            box["stream"] = stream
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            await end.wait_reached(within=10)
            await stream.wait_for(lambda events: recorder.caught_up(task["id"], events), within=15)
            end.release()
            if during is not None:
                await during(world, stream)
            events = await stream.finish(within=20)
    finally:
        gate.release()
        end.release()
        for hold in holds or ():
            hold.proceed.set()
        if stream is not None:
            await stream.aclose()
        await guest_app.state.task_engine.stop()
    assert fired == ["done"]
    return world, task, stream, events


@pytest.mark.parametrize("kind", KINDS)
async def test_postgres_receipt_done_live_and_replay(postgres_receipt_apps, fake_llm, monkeypatch, kind):
    owner_app, guest_app = postgres_receipt_apps
    owner, guest = _clients(owner_app, guest_app)
    async with owner, guest:
        _, task, stream, events = await _live(owner_app, guest_app, owner, guest, fake_llm, monkeypatch, kind)
        row = await _settled(guest_app, task["id"])
        assert row.status == "succeeded" and row.execution_key is None and row.result is not None
        assert stream.status == 200 and "permission_denied" not in stream.text, stream.text
        _assert_unpublished_done(kind, events[-1], task)
        # 回放（重新订阅已结束任务）与实时同一帧。
        streams: list = []
        async with _serve(guest_app, streams=streams) as base:
            replay = _Stream(base, guest, task)
            streams.append(replay)
            replayed = await replay.finish(within=15)
    assert [event for event, _ in replayed] == ["snapshot", "done"], replayed
    assert replayed[-1] == events[-1]


@pytest.mark.parametrize("kind", ["chapter", "wizard"])
async def test_postgres_receipt_done_lease_first_blocks_revoke_then_done_is_sent(
    postgres_receipt_apps, fake_llm, monkeypatch, kind,
):
    """done 帧已在实际 send 内持帧租约共享锁：撤权在数据库层等待；帧照常发出，撤权随后提交。"""
    owner_app, guest_app = postgres_receipt_apps
    owner, guest = _clients(owner_app, guest_app)
    entered, release = asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    original = stream_evidence.verify_stream_frame

    async def recording(db, task, project, event, data, evidence):
        result = await original(db, task, project, event, data, evidence)
        if result and event == "done" and not pid.done():
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))
        return result

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", recording)
    revoke: list[asyncio.Task] = []

    async def during(world, stream):
        await asyncio.wait_for(entered.wait(), 15)
        backend_pid = await asyncio.wait_for(pid, 5)
        revoke.append(asyncio.create_task(_member_change(owner_app, owner, world, "revoked")))
        await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, backend_pid), 10)
        assert not revoke[0].done()
        release.set()
        await asyncio.wait_for(revoke[0], 10)
        await stream.closed.wait()
        await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, backend_pid)

    async with owner, guest:
        try:
            _, task, stream, events = await _live(
                owner_app, guest_app, owner, guest, fake_llm, monkeypatch, kind, during=during,
                serve_kwargs={"hold_body": '"unpublished"', "entered": entered, "release": release},
            )
        finally:
            release.set()
            for item in revoke:
                if not item.done():
                    item.cancel()
            await asyncio.gather(*revoke, return_exceptions=True)
    _assert_unpublished_done(kind, events[-1], task)
    assert "permission_denied" not in stream.text


@pytest.mark.parametrize("kind", ["chapter", "wizard"])
async def test_postgres_revoke_first_then_held_receipt_done_is_denied(
    postgres_receipt_apps, fake_llm, monkeypatch, kind,
):
    """done 帧已由引擎产出、尚未取帧租约；撤权（另一实例）先持锁未提交：帧租约等待，按已提交撤权复查后拒绝。"""
    owner_app, guest_app = postgres_receipt_apps
    owner, guest = _clients(owner_app, guest_app)
    hold = _Hold(lambda item: item[0] == "done")
    holder, reached, release = {}, asyncio.Event(), asyncio.Event()
    revoke: list[asyncio.Task] = []

    async def during(world, stream):
        _gate_revoke(monkeypatch, holder, reached, release)
        await asyncio.wait_for(hold.reached.wait(), 15)
        revoke.append(asyncio.create_task(_member_change(owner_app, owner, world, "revoked")))
        await asyncio.wait_for(reached.wait(), 10)
        before = len(stream.events())
        hold.proceed.set()
        await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 10)
        assert len(stream.events()) == before
        release.set()
        await asyncio.wait_for(revoke[0], 10)

    async with owner, guest:
        try:
            _, task, stream, events = await _live(
                owner_app, guest_app, owner, guest, fake_llm, monkeypatch, kind, holds=[hold], during=during,
            )
        finally:
            release.set()
            hold.proceed.set()
            for item in revoke:
                if not item.done():
                    item.cancel()
            await asyncio.gather(*revoke, return_exceptions=True)
        row = await _settled(guest_app, task["id"])
    assert row.status == "succeeded" and row.execution_key is None
    assert hold.item is not None and hold.item[0] == "done"
    assert events[-1] == ("error", DENIED), events
    assert not any(event == "done" for event, _ in events) and '"unpublished"' not in stream.text
