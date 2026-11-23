"""两个应用进程的引擎共享数据库时的领取、接管和恢复契约。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.errors import ConflictError
from app.models import AgentChange, Chapter, GenerationLog, Project, Task, TaskExecutionSlot, TaskStatistic, TaskWorker
from app.services import task_service, task_statistics_service
from app.services.task_engine import workers
from app.services.task_engine.execution_identity import require_write_execution
from app.services.task_engine.registry import get_handler
from tests.helpers import Gate, register, wait_task, wait_until
from tests.mcp_fakes import FakeMcpConnector
from tests.test_agent_mcp_skills import _stuck_external
from tests.test_tasks import _generate, _setup
from tests.worker_helpers import age_heartbeat, expire_worker, fence_worker, freeze_heartbeat, worker_row


async def _chapter_state(app, task_id, chapter_id):
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        chapter = await db.get(Chapter, chapter_id)
        logs = list(await db.scalars(select(GenerationLog.id).where(GenerationLog.task_id == task_id)))
        return (
            row.status, row.execution_key, row.output_text, row.finished_at,
            chapter.status, chapter.content, chapter.generation_claim_key, tuple(logs),
        )


async def test_worker_register_heartbeat_stop_and_restart(second_app, monkeypatch):
    engine = second_app.state.task_engine
    engine.heartbeat_interval = 0.05
    await engine.start()
    original = await worker_row(engine)
    assert original.worker_key and original.fenced_at is None and original.stopped_at is None
    async with engine.ctx.sessionmaker() as db:
        diagnostic = await db.get(TaskWorker, original.worker_key)
        assert diagnostic.hostname and diagnostic.pid > 0
    pair = engine.lease.sqlite_pair
    before = await age_heartbeat(engine, 3)
    renewed = asyncio.Event()
    heartbeat = engine.lease.heartbeat

    async def observe_renewal():
        ok = await heartbeat()
        if ok:
            renewed.set()
        return ok

    monkeypatch.setattr(engine.lease, "heartbeat", observe_renewal)
    await asyncio.wait_for(renewed.wait(), 6)
    assert (await worker_row(engine)).heartbeat_at > before
    await engine.stop()
    assert (await worker_row(engine, original.worker_key, previous_pair=pair)).stopped_at is not None
    await engine.start()
    assert engine.lease.worker_key not in {None, original.worker_key}
    assert (await worker_row(engine)).stopped_at is None


async def test_healthy_generation_survives_second_start(app, second_app, client, fake_llm, monkeypatch):
    """AC1：让 A 的真实模型流停在中间，B 完成启动及恢复后 A 继续保存。"""
    _, _, (chapter,) = await _setup(client, content="原稿")
    gate = Gate()
    fake_llm.push(["开头", gate, "结尾"])
    response = await _generate(client, chapter)
    assert response.status_code == 202, response.text
    ref = response.json()
    handler = get_handler("chapter.generate")
    aborts = []
    on_abort = handler.on_abort

    async def count_abort(*args, **kwargs):
        aborts.append(ref["id"])
        return await on_abort(*args, **kwargs)

    # 注册表持有单例。撤销补丁时必须删掉实例属性，恢复类方法查找。
    monkeypatch.setitem(vars(handler), "on_abort", count_abort)
    try:
        await gate.wait_reached()
        owner = app.state.task_engine.lease.worker_key
        async with app.state.sessionmaker() as db:
            running = await db.get(Task, ref["id"])
            assert (running.status, running.worker_key) == ("running", owner)
            assert running.execution_key
        peer = second_app.state.task_engine
        await peer.start()
        await peer.recover()
        async with second_app.state.sessionmaker() as db:
            row = await db.get(Task, ref["id"])
            assert (row.status, row.worker_key, row.execution_key, row.recovered_at) == (
                "running", owner, running.execution_key, None,
            )
    finally:
        gate.release()
    assert (await wait_task(client, ref))["status"] == "succeeded"
    async with second_app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter["id"])).content == "开头结尾"
    assert aborts == []


async def test_periodic_recovery_releases_chapter_and_rejects_late_worker(
    app, second_app, client, fake_llm, monkeypatch,
):
    """AC2：一次兜底调度周期接管。仍挂在模型流里的旧执行体在接管提交后才恢复。"""
    _, _, (chapter,) = await _setup(client, content="原稿")
    source = app.state.task_engine
    gate = Gate()
    fake_llm.push(["尚未保存", gate, "过期执行体的尾段"])
    response = await _generate(client, chapter)
    assert response.status_code == 202, response.text
    ref = response.json()
    try:
        await gate.wait_reached()
        peer = second_app.state.task_engine
        peer.poll_interval = 0.1
        await peer.start()
        await freeze_heartbeat(source)
        # 保证旧进程仍持有运行体，将本轮恢复交给 B。最后照常释放模型，真实执行迟到写入。
        async def idle_reaper():
            pass

        monkeypatch.setattr(source, "_reap_orphans", idle_reaper)
        tick, ready, processed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        reap = peer._reap_orphans

        async def count_cycle():
            tick.set()
            await ready.wait()
            await reap()
            processed.set()

        monkeypatch.setattr(peer, "_reap_orphans", count_cycle)
        try:
            await asyncio.wait_for(tick.wait(), 6)
            await expire_worker(source)
        finally:
            ready.set()
        await asyncio.wait_for(processed.wait(), 6)
        before = await _chapter_state(app, ref["id"], chapter["id"])
        assert before[:2] == ("interrupted", None)
        assert before[4:7] == ("draft", "原稿", None)
        assert (await worker_row(source)).fenced_at is not None
    finally:
        gate.release()
    await wait_until(lambda: ref["id"] not in source._running)
    assert await _chapter_state(app, ref["id"], chapter["id"]) == before


async def test_two_reapers_interrupt_and_count_once(app, second_app, client, monkeypatch):
    """AC3：两个独立连接池同时发现一条升级前的 running，只有一份清理和统计。"""
    user = await register(client)
    first, second = app.state.task_engine, second_app.state.task_engine
    await first.stop()
    await first.lease.register()
    await second.lease.register()
    async with app.state.sessionmaker() as db:
        row = Task(user_id=user["id"], kind="outline.continue", status="running", execution_key=str(uuid4()))
        db.add(row)
        await task_statistics_service.record_created(db, row)
        await db.commit()
        key, task_id = row.statistics_key, row.id

    handler = get_handler("outline.continue")
    abort, record = handler.on_abort, task_statistics_service.record_state_change
    aborts, transitions = [], []

    async def counted_abort(ctx, row, **kwargs):
        aborts.append((row.id, ctx.task_engine.lease.worker_key))
        return await abort(ctx, row, **kwargs)

    async def counted_transition(db, row, status, **kwargs):
        if row.id == task_id and status == "interrupted":
            transitions.append(status)
        return await record(db, row, status, **kwargs)

    # 两个读事务都已提交候选快照，再同时发起认领 CAS，避免串行运行也被当作并发验收。
    rendezvous = asyncio.Barrier(2)
    adopt = workers.adopt
    contenders = set()

    async def simultaneous_adopt(db, task_id, statistics_key, execution_key, old_worker_key, me):
        contenders.add(me)
        await asyncio.wait_for(rendezvous.wait(), 5)
        return await adopt(db, task_id, statistics_key, execution_key, old_worker_key, me)

    monkeypatch.setattr(workers, "adopt", simultaneous_adopt)
    monkeypatch.setitem(vars(handler), "on_abort", counted_abort)
    monkeypatch.setattr(task_statistics_service, "record_state_change", counted_transition)
    await asyncio.gather(first.recover(), second.recover())
    await asyncio.gather(first.recover(), second.recover())
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        stat = await db.get(TaskStatistic, key)
        assert row.status == stat.status == "interrupted"
        assert row.finished_at and stat.finished_event_at and row.execution_key is None
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(TaskStatistic.key == key)) == 1
    assert len(aborts) == 1 and aborts[0] in [(task_id, first.lease.worker_key), (task_id, second.lease.worker_key)]
    assert transitions == ["interrupted"]
    assert contenders == {first.lease.worker_key, second.lease.worker_key}


async def test_living_adopter_retries_after_a_database_error(app, second_app, client, monkeypatch):
    """认领已提交后遇到临时错误，同一有效 worker 的下一次扫描能完成原任务。"""
    await app.state.task_engine.stop()
    user = await register(client)
    peer = second_app.state.task_engine
    await peer.lease.register()
    async with app.state.sessionmaker() as db:
        row = Task(user_id=user["id"], kind="outline.continue", status="running", execution_key=str(uuid4()))
        db.add(row)
        await task_statistics_service.record_created(db, row)
        await db.commit()
    attempts = []
    recover_pending = peer._durable_rich_review

    async def unavailable_once(task):
        attempts.append(task.worker_key)
        if len(attempts) == 1:
            raise OSError("模拟存储暂不可用")
        return await recover_pending(task)

    monkeypatch.setattr(peer, "_durable_rich_review", unavailable_once)
    await peer.recover()
    async with app.state.sessionmaker() as db:
        claimed = await db.get(Task, row.id)
        assert (claimed.status, claimed.worker_key) == ("running", peer.lease.worker_key)
        assert claimed.recovered_at is None
    await peer.recover()
    async with app.state.sessionmaker() as db:
        finished = await db.get(Task, row.id)
        assert finished.status == "interrupted" and finished.execution_key is None
        assert (await db.get(TaskStatistic, row.statistics_key)).status == "interrupted"
    assert attempts == [peer.lease.worker_key] * 2


async def test_fenced_worker_cannot_write_or_claim_before_adoption(app, client, fake_llm, monkeypatch):
    """在 Task.worker_key 仍等于 A 时检验数据库围栏，覆盖围栏与接管之间的间隙。"""
    source = app.state.task_engine
    await source.stop()
    _, _, (chapter, next_chapter) = await _setup(client, chapters=2, content="原稿")
    source.heartbeat_interval = 60
    source.poll_interval = 60
    await source.start()
    calls = []
    handler = get_handler("chapter.generate")
    original = handler.build_job

    async def capture(ctx, task, *, claim, execution):
        calls.append(execution)
        return await original(ctx, task, claim=claim, execution=execution)

    monkeypatch.setitem(vars(handler), "build_job", capture)
    gate = Gate()
    fake_llm.push([gate, "旧写入"])
    ref = (await _generate(client, chapter)).json()
    try:
        await gate.wait_reached()
        assert len(calls) == 1 and calls[0].worker_key == source.lease.worker_key
        await fence_worker(source)
        assert (await worker_row(source, calls[0].worker_key)).fenced_at is not None
        async with app.state.sessionmaker() as db:
            assert (await db.get(Task, ref["id"])).worker_key == calls[0].worker_key
            with pytest.raises(ConflictError):
                await require_write_execution(db, calls[0])

        # 隔离调度器，直接走同一真实领取入口。数据库已围栏、本地尚未收到心跳应答。
        with monkeypatch.context() as patch:
            patch.setattr(source, "notify", lambda: None)
            response = await _generate(client, next_chapter)
            assert response.status_code == 202, response.text
            queued = response.json()
            await source._claim_and_run(queued["id"], queued["task_key"])
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, queued["id"])
            assert row.status == "queued" and row.execution_key is None and row.worker_key is None
    finally:
        gate.release()
    await wait_until(lambda: ref["id"] not in source._running)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter["id"])).content == "原稿"


@pytest.mark.parametrize("loss", ["database_fence", "missed_heartbeat"])
async def test_lost_lease_stops_body_and_rejoins_with_new_identity(app, client, fake_llm, loss):
    """AC5：旧执行体被实际取消；同一引擎以新的登记继续领取下一章。"""
    engine = app.state.task_engine
    await engine.stop()
    _, _, (chapter, next_chapter) = await _setup(client, chapters=2, content="原稿")
    engine.heartbeat_interval = 0.1
    engine.poll_interval = 0.1
    await engine.start()
    original = engine.lease.worker_key
    first_gate, next_gate = Gate(), Gate()
    fake_llm.push([first_gate, "失效后的正文"])
    first = (await _generate(client, chapter)).json()
    try:
        await first_gate.wait_reached()
        if loss == "database_fence":
            await fence_worker(engine)
        else:
            # 将本地上一次续约推进一个租期，持久登记尚未被别人围栏。
            engine.lease._last_ok -= engine.lease_seconds
            assert not engine.lease.valid
        await wait_until(lambda: engine.lease.worker_key not in {original, None} and first["id"] not in engine._running)
        assert fake_llm.aborted == 1
        fake_llm.push([next_gate, "新执行者正文"])
        following = (await _generate(client, next_chapter)).json()
        await next_gate.wait_reached()
        async with app.state.sessionmaker() as db:
            assert (await db.get(Task, following["id"])).worker_key == engine.lease.worker_key
    finally:
        first_gate.release()
        next_gate.release()
    assert (await wait_task(client, following))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter["id"])).content == "原稿"
        assert (await db.get(Chapter, next_chapter["id"])).content == "新执行者正文"


@pytest.mark.parametrize("kind", ["chapter.analyze", "wizard.characters"])
async def test_uncertain_task_is_visible_from_both_engines_without_repeated_cleanup(
    app, second_app, client, fake_llm, monkeypatch, kind,
):
    project, _, (chapter,) = await _setup(client)
    async with app.state.sessionmaker() as db:
        actor_id = await db.scalar(select(Project.user_id).where(Project.id == project["id"]))
        row = Task(user_id=actor_id, project_id=project["id"], kind=kind, status="running",
                   execution_key=str(uuid4()), serial_key="held-key")
        db.add(row)
        await db.commit()
    first, second = app.state.task_engine, second_app.state.task_engine
    await second.start()
    async with app.state.sessionmaker() as db:
        actual = await db.get(Task, row.id)
        assert actual.recovered_at and actual.worker_key is None and actual.status == "running"
        # 改成可区分的时间戳，SQLite 秒级精度也能检测到第二次接管产生的覆盖。
        original_stamp = actual.recovered_at - timedelta(minutes=1)
        actual.recovered_at = original_stamp
        await db.commit()
    for engine in (first, second):
        await engine.recover()
        async with engine.ctx.sessionmaker() as db:
            actual = await db.get(Task, row.id)
            assert actual.recovered_at == original_stamp and actual.worker_key is None
            assert task_service.to_out(actual, engine=engine).stage == "结果待核对"
            assert await db.scalar(select(func.count()).select_from(TaskExecutionSlot)) == 0

    # 让新提交的真实任务使用相同串行键，验证待核对任务未阻止领取和落库。
    handler = get_handler("chapter.generate")
    validate = handler.validate_and_claim

    async def same_serial(*args, **kwargs):
        spec = await validate(*args, **kwargs)
        spec.serial_key = row.serial_key
        return spec

    monkeypatch.setitem(vars(handler), "validate_and_claim", same_serial)
    fake_llm.push("新的同键任务已保存")
    response = await _generate(client, chapter)
    assert response.status_code == 202, response.text
    assert (await wait_task(client, response.json()))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, response.json()["id"])).serial_key == row.serial_key
        assert (await db.get(Task, row.id)).recovered_at == original_stamp


@pytest.mark.parametrize(("age_minutes", "expected"), [(0, "executing"), (3, "failed")])
async def test_peer_start_recovers_only_expired_external_calls(
    app, second_app, client, fake_llm, age_minutes, expected,
):
    fake_mcp = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = fake_mcp
    _, _, _, change = await _stuck_external(client, fake_llm, fake_mcp, app, minutes_ago=age_minutes)
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, change["id"])).status == "executing"
    await second_app.state.task_engine.start()
    async with second_app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, change["id"])).status == expected
    assert fake_mcp.calls == []


async def test_scheduled_peer_recovers_external_created_after_start(app, second_app, client, fake_llm, monkeypatch):
    peer = second_app.state.task_engine
    peer.poll_interval = 0.1

    async def no_dispatch():
        """由原引擎领取本轮，以便验证对端周期恢复。"""

    monkeypatch.setattr(peer, "_dispatch", no_dispatch)
    await peer.start()
    ready, scanned = asyncio.Event(), asyncio.Future()
    reap = peer._reap_orphans

    async def observe_cycle():
        await ready.wait()
        await reap()
        if not scanned.done():
            async with second_app.state.sessionmaker() as db:
                row = await db.get(AgentChange, change["id"])
                scanned.set_result(row.status)

    monkeypatch.setattr(peer, "_reap_orphans", observe_cycle)
    fake_mcp = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = fake_mcp
    try:
        _, _, _, change = await _stuck_external(client, fake_llm, fake_mcp, app, minutes_ago=3)
        async with second_app.state.sessionmaker() as db:
            assert (await db.get(AgentChange, change["id"])).status == "executing"
    finally:
        ready.set()

    assert await asyncio.wait_for(scanned, 6) == "failed"
    assert fake_mcp.calls == []


async def test_one_adoption_error_does_not_delay_other_orphans(app, client, monkeypatch):
    engine = app.state.task_engine
    await engine.stop()
    await engine.lease.register()
    user = await register(client)
    async with app.state.sessionmaker() as db:
        rows = [Task(user_id=user["id"], kind="outline.continue", status="running", execution_key=str(uuid4()))
                for _ in range(2)]
        db.add_all(rows)
        await db.commit()
        first, second = (row.id for row in rows)

    adopt = workers.adopt

    async def first_unavailable(db, task_id, *args):
        if task_id == first:
            raise OSError("模拟单条认领暂时不可用")
        return await adopt(db, task_id, *args)

    with monkeypatch.context() as patch:
        patch.setattr(workers, "adopt", first_unavailable)
        await engine.recover()
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, first)).status == "running"
        assert (await db.get(Task, second)).status == "interrupted"

    await engine.recover()
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, first)).status == "interrupted"
