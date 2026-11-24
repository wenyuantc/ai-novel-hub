"""在 B 提交真实业务，测量 A 的领取、取消、撤权和设置发布。"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.conftest import _new_client
from tests.helpers import Gate, task_params, wait_task, wait_until
from tests.schema_helpers import isolated_worker_database
from tests.test_c4_transport import listeners
from tests.test_collaboration_task_revocation_e4 import (
    MESSAGES,
    _change,
    _start_outline,
    _wait_status,
    _world,
)
from tests.test_collaboration_task_revocation_e4 import blocking_outline as blocking_outline
from tests.test_system_settings import save_settings
from tests.test_tasks import _generate, _setup

POLL = 0.8


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


@pytest.fixture
def committed_at(second_app):
    """用 B 的真实根事务提交时间计时，忽略其他请求和后台心跳。"""
    times = WeakKeyDictionary()
    engine = second_app.state.engine.sync_engine

    def mark(session):
        if session.get_bind() is engine and not session.in_nested_transaction():
            times[asyncio.current_task()] = time.monotonic()

    event.listen(Session, "after_commit", mark)
    try:
        yield lambda: times.pop(asyncio.current_task())
    finally:
        event.remove(Session, "after_commit", mark)


def observe(monkeypatch, obj, name, *, clock=time.monotonic):
    """记录同步回调实际执行的时间，保持原副作用。"""
    times = []
    original = getattr(obj, name)

    def record(*args, **kwargs):
        times.append(clock())
        return original(*args, **kwargs)

    monkeypatch.setattr(obj, name, record)
    return times


def assert_latency(operation, origin, reaction, online, *, starts, interval, detected=None, hard_wall=False):
    elapsed = reaction - origin
    logger = logging.getLogger(__name__)
    if online:
        logger.info("%s 在线提交至反应 %.3f 秒", operation, elapsed)
        assert 0 <= elapsed <= 1.0, (operation, elapsed)
        return
    # B 在完成一轮扫描后才写。按实际扫描起点计数：若漏过第一轮，物理信号前会有两个起点。
    intervening = [stamp for stamp in starts if origin <= stamp <= reaction]
    assert len(intervening) == 1, (operation, origin, intervening, reaction)
    tick, = intervening
    if detected is None:
        detected = reaction
    logger.info("%s 离线周期 %.3fs，提交至第 %d 轮扫描 %.3fs，扫描至发现 %.3fs，"
                "发现至实际反应 %.3fs，提交至实际反应 %.3fs", operation, interval,
                starts.index(tick) + 1, tick - origin, detected - tick, reaction - detected, elapsed)
    assert tick - origin <= interval, (operation, "扫描迟到", tick - origin)
    assert tick <= detected <= reaction
    if hard_wall:
        assert elapsed <= interval, (operation, elapsed)


@asynccontextmanager
async def pair(app, second_app, monkeypatch, online, *, production=False):
    a = app.state.task_engine
    if not production:
        monkeypatch.setattr(a, "poll_interval", 60.0 if online else POLL)
    starts = []
    scanned = asyncio.Event()
    periodic = False
    reap, dispatch = a._reap_orphans, a._dispatch

    async def on_reap():
        nonlocal periodic
        if asyncio.current_task() is a._loop_task:
            periodic = True
            starts.append(time.monotonic())
        await reap()

    async def on_dispatch():
        nonlocal periodic
        await dispatch()
        if online or periodic:
            periodic = False
            scanned.set()

    monkeypatch.setattr(a, "_reap_orphans", on_reap)
    monkeypatch.setattr(a, "_dispatch", on_dispatch)

    async def after_scan():
        scanned.clear()
        if online:
            a.notify()
        await asyncio.wait_for(scanned.wait(), 3 if online else a.poll_interval + 3)

    async with listeners(app, second_app) as (a, b):
        if not online:
            await b.peer.stop()
        assert not b.running
        yield a, b, after_scan, starts


@pytest.mark.parametrize("admin,online,production", [
    pytest.param(False, True, False, id="owner-connected"),
    pytest.param(False, False, False, id="owner-poll"),
    pytest.param(True, True, False, id="admin-connected"),
    pytest.param(True, False, False, id="admin-poll"),
    pytest.param(True, False, True, id="admin-production-poll"),
])
async def test_create_and_cancel(
    app, second_app, fake_llm, monkeypatch, system_admin, committed_at, online, admin, production,
):
    async with _new_client(second_app) as owner, _new_client(second_app) as auditor:
        auditor.cookies.update(system_admin.cookies)
        _, _, (chapter,) = await _setup(owner)
        async with pair(app, second_app, monkeypatch, online, production=production) as (a, b, after_scan, starts):
            committed = observe(monkeypatch, b.peer_wakes, "schedule", clock=committed_at)
            signalled = observe(monkeypatch, a, "signal_running_cancel")
            admin_intent = observe(monkeypatch, a.admin_cancellations, "request")
            claimed = []
            run = a._run

            async def on_run(*args, **kwargs):
                claimed.append(time.monotonic())
                return await run(*args, **kwargs)

            monkeypatch.setattr(a, "_run", on_run)
            gate = Gate()
            fake_llm.push(["第一段", gate, "取消后的文字"])
            await after_scan()
            created = await _generate(owner, chapter)
            assert created.status_code == 202, created.text
            ref = created.json()
            try:
                budget = 5 if online else a.poll_interval + 5
                await gate.wait_reached(within=budget)
                assert len(claimed) == len(committed) == 1
                assert_latency("领取", committed[0], claimed[0], online, starts=starts, interval=a.poll_interval)
                await after_scan()
                if admin:
                    result = await auditor.post(f"/api/v1/admin/tasks/{ref['task_key']}/cancel",
                                                json={"confirm": True, "reason": "取消运行"})
                    assert result.status_code == 202, result.text
                else:
                    result = await owner.post(f"/api/v1/tasks/{ref['id']}/cancel", params=task_params(ref))
                    assert result.status_code == 200, result.text
                assert (await wait_task(owner, ref, within=budget))["status"] == "cancelled"
                assert len(committed) == 2 and signalled
                discovered = admin_intent[0] if admin else None
                assert_latency("管理员取消" if admin else "本人取消", committed[1], signalled[0], online,
                               detected=discovered, starts=starts, interval=a.poll_interval)
            finally:
                gate.release()


@pytest.mark.parametrize("online", [True, False], ids=["connected", "poll"])
async def test_member_change_rechecks_silent_execution(
    app, second_app, make_client, blocking_outline, monkeypatch, committed_at, online,
):
    async with _new_client(second_app) as owner:
        project, _, actor = await _world(owner, make_client(), chapters=0)
        pid, uid = project["id"], actor["id"]
        async with pair(app, second_app, monkeypatch, online) as (a, b, after_scan, starts):
            committed = observe(monkeypatch, b.peer_wakes, "collaboration", clock=committed_at)
            signalled = observe(monkeypatch, a, "signal_running_cancel")
            task_id = await _start_outline(app, pid, uid)
            try:
                await asyncio.wait_for(blocking_outline.reached.wait(), 3)
                await after_scan()
                with a.peer_wakes.watch_member(pid, uid) as watcher:
                    await _change(app, owner, pid, actor, "viewer")
                    await wait_until(lambda: bool(blocking_outline.cancelled), within=2)
                    assert_latency("撤权停止静默执行", committed[0], signalled[0], online,
                                   starts=starts, interval=a.poll_interval)
                    assert watcher.is_set() == online
                row = await _wait_status(app, task_id, ["failed"])
                assert row.error == MESSAGES["collaboration_revoked"] and row.output_text == ""
            finally:
                blocking_outline.release.set()


@pytest.mark.parametrize("online", [True, False], ids=["connected", "poll"])
async def test_settings_refresh_while_scheduler_stopped(
    app, second_app, system_admin, monkeypatch, committed_at, online,
):
    async with _new_client(second_app) as remote:
        remote.cookies.update(system_admin.cookies)
        async with pair(app, second_app, monkeypatch, online) as (a, _, _, _):
            await a.stop()
            manager = app.state.system_settings
            resends = observe(monkeypatch, manager, "notify_peers")
            # 断线样例保留生产设置轮询周期，并对真实版本发布执行硬墙钟断言。
            if online:
                monkeypatch.setattr(manager, "_poll_interval", 60.0)
            starts = []
            reloaded = asyncio.Event()
            reload = manager._reload

            async def after_reload():
                starts.append(time.monotonic())
                result = await reload()
                reloaded.set()
                return result

            monkeypatch.setattr(manager, "_reload", after_reload)
            manager.request_refresh()
            await asyncio.wait_for(reloaded.wait(), 3)
            committed = observe(monkeypatch, second_app.state.system_settings, "notify_peers", clock=committed_at)
            published = observe(monkeypatch, manager, "_publish")
            saved = await save_settings(remote, task_max_concurrency=3)
            budget = 2 if online else manager._poll_interval + 3
            await wait_until(lambda: manager._revision == saved["revision"], within=budget)
            assert app.state.settings.task_max_concurrency == 3
            assert_latency("设置发布", committed[0], published[0], online, starts=starts,
                           interval=manager._poll_interval, hard_wall=True)
            # 重复提示只读主库，不产生新版本，也不向源端回传。
            reloaded.clear()
            await a.peer_wakes.receive({"schedule": False, "settings": True, "members": [], "all_members": False})
            await asyncio.wait_for(reloaded.wait(), 3)
            assert len(published) == 1 and not resends


async def test_member_hints_coalesce_and_reach_http_only_subscribers(app, second_app, monkeypatch):
    async with listeners(app, second_app) as (a, b):
        await a.stop()
        delivered = []
        bounced = []
        original = b.peer.wake

        async def deliver(target, payload):
            delivered.append(payload)
            await original(target, payload)

        async def bounce(target, payload):
            bounced.append(payload)

        monkeypatch.setattr(b.peer, "wake", deliver)
        monkeypatch.setattr(a.peer, "wake", bounce)
        with a.peer_wakes.watch_member(1, 1) as one, a.peer_wakes.watch_member(2, 1) as two:
            for _ in range(100):
                b.peer_wakes.collaboration(1, 1)
            await b.peer_wakes._sender
            assert one.is_set() and not two.is_set() and len(delivered) == 1
            one.clear()
            for actor in range(1, 200):
                b.peer_wakes.collaboration(3, actor)
            assert len(b.peer_wakes._pending) == 1
            await b.peer_wakes._sender
            if a.peer_wakes._sender is not None:
                await a.peer_wakes._sender
            assert one.is_set() and two.is_set() and len(delivered) == 2
            assert delivered[-1]["all_members"] and not bounced
        assert not a.peer_wakes._members
