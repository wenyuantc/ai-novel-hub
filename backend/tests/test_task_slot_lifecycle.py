"""从独立连接观察正常领取的名额，覆盖迟到的提交回执、退出与失联。"""

import asyncio

import pytest
from sqlalchemy import select

from app.models import Task, TaskExecutionSlot
from app.services.task_engine import execution_slots
from tests.helpers import Gate, task_params, wait_task, wait_until
from tests.schema_helpers import isolated_worker_database
from tests.test_batch_generate import _setup as setup_batch
from tests.test_batch_generate import _start as start_batch
from tests.test_tasks import _generate, _setup
from tests.worker_helpers import expire_worker, fence_worker, freeze_heartbeat, worker_row

# 多笔真实事务完成后才会触达控制点。只断言因果次序，允许 PostgreSQL 在并行建库时等待 I/O。
BARRIER_TIMEOUT = 20


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


async def read_slot(app, task_id):
    async with app.state.sessionmaker() as db:
        return (await db.scalars(select(TaskExecutionSlot).where(TaskExecutionSlot.task_id == task_id))).one_or_none()


@pytest.mark.parametrize("fenced", [False, True], ids=["normal", "owner-fenced"])
async def test_slot_after_success_key_cleared_until_final_publication(
    app, second_app, client, fake_llm, monkeypatch, fenced,
):
    engine = app.state.task_engine
    after_clear = Gate()
    clear = engine.clear_finished_execution

    async def block_after_clear(task):
        await clear(task)
        after_clear.reached.set()
        await after_clear.released.wait()

    monkeypatch.setattr(engine, "clear_finished_execution", block_after_clear)
    _, _, (chapter,) = await _setup(client)
    fake_llm.push("落库的正文")
    started = await _generate(client, chapter)
    assert started.status_code == 202, started.text
    ref = started.json()
    task_id = ref["id"]
    try:
        await after_clear.wait_reached(within=BARRIER_TIMEOUT)
        async with second_app.state.sessionmaker() as db:
            row = await db.get(Task, task_id)
            assert row.status == "succeeded" and row.execution_key is None
        assert not engine.hubs[task_id].closed
        slot = await read_slot(second_app, task_id)
        assert slot is not None and slot.statistics_key == ref["task_key"]
        assert slot.worker_key == engine.lease.worker_key
        if fenced:
            # 即使该 worker 没有 running 行，确认失联后也要回收终态回调尚持有的名额。
            await freeze_heartbeat(engine)
            if engine.lease.sqlite_pair is not None:
                await fence_worker(engine, mirror=False)
            else:
                await expire_worker(engine)
            assert (await read_slot(second_app, task_id)).execution_key == slot.execution_key
            await second_app.state.task_engine.lease.register()
            await second_app.state.task_engine.recover()
            assert (await worker_row(engine, slot.worker_key)).fenced_at is not None
            assert await read_slot(second_app, task_id) is None
    finally:
        after_clear.release()
    await wait_until(lambda: task_id not in engine._running, within=BARRIER_TIMEOUT)
    assert await read_slot(second_app, task_id) is None
    assert len(fake_llm.calls) == 1


@pytest.mark.parametrize("applied", [False, True], ids=["rollback", "lost-ack"])
async def test_retry_of_old_slot_release_cannot_free_a_resumed_round(
    app, second_app, client, fake_llm, monkeypatch, applied,
):
    owner, peer = app.state.task_engine, second_app.state.task_engine
    owner_key = owner.lease.worker_key
    first_release, second_model = Gate(), Gate()
    original_release = execution_slots.release
    attempts = []

    async def inject_release(db, execution):
        if execution.worker_key == owner_key:
            attempts.append(execution.execution_key)
            if len(attempts) == 1:
                commit = db.commit

                async def lose_confirmation():
                    # 先真实执行 DELETE，再控制服务器最终提交或回滚，两种结果都丢失回执。
                    if applied:
                        await commit()
                    else:
                        await db.rollback()
                    first_release.reached.set()
                    await first_release.released.wait()
                    raise OSError("模拟归还提交回执丢失")

                monkeypatch.setattr(db, "commit", lose_confirmation)
        await original_release(db, execution)

    monkeypatch.setattr(execution_slots, "release", inject_release)
    _, base, (first, last), _ = await setup_batch(client, ["", ""])
    fake_llm.push("暂停前的第一章")
    fake_llm.push([second_model, "继续后的第二章"])
    started = await start_batch(client, base, first, last, review_every=1)
    assert started.status_code == 202, started.text
    ref = started.json()
    task_id = ref["id"]
    try:
        await first_release.wait_reached(within=BARRIER_TIMEOUT)
        assert (await wait_task(client, ref, statuses=("paused",)))["step_cursor"] == 1

        async def idle():
            pass

        # 第一端只归还原轮次，继续后的领取交给已注册且使用独立连接池的第二端。
        monkeypatch.setattr(owner, "_dispatch", idle)
        resumed = await client.post(f"/api/v1/tasks/{task_id}/resume", params=task_params(ref))
        assert resumed.status_code == 202, resumed.text
        assert resumed.json()["id"] == task_id
        if not applied:
            await peer.lease.register()
            assert (await read_slot(second_app, task_id)).execution_key == attempts[0]
            # 直接走门内复查，即使预选快照陈旧，同一 task_id 也必须等待原名额归还。
            await asyncio.wait_for(peer._claim_and_run(task_id, ref["task_key"]), BARRIER_TIMEOUT)
            assert not second_model.reached.is_set()
            first_release.release()
            await wait_until(lambda: task_id not in owner._running, within=BARRIER_TIMEOUT)
        else:
            assert await read_slot(second_app, task_id) is None
        peer.poll_interval = 0.1
        await peer.start()
        await second_model.wait_reached(within=BARRIER_TIMEOUT)
        new_slot = await read_slot(app, task_id)
        assert new_slot.worker_key == peer.lease.worker_key
        assert new_slot.execution_key != attempts[0] and new_slot.statistics_key == ref["task_key"]
        first_release.release()
        await wait_until(lambda: task_id not in owner._running, within=BARRIER_TIMEOUT)
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert (await read_slot(app, task_id)).execution_key == new_slot.execution_key
    finally:
        first_release.release()
        second_model.release()
    assert (await wait_task(client, ref, within=BARRIER_TIMEOUT))["status"] == "succeeded"
    await wait_until(lambda: task_id not in peer._running, within=BARRIER_TIMEOUT)
    assert await read_slot(app, task_id) is None and len(fake_llm.calls) == 2


