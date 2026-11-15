"""设定类向导「同执行戳后继」（2026-11-13）：反例、等待来源与形状。

保存前产出、在本任务收紧可见性的保存提交后才发出的帧，只能由同一 hub 登记的本执行完成仅发布 guard
（保存事务内的新可见性戳、由原未决凭据派生的完成收据）证明，并在帧租约锁内按当前授权复核。
他人造成的可见性变化、撤权/降级/关作者模型授权（有无通知）都必须仍被拒；不以持锁行补签，不跨执行。
正例与原已知窗口的翻转见 ``test_collaboration_stream_wizard_e5c3.py``。真实引擎 + fake_llm + Gate/Event，
受控 `/_test` 路由经本机 TCP 实际发送，不替换中央帧校验器；无 sleep 定时。
"""

import asyncio
from contextlib import aclosing
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.models import Task
from app.services import setting_ai_visibility
from app.services.ai_dispatch import read_visibility_stamp
from app.services.collaboration_ai_guard import (
    for_task,
    wizard_publication_successor,
    wizard_successor_eligible,
)
from app.services.task_engine import execution_identity, stream_evidence
from app.services.task_engine.handlers.wizard import WizardHandler
from tests.helpers import Gate, wait_until
from tests.test_collaboration_entity_entry_e4g3b import _change, _world
from tests.test_collaboration_stream_replay_e5b import DENIED
from tests.test_collaboration_stream_running_e5c1 import _Hold, _mount, _serve, _settled, _Stream, _task_row
from tests.test_collaboration_stream_wizard_e5c3 import _capture_jobs, _launch, _pieces, _text
from tests.test_collaboration_wizard_entry_e4g3c import ANSWERS, NEW_NAMES, _names

OLD = ["乙", "甲"]
HEAD, MARKER, REST = _pieces(ANSWERS["characters"])


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


async def _stamp(app, world):
    async with app.state.sessionmaker() as db:
        return await read_visibility_stamp(db, world.project["id"])


def _silence(app, monkeypatch):
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)


def _disable_lease_owner_ai_layer(monkeypatch) -> None:
    """只屏蔽帧租约层（E5-f）的作者模型授权直接核对；后继 guard 内的核对不受影响。"""
    from app.services.task_engine import stream_lease

    async def _always(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(stream_lease, "owner_ai_allowed", _always)


def _spy_pending(monkeypatch) -> asyncio.Event:
    """帧在租约外等待下一个已发布来源（PublicationSourcePending）时置位；仍调用真实中央校验器。"""
    raised = asyncio.Event()
    original = stream_evidence.verify_stream_frame

    async def spy(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except stream_evidence.PublicationSourcePending:
            raised.set()
            raise

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", spy)
    return raised


def _hold_done_event(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    """运行体保存（内容 + 完成收据）已提交、尚未把 done 交给引擎核验时停住。"""
    reached, release = asyncio.Event(), asyncio.Event()
    original = WizardHandler.run

    def run(self, job, ctx):
        async def events():
            async with aclosing(original(self, job, ctx)) as source:
                async for event, data in source:
                    if event == "done":
                        reached.set()
                        await release.wait()
                    yield event, data

        return events()

    monkeypatch.setattr(WizardHandler, "run", run)
    return reached, release


async def _held_marker(app, client_for, world, fake_llm, chunks):
    hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == MARKER)
    _mount(app, [hold])
    task = await _launch(client_for, world, "characters", fake_llm, chunks)
    return hold, task


def _assert_denied(events, stream):
    assert events[0][0] == "snapshot" and events[0][1]["text"] == HEAD, events
    assert events[1:] == [("error", DENIED)], events
    assert MARKER not in stream.text and '"result"' not in stream.text


# ---------------------------------------------------------------- 他人造成的可见性变化


@pytest.mark.parametrize("tighten", [True, False])
async def test_other_visibility_change_after_own_save_denies_in_flight_frame(
    app, client, make_client, fake_llm, monkeypatch, tighten,
):
    """本任务收紧保存已提交、在途帧尚未发出时作者再改可见性：完成来源的新戳不再成立 → 拒绝。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _silence(app, monkeypatch)
    gate, gate2 = Gate(), Gate()
    hold, task = await _held_marker(app, guest, world, fake_llm, [HEAD, gate, MARKER, gate2, REST])
    await gate.wait_reached()
    await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
    before = await _stamp(app, world)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            await asyncio.wait_for(hold.reached.wait(), 15)
            gate2.release()
            assert (await _settled(app, task["id"])).status == "succeeded"
            saved = await _stamp(app, world)
            assert saved.epoch > before.epoch
            async with app.state.sessionmaker() as db:
                await setting_ai_visibility.bump_visibility_revision(db, world.project["id"], tighten=tighten)
                await db.commit()
            hold.proceed.set()
            events = await stream.finish()
        finally:
            hold.proceed.set()
            gate.release()
            gate2.release()
            await stream.aclose()
    _assert_denied(events, stream)
    assert await _names(app, world, "characters") == sorted(NEW_NAMES["characters"])


async def test_other_epoch_change_between_build_and_save_denies_in_flight_frame(
    app, client, make_client, fake_llm, monkeypatch,
):
    """构建后、保存前他人改 epoch：保存按构建戳失败、没有完成来源，在途帧不能借后继证明发出。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _silence(app, monkeypatch)
    gate1, gate2 = Gate(), Gate()
    hold, task = await _held_marker(app, guest, world, fake_llm, [HEAD, gate1, MARKER, gate2, REST])
    await gate1.wait_reached()
    await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate1.release()
            await asyncio.wait_for(hold.reached.wait(), 15)
            await gate2.wait_reached()
            async with app.state.sessionmaker() as db:
                await setting_ai_visibility.bump_visibility_revision(db, world.project["id"], tighten=True)
                await db.commit()
            gate2.release()
            row = await _settled(app, task["id"])
            assert row.status != "succeeded", (row.status, row.error)
            hold.proceed.set()
            events = await stream.finish()
        finally:
            hold.proceed.set()
            gate1.release()
            gate2.release()
            await stream.aclose()
    _assert_denied(events, stream)
    assert await _names(app, world, "characters") == OLD


async def test_forged_completion_successor_after_other_tightening_is_denied(
    app, client, make_client, fake_llm, monkeypatch,
):
    """他人收紧后伪造「完成来源」（收据 nonce 与原未决凭据不同、戳为他人变化后的戳）登记到同一 hub，
    且持锁行与之逐字一致：唯有后继判定的完成收据逐项核对能拦截（承重）。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _silence(app, monkeypatch)
    pending_raised = _spy_pending(monkeypatch)
    gate1, gate2 = Gate(), Gate()
    hold, task = await _held_marker(app, guest, world, fake_llm, [HEAD, gate1, MARKER, gate2, REST])
    engine = app.state.task_engine
    await gate1.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate1.release()
            await asyncio.wait_for(hold.reached.wait(), 15)
            await gate2.wait_reached()
            hub = engine.hubs[task["id"]]
            source = hub.source
            guard, execution = source.guard, source.execution
            assert wizard_successor_eligible(guard)
            async with app.state.sessionmaker() as db:
                await setting_ai_visibility.bump_visibility_revision(db, world.project["id"], tighten=True)
                await db.commit()
            other = await _stamp(app, world)
            base_checkpoint = dict(execution.checkpoint)
            marker = base_checkpoint.pop("_wizard_pending")
            result = {"kind": "characters", "items": []}
            receipt = {**marker, "nonce": str(uuid4()), "result": result, "result_hash": "r" * 64,
                       "stream_hash": "s" * 64}
            forged_checkpoint = {**base_checkpoint, "_wizard_completion": receipt}
            forged = execution_identity.advance_checkpoint(execution, forged_checkpoint)
            forged_guard = for_task(forged, guard._binding, other, cipher=guard._cipher,
                                    allow_private=guard._allow_private, publication_only=True,
                                    extra_bindings=guard._extra_bindings)
            async with app.state.sessionmaker() as db:
                changed = await db.execute(update(Task).where(Task.id == task["id"]).values(
                    checkpoint=forged_checkpoint,
                ))
                assert changed.rowcount == 1
                await db.commit()
            hub.source = stream_evidence.issue_publication(forged, forged_guard, hub=hub)
            # 除完成收据外全部成立：同一执行、同绑定、新戳当前有效、持锁行与伪造来源逐字一致。
            assert not wizard_publication_successor(guard, execution, forged_guard, forged, hold.item[2].visibility)
            hold.proceed.set()
            # 伪造来源不是后继：帧不能发出，只能退出租约等待下一来源。
            await asyncio.wait_for(pending_raised.wait(), 10)
            assert MARKER not in stream.text
            # 还原持锁行与 hub 原来源（唤醒等待）：原未决执行仍在运行而戳已被他人改变 → 拒绝。
            async with app.state.sessionmaker() as db:
                await db.execute(update(Task).where(Task.id == task["id"]).values(
                    checkpoint=execution.checkpoint,
                ))
                await db.commit()
            hub.source = source
            events = await stream.finish()
            gate2.release()
            assert (await _settled(app, task["id"])).status != "succeeded"
        finally:
            hold.proceed.set()
            gate1.release()
            gate2.release()
            await stream.aclose()
    _assert_denied(events, stream)
    assert await _names(app, world, "characters") == OLD


# ---------------------------------------------------------------- 在途帧期间改变授权


@pytest.mark.parametrize("notify", ["notify", "silent"])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai"])
async def test_change_after_own_save_denies_in_flight_frame(
    app, client, make_client, fake_llm, monkeypatch, change, notify,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    if notify == "silent":
        _silence(app, monkeypatch)
    gate, gate2 = Gate(), Gate()
    hold, task = await _held_marker(app, guest, world, fake_llm, [HEAD, gate, MARKER, gate2, REST])
    await gate.wait_reached()
    await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            await asyncio.wait_for(hold.reached.wait(), 15)
            gate2.release()
            assert (await _settled(app, task["id"])).status == "succeeded"
            await _change(app, client, world, change)
            hold.proceed.set()
            events = await stream.finish()
        finally:
            hold.proceed.set()
            gate.release()
            gate2.release()
            await stream.aclose()
    _assert_denied(events, stream)
    assert await _names(app, world, "characters") == sorted(NEW_NAMES["characters"])


async def test_direct_owner_ai_close_after_own_save_is_rechecked_by_successor_guard(
    app, client, make_client, fake_llm, monkeypatch,
):
    """协作者无本人模型（绑定作者模型）：直接关闭 allow_owner_ai（不递增成员轮次、无通知），
    帧租约的成员复查不变；唯有后继 guard 的锁内当前授权复核能拦截（承重）。正对照：改变前同类帧照常发出。
    E5-f 起帧租约对活动绑定也直接核对 allow_owner_ai（该层由 test_task_sse_scope 单独证明），此处将其
    置为恒真，保持本用例只由后继 guard 承重。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    _silence(app, monkeypatch)
    _disable_lease_owner_ai_layer(monkeypatch)
    first = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == MARKER)
    # 订阅侧会合并积压的连续增量：保存后的其余正文作为一帧到达。
    second = _Hold(lambda item: item[0] == "delta" and "新角色乙" in item[1].get("text", ""))
    _mount(app, [first, second])
    gate = Gate()
    gate2 = Gate()
    task = await _launch(guest, world, "characters", fake_llm, [HEAD, gate, MARKER, gate2, REST])
    await gate.wait_reached()
    await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            await asyncio.wait_for(first.reached.wait(), 15)
            gate2.release()
            assert (await _settled(app, task["id"])).status == "succeeded"
            assert (await _stamp(app, world)).epoch > 0
            # 正对照：保存后、改变前，被持住的未决帧经后继证明发出。
            first.proceed.set()
            await stream.wait_for(lambda events: any(
                event == "delta" and data["text"] == MARKER for event, data in events))
            await asyncio.wait_for(second.reached.wait(), 15)
            await _change(app, client, world, "owner_ai_direct")
            second.proceed.set()
            events = await stream.finish()
        finally:
            first.proceed.set()
            second.proceed.set()
            gate.release()
            gate2.release()
            await stream.aclose()
    assert events[-1] == ("error", DENIED), events
    assert "新角色乙" not in stream.text and '"result"' not in stream.text
    assert _text(events[:-1]) == HEAD + MARKER


# ---------------------------------------------------------------- 等待完成来源（租约外）


@pytest.mark.parametrize("change", [None, "revoked"])
async def test_frame_waits_outside_lease_for_completion_source(
    app, client, make_client, fake_llm, monkeypatch, change,
):
    """保存已提交、done 尚未交给引擎核验：在途帧不能用持锁行补签，只能退出租约等同一 hub 发布完成来源。

    无变化时完成来源发布后帧发出、最终 done；等待期间撤权（无通知）则 done 核验失败、完成来源不会发布，
    hub 关闭后帧被拒。等待期间不持锁：运行体照常完成核验与终态写入。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _silence(app, monkeypatch)
    pending_raised = _spy_pending(monkeypatch)
    done_reached, done_release = _hold_done_event(monkeypatch)
    jobs = _capture_jobs(monkeypatch)
    gate, gate2 = Gate(), Gate()
    hold, task = await _held_marker(app, guest, world, fake_llm, [HEAD, gate, MARKER, gate2, REST])
    engine = app.state.task_engine
    await gate.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            await asyncio.wait_for(hold.reached.wait(), 15)
            gate2.release()
            await asyncio.wait_for(done_reached.wait(), 15)
            row = await _task_row(app, task["id"])
            assert row.status == "running" and "_wizard_completion" in row.checkpoint
            hub = engine.hubs[task["id"]]
            assert hub.source.guard is jobs[task["id"]].guard
            hold.proceed.set()
            await asyncio.wait_for(pending_raised.wait(), 10)
            assert MARKER not in stream.text
            if change is not None:
                await _change(app, client, world, change)
            done_release.set()
            await _settled(app, task["id"])
            events = await stream.finish()
        finally:
            hold.proceed.set()
            done_release.set()
            gate.release()
            gate2.release()
            await stream.aclose()
    assert await _names(app, world, "characters") == sorted(NEW_NAMES["characters"])
    if change is None:
        assert events[-1][0] == "done", events
        assert _text(events) == ANSWERS["characters"]
        assert "permission_denied" not in stream.text
    else:
        _assert_denied(events, stream)


# ---------------------------------------------------------------- 后继判定的形状（承重）


async def test_wizard_publication_successor_shape(app, client, make_client, fake_llm, monkeypatch):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _silence(app, monkeypatch)
    jobs = _capture_jobs(monkeypatch)
    end = Gate()
    fake_llm.push([ANSWERS["characters"], end])
    created = await guest.post(f"{world.base}/wizard/characters", json={})
    assert created.status_code == 202, created.text
    task = created.json()
    await end.wait_reached()
    job = jobs[task["id"]]
    guard, original = job.guard, job.pending.active
    end.release()
    await _settled(app, task["id"])
    later = job.publication_guard[1]
    completed = later._task
    build, saved = guard._stamp, later._stamp
    assert saved.epoch > build.epoch
    assert wizard_successor_eligible(guard) and not wizard_successor_eligible(later)
    # 正对照：帧投影戳为构建戳或保存新戳。
    assert wizard_publication_successor(guard, original, later, completed, build)
    assert wizard_publication_successor(guard, original, later, completed, saved)
    # 第三方戳（他人变化后的戳）不可。
    assert not wizard_publication_successor(guard, original, later, completed, replace(saved, epoch=saved.epoch + 1))
    assert not wizard_publication_successor(guard, original, later, completed, None)
    # 方向与类型：完成 → 未决、自身、未决 → 未决都不是后继。
    assert not wizard_publication_successor(later, completed, guard, original, build)
    assert not wizard_publication_successor(guard, original, guard, original, build)
    # 收据与原未决凭据不一致（nonce 换成另一合法 UUID，仍是合法完成形状）或断点夹带其他变化。
    receipt = completed.checkpoint["_wizard_completion"]
    forged = execution_identity.advance_checkpoint(
        completed, {**completed.checkpoint, "_wizard_completion": {**receipt, "nonce": str(uuid4())}},
    )
    forged_guard = for_task(forged, later._binding, saved, cipher=later._cipher,
                            allow_private=later._allow_private, publication_only=True,
                            extra_bindings=later._extra_bindings)
    assert forged_guard._wizard_stream_shape() == "completed"
    assert not wizard_publication_successor(guard, original, forged_guard, forged, build)
    extra = execution_identity.advance_checkpoint(completed, {**completed.checkpoint, "other": 1})
    extra_guard = for_task(extra, later._binding, saved, cipher=later._cipher,
                           allow_private=later._allow_private, publication_only=True,
                           extra_bindings=later._extra_bindings)
    assert not wizard_publication_successor(guard, original, extra_guard, extra, build)
    # 游标推进、跨执行（不同 execution_key）不可。
    moved = execution_identity.advance_checkpoint(completed, completed.checkpoint, step_cursor=1)
    moved_guard = for_task(moved, later._binding, saved, cipher=later._cipher,
                           allow_private=later._allow_private, publication_only=True,
                           extra_bindings=later._extra_bindings)
    assert not wizard_publication_successor(guard, original, moved_guard, moved, build)
    other_run = replace(completed, execution_key=str(uuid4()))
    assert not wizard_publication_successor(guard, original, later, other_run, build)
    # 新戳换删除代次（软删除再恢复）不可。
    recycled = for_task(completed, later._binding, replace(saved, last_deleted_at=datetime.now(UTC)),
                        cipher=later._cipher, allow_private=later._allow_private, publication_only=True,
                        extra_bindings=later._extra_bindings)
    assert not wizard_publication_successor(guard, original, recycled, completed, build)
    # 私网开关不同（其余全部一致）不可：后继须与构建 guard 同一出站策略。
    private_flip = for_task(completed, later._binding, saved, cipher=later._cipher,
                            allow_private=not later._allow_private, publication_only=True,
                            extra_bindings=later._extra_bindings)
    assert not wizard_publication_successor(guard, original, private_flip, completed, build)
    # E6-d：协作执行冻结的共享向量选择（额外绑定）必须与构建 guard 一致，缺失或替换都不是后继。
    assert len(guard._extra_bindings) == 1 and later._extra_bindings == guard._extra_bindings
    assert not wizard_publication_successor(guard, original, replace(later, _extra_bindings=()), completed, build)
    # 大纲向导不适用（保存不改可见性），其构建 guard 不是后继对象。
    end2 = Gate()
    fake_llm.push([ANSWERS["outline"], end2])
    created = await guest.post(f"{world.base}/wizard/outline", json={})
    assert created.status_code == 202, created.text
    await end2.wait_reached()
    outline_job = jobs[created.json()["id"]]
    assert outline_job.guard._wizard_stream_shape() == "pending" and not wizard_successor_eligible(outline_job.guard)
    end2.release()
    assert (await _settled(app, created.json()["id"])).status == "succeeded"