async def test_late_duplicate_release_leaves_next_round_on_same_worker(
    app, second_app, client, fake_llm, monkeypatch,
):
    engine = app.state.task_engine
    release = execution_slots.release
    released = []

    async def capture(db, execution):
        await release(db, execution)
        released.append(execution)

    monkeypatch.setattr(execution_slots, "release", capture)
    _, base, (first, last), _ = await setup_batch(client, ["", ""])
    second_model = Gate()
    fake_llm.push("第一章")
    fake_llm.push([second_model, "第二章"])
    started = await start_batch(client, base, first, last, review_every=1)
    assert started.status_code == 202, started.text
    ref = started.json()
    task_id = ref["id"]
    await wait_until(lambda: len(released) == 1 and task_id not in engine._running, within=BARRIER_TIMEOUT)
    original = released[0]
    try:
        resumed = await client.post(f"/api/v1/tasks/{task_id}/resume", params=task_params(ref))
        assert resumed.status_code == 202, resumed.text
        await second_model.wait_reached(within=BARRIER_TIMEOUT)
        slot = await read_slot(second_app, task_id)
        assert slot.worker_key == original.worker_key and slot.statistics_key == original.task_key
        assert slot.execution_key != original.execution_key
        async with app.state.sessionmaker() as db:
            await release(db, original)
        assert (await read_slot(second_app, task_id)).execution_key == slot.execution_key
    finally:
        second_model.release()
    assert (await wait_task(client, ref, within=BARRIER_TIMEOUT))["status"] == "succeeded"
    await wait_until(lambda: len(released) == 2 and task_id not in engine._running, within=BARRIER_TIMEOUT)
    assert await read_slot(second_app, task_id) is None and len(fake_llm.calls) == 2


@pytest.mark.parametrize("reason", ["cancel", "stop", "stop-release-unavailable", "exception"])
async def test_release_on_executor_exit(app, second_app, client, fake_llm, monkeypatch, reason):
    engine = app.state.task_engine
    model, releasing = Gate(), Gate()
    original_release = execution_slots.release

    async def hold_release(db, execution):
        releasing.reached.set()
        await releasing.released.wait()
        if reason == "stop-release-unavailable":
            raise OSError("模拟归还事务暂时无法写入")
        await original_release(db, execution)

    monkeypatch.setattr(execution_slots, "release", hold_release)
    if reason == "exception":
        async def fail_after_claim(*args, **kwargs):
            raise OSError("模拟执行体已领取后意外退出")

        monkeypatch.setattr(engine, "_run", fail_after_claim)
    _, _, (chapter,) = await _setup(client)
    fake_llm.push([model, "正文"])
    started = await _generate(client, chapter)
    assert started.status_code == 202, started.text
    ref = started.json()
    task_id = ref["id"]
    action = None
    try:
        if reason != "exception":
            await model.wait_reached(within=BARRIER_TIMEOUT)
            if reason.startswith("stop"):
                action = asyncio.create_task(engine.stop())
            else:
                action = asyncio.create_task(client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(ref)))
        await releasing.wait_reached(within=BARRIER_TIMEOUT)
        slot = await read_slot(second_app, task_id)
        assert slot is not None and slot.worker_key == engine.lease.worker_key
        if reason.startswith("stop"):
            assert not action.done() and (await worker_row(engine, slot.worker_key)).stopped_at is None
    finally:
        releasing.release()
        model.release()
        if action is not None:
            result = await asyncio.wait_for(action, BARRIER_TIMEOUT)
            if reason == "cancel":
                assert result.status_code == 200, result.text
    await wait_until(lambda: task_id not in engine._running, within=BARRIER_TIMEOUT)
    assert await read_slot(second_app, task_id) is None
    if reason == "exception":
        await engine.recover()
    final = await wait_task(client, ref, within=BARRIER_TIMEOUT)
    assert final["status"] == ("cancelled" if reason == "cancel" else "interrupted")
