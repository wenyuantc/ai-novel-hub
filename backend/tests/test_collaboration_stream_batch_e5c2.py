"""E5-c2：受控任务流对批量生成（chapter.batch）运行中每一帧的发布身份证明，含批量内逐章分析帧。

真实引擎执行（fake_llm + Gate/Event），经受控 `/_test` 路由（engine.subscribe(guarded=True) + task_sse_response）
与本机 TCP 实际发送；不替换中央帧校验器。
生产路由自 E5-f 起对协作者挂载同一受控管线（tasks.py），此处 /_test 路由便于插入持帧点，作者生产订阅路径不变。

批量的发布 guard 随步骤切换：章内为本章 attempt + 同一批量章节占用；逐章分析中为分析原执行 + 收据引用；
章与章之间（含审核点、完成）为仅发布用 guard（原执行即本执行最近确认提交的断点）。帧发出时若持锁行已被
同一执行推进，只能改以同一 hub 已登记、与持锁行逐字一致的最新来源复核（不以持锁行补签）。

撤权类矩阵采用「引擎已产出带证明的帧、发送前改变授权」：持住一帧再改变，证明拒绝来自发送时的帧租约与
`SendGuard.require_task_read` 的锁内只读核对，而不是运行体自身停止。
"""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.models import Chapter, SceneReviewAnalysisReceipt, Task
from app.services import batch_service, collaboration_ai_guard
from app.services.ai_dispatch import read_visibility_stamp
from app.services.collaboration_ai_guard import (
    CollaborationSendBlocked,
    publication_execution_matches,
    publication_successor,
)
from app.services.task_engine import execution_identity, stream_evidence
from tests.helpers import Gate, analysis_json, wait_until
from tests.test_collaboration_batch_entry_e4g2 import _change as _batch_change
from tests.test_collaboration_batch_entry_e4g2 import _start as _batch_start
from tests.test_collaboration_batch_entry_e4g2 import _world
from tests.test_collaboration_stream_replay_e5b import DENIED, _send
from tests.test_collaboration_stream_running_e5c1 import (
    _Hold,
    _mount,
    _serve,
    _settled,
    _Stream,
    _task_row,
)
from tests.test_task_sse_scope import _binding

PASSWORD, NEW_PASSWORD = "secret123", "secret456new"
AUTO_ON = {"allow_private_ai_endpoints": True, "auto_analyze_enabled": True}
ANALYZING = "分析中"


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))


def _segments(events) -> list[str]:
    """按 reset 切分实时文本：首段以快照开头，其后每段是一章 attempt 的增量。"""
    segments = [next(data["text"] for event, data in events if event == "snapshot")]
    for event, data in events:
        if event == "reset":
            segments.append("")
        elif event == "delta":
            segments[-1] += data["text"]
    return segments


def _stages(events) -> list[str]:
    return [data.get("stage") or "" for event, data in events if event == "progress"]


def _split_analysis(summary="批量逐章分析概要"):
    output = analysis_json(summary=summary)
    return output[:40], output[40:80], output[80:]


async def _subscribe_mid_chapter(app, client, task, text, within=10.0):
    engine = app.state.task_engine
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text == text, within=within)


def _gate_between(monkeypatch, index=1):
    """第 index 章开始抢占前停住：此时断点已推进到章与章之间，持锁行不再变化。"""
    entered, release = asyncio.Event(), asyncio.Event()
    original = batch_service._generate_chapter

    async def gated(ctx, job, position, item):
        if position == index:
            entered.set()
            await release.wait()
        async for event in original(ctx, job, position, item):
            yield event

    monkeypatch.setattr(batch_service, "_generate_chapter", gated)
    return entered, release


# ---------------------------------------------------------------- 正例：各类帧均可证明


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
@pytest.mark.parametrize("actor", ["editor", "owner"])
async def test_batch_frames_across_chapters_and_analysis_pass_proof_over_tcp(
    app, client, make_client, fake_llm, actor,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _mount(app)
    gate = Gate()
    fake_llm.push(["第一章开头", gate, "第一章结尾"])
    fake_llm.push(analysis_json(summary="第一章分析"))
    fake_llm.push("第二章完整正文")
    fake_llm.push(analysis_json(summary="第二章分析"))
    starter = guest if actor == "editor" else client
    created = await _batch_start(starter, world)
    assert created.status_code == 202, created.text
    task = created.json()
    await gate.wait_reached()
    await _subscribe_mid_chapter(app, starter, task, "第一章开头")
    async with _serve(app) as base:
        stream = _Stream(base, starter, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            events = await stream.finish(within=30)
        finally:
            gate.release()
            await stream.aclose()
    assert stream.status == 200, stream.text
    names = [event for event, _ in events]
    assert names[0] == "snapshot" and names[-1] == "done", names
    assert {"delta", "progress", "reset", "chapter_start", "chapter_end"} <= set(names), names
    assert [data["index"] for event, data in events if event == "chapter_end"] == [0, 1]
    assert [data["index"] for event, data in events if event == "chapter_start"] == [1]
    # 逐章分析中的帧（阶段进度）也经批量分析 guard 证明后发出。
    assert sum(ANALYZING in stage for stage in _stages(events)) == 2, _stages(events)
    async with app.state.sessionmaker() as db:
        contents = [(await db.get(Chapter, chapter["id"])).content for chapter in world.chapters]
    assert _segments(events) == contents == ["第一章开头第一章结尾", "第二章完整正文"]
    done = events[-1][1]
    assert done["task"]["status"] == "succeeded" and done["task"]["task_key"] == task["task_key"]
    assert done["result"]["done"] == 2
    assert "permission_denied" not in stream.text


@pytest.mark.parametrize("actor", ["editor", "owner"])
async def test_batch_review_point_paused_frame_is_proven(app, client, make_client, fake_llm, actor):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _mount(app)
    gate = Gate()
    fake_llm.push(["审核点前", gate, "第一章其余"])
    starter = guest if actor == "editor" else client
    created = await _batch_start(starter, world, review_every=1)
    assert created.status_code == 202, created.text
    task = created.json()
    await gate.wait_reached()
    await _subscribe_mid_chapter(app, starter, task, "审核点前")
    async with _serve(app) as base:
        stream = _Stream(base, starter, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            events = await stream.finish()
        finally:
            gate.release()
            await stream.aclose()
    names = [event for event, _ in events]
    assert names[0] == "snapshot" and names[-1] == "paused", names
    assert [data["index"] for event, data in events if event == "chapter_end"] == [0]
    paused = events[-1][1]
    assert paused["task"]["status"] == "paused" and paused["result"]["done"] == 1
    assert _segments(events) == ["审核点前第一章其余"]
    assert "permission_denied" not in stream.text
    assert (await _task_row(app, task["id"])).status == "paused"


# ---------------------------------------------------------------- 撤权矩阵


async def _apply(app, owner, guest, world, change):
    if change == "password":
        response = await guest.post("/api/v1/auth/change-password", json={
            "old_password": PASSWORD, "new_password": NEW_PASSWORD,
        })
        assert response.status_code == 204, response.text
        return
    await _batch_change(app, owner, world, {
        "revoke": "revoked", "viewer": "viewer", "owner_ai": "owner_ai", "reinvite": "reinvite",
    }[change])


class _Scenario:
    """各时点：被持住的帧、运行体阻塞点，以及持住之后不得出现在流中的内容。"""

    def __init__(self, point, fake_llm, monkeypatch):
        self.point, self.gate0 = point, Gate()
        self.leaks: tuple[str, ...] = ()
        self.body: dict = {}
        self.release_runner = lambda: None
        if point == "chapter":
            marker, gate1 = "章内已产出的帧", Gate()
            fake_llm.push(["开头", self.gate0, marker, gate1, "章内撤权后不得生成"])
            self.predicate = lambda item: item[0] == "delta" and marker in item[1].get("text", "")
            self.blocked = gate1.wait_reached
            self.release_runner = gate1.release
            self.leaks = (marker, "章内撤权后不得生成")
        elif point == "between":
            fake_llm.push(["开头", self.gate0, "第一章其余"])
            fake_llm.push("第二章不得生成")
            entered, release = _gate_between(monkeypatch)
            self.predicate = lambda item: item[0] == "chapter_start" and item[1].get("index") == 1
            self.blocked = lambda: asyncio.wait_for(entered.wait(), 10)
            self.release_runner = release.set
            self.leaks = ("第二章不得生成",)
        elif point == "paused":
            fake_llm.push(["开头", self.gate0, "第一章其余"])
            self.body = {"review_every": 1}
            self.predicate = lambda item: item[0] == "paused"
            self.blocked = None
            self.leaks = ('"result"', '"awaiting_review"')
        elif point == "analysis":
            gate_a = Gate()
            first, second, rest = _split_analysis()
            fake_llm.push(["开头", self.gate0, "第一章其余"])
            fake_llm.push([first, gate_a, second, rest])
            fake_llm.push("第二章不得生成")
            self.predicate = lambda item: item[0] == "progress" and ANALYZING in (item[1].get("stage") or "")
            self.blocked = gate_a.wait_reached
            self.release_runner = gate_a.release
            self.leaks = ("第二章不得生成", ANALYZING)
        else:
            raise AssertionError(point)

    def release(self):
        self.gate0.release()
        self.release_runner()


POINTS = ("chapter", "between", "paused", "analysis")
CHANGES = ("revoke", "viewer", "owner_ai", "password", "reinvite")


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
@pytest.mark.parametrize("notify", ["notify", "silent"])
@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("point", POINTS)
async def test_batch_frame_produced_before_change_is_denied_at_send(
    app, client, make_client, fake_llm, monkeypatch, point, change, notify,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    if point != "analysis":
        # 只有逐章分析时点需要分析；其余时点关闭，避免分析模型调用打乱分片顺序。
        app.state.task_engine.ctx.settings.auto_analyze_enabled = False
    scenario = _Scenario(point, fake_llm, monkeypatch)
    hold = _Hold(scenario.predicate)
    _mount(app, [hold])
    created = await _batch_start(guest, world, **scenario.body)
    assert created.status_code == 202, created.text
    task = created.json()
    await scenario.gate0.wait_reached()
    await _subscribe_mid_chapter(app, guest, task, "开头")
    if notify == "silent":
        # 无通知：不唤醒订阅、不提前停止运行体，也不跑待审核全量复查；只有发送时的锁内复查能拦截。
        monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
        engine.collaboration_scan_interval = 3600
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            scenario.gate0.release()
            # 运行体已核验并发布该帧，订阅侧已取得、尚未发送；运行体随后阻塞（或任务已到审核点）。
            await asyncio.wait_for(hold.reached.wait(), 10)
            if scenario.blocked is not None:
                await scenario.blocked()
            else:
                await wait_until(lambda: task["id"] not in engine._running, within=10)
            await _apply(app, client, guest, world, change)
            if notify == "notify" and change == "password":
                engine.notify_collaboration_change(world.project["id"], world.guest["id"])
            if notify == "silent":
                row = await _task_row(app, task["id"])
                assert row.status == ("paused" if point == "paused" else "running"), row.status
            hold.proceed.set()
            events = await stream.finish(within=20)
            if notify == "silent" and point != "paused":
                # 拒绝来自发送侧：运行体仍阻塞，任务仍为 running。
                assert (await _task_row(app, task["id"])).status == "running"
        finally:
            hold.proceed.set()
            scenario.release()
            await stream.aclose()
    assert stream.status == 200
    assert events[0][0] == "snapshot" and events[0][1]["text"] == "开头"
    assert events[-1] == ("error", DENIED), events
    assert not any(scenario.predicate(item) for item in events[:-1]), events
    if point == "chapter":
        assert events[1:] == [("error", DENIED)], events
    for leak in scenario.leaks:
        assert leak not in stream.text, leak
    # 终止帧只能是失权帧：不得发出 done / paused（及其结果）。
    assert not any(event in ("done", "paused") for event, _ in events), events
    row = await _settled(app, task["id"], within=20)
    assert row.status != "succeeded", (row.status, row.error)


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
@pytest.mark.parametrize("point", POINTS)
async def test_held_batch_frame_is_sent_when_nothing_changes(app, client, make_client, fake_llm, monkeypatch, point):
    """正对照：同样持住已产出帧再发送，未改变授权时该帧与后续帧照常发出。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    if point != "analysis":
        app.state.task_engine.ctx.settings.auto_analyze_enabled = False
    scenario = _Scenario(point, fake_llm, monkeypatch)
    if point == "analysis":
        fake_llm.push(analysis_json(summary="第二章分析"))
    hold = _Hold(scenario.predicate)
    _mount(app, [hold])
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    created = await _batch_start(guest, world, **scenario.body)
    assert created.status_code == 202, created.text
    task = created.json()
    await scenario.gate0.wait_reached()
    await _subscribe_mid_chapter(app, guest, task, "开头")
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            scenario.gate0.release()
            await asyncio.wait_for(hold.reached.wait(), 10)
            if scenario.blocked is not None:
                await scenario.blocked()
            hold.proceed.set()
            await stream.wait_for(lambda events: any(scenario.predicate(item) for item in events))
            scenario.release_runner()
            events = await stream.finish(within=20)
        finally:
            hold.proceed.set()
            scenario.release()
            await stream.aclose()
    assert "permission_denied" not in stream.text
    assert events[-1][0] == ("paused" if point == "paused" else "done"), events[-1]
    assert (await _settled(app, task["id"])).status == ("paused" if point == "paused" else "succeeded")


async def test_notification_closes_idle_batch_stream_between_chapters(app, client, make_client, fake_llm, monkeypatch):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _mount(app)
    gate = Gate()
    fake_llm.push(["开头", gate, "第一章其余"])
    fake_llm.push("第二章不得生成")
    entered, release = _gate_between(monkeypatch)
    created = await _batch_start(guest, world)
    task = created.json()
    await gate.wait_reached()
    await _subscribe_mid_chapter(app, guest, task, "开头")
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            await asyncio.wait_for(entered.wait(), 10)
            await stream.wait_for(lambda events: any(
                event == "chapter_start" and data["index"] == 1 for event, data in events))
            await _batch_change(app, client, world, "viewer")
            # 本进程通知唤醒空闲订阅，无需等待下一帧或心跳。
            events = await stream.finish(within=5)
        finally:
            gate.release()
            release.set()
            await stream.aclose()
    assert events[-1] == ("error", DENIED)
    assert "第二章不得生成" not in stream.text
    await _settled(app, task["id"])


# ---------------------------------------------------------------- 只读核对是承重的


async def test_batch_frame_denied_when_current_chapter_claim_changed(app, client, make_client, fake_llm, monkeypatch):
    """只改本章占用，不动 Task 行与授权：Task 视图、成员与认证仍一致，唯有当前章占用只读核对能拦截。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    marker = "占用变化前已产出"
    hold = _Hold(lambda item: item[0] == "delta" and marker in item[1].get("text", ""))
    _mount(app, [hold])
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    gate0, gate1 = Gate(), Gate()
    fake_llm.push(["开头", gate0, marker, gate1, "其余"])
    created = await _batch_start(guest, world)
    task = created.json()
    await gate0.wait_reached()
    await _subscribe_mid_chapter(app, guest, task, "开头")
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate0.release()
            await asyncio.wait_for(hold.reached.wait(), 10)
            await gate1.wait_reached()
            before = await _task_row(app, task["id"])
            async with app.state.sessionmaker() as db:
                changed = await db.execute(update(Chapter).where(Chapter.id == world.chapters[0]["id"]).values(
                    generation_claim_key=str(uuid4()),
                ))
                assert changed.rowcount == 1
                await db.commit()
            after = await _task_row(app, task["id"])
            assert (after.checkpoint, after.step_cursor) == (before.checkpoint, before.step_cursor)
            hold.proceed.set()
            events = await stream.finish()
            assert (await _task_row(app, task["id"])).status == "running"
        finally:
            hold.proceed.set()
            gate0.release()
            gate1.release()
            await stream.aclose()
    assert events[1:] == [("error", DENIED)], events
    assert marker not in stream.text
    await _settled(app, task["id"])


async def test_batch_attempt_read_check_rejects_forged_attempt_even_if_publication_fails_open(
    app, client, make_client, fake_llm, monkeypatch,
):
    """持久断点换成另一 attempt：与之逐字一致的伪造身份不能由本章 guard 签发；即使发布层失守（测试内模拟），
    中央锁内只读核对仍按 guard 自己的 attempt 拒绝。原来源在原断点下可通过（正对照）。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    gate = Gate()
    fake_llm.push(["开头", gate, "其余"])
    created = await _batch_start(guest, world)
    task = created.json()
    await gate.wait_reached()
    await _subscribe_mid_chapter(app, guest, task, "开头")
    hub = engine.hubs[task["id"]]
    source = hub.source
    assert type(source) is stream_evidence.PublicationSource
    guard, execution = source.guard, source.execution
    assert guard._batch_stream_shape() == "attempt" and publication_execution_matches(guard, execution)
    checkpoint = execution.checkpoint
    forged_checkpoint = {**checkpoint, "_batch_attempt": str(uuid4())}
    forged = execution_identity.advance_checkpoint(execution, forged_checkpoint)
    assert not publication_execution_matches(guard, forged)
    with pytest.raises(ValueError):
        stream_evidence.issue_publication(forged, guard, hub=hub)
    try:
        binding = await _binding(app, world.guest["id"], task["id"], task["task_key"])
        async with app.state.sessionmaker() as db:
            await db.execute(update(Task).where(Task.id == task["id"]).values(checkpoint=forged_checkpoint))
            await db.commit()
            stamp = await read_visibility_stamp(db, world.project["id"])
        monkeypatch.setattr("app.services.collaboration_ai_guard.publication_execution_matches",
                            lambda _guard, _execution: True)
        forged_source = stream_evidence.issue_publication(forged, guard, hub=hub)
        data = {"text": "伪造 attempt 的增量"}
        evidence = stream_evidence.frame_evidence("delta", data, forged_source, stamp)
        assert await _send(app, binding, [("delta", data, evidence)]) == [("error", DENIED)]
        async with app.state.sessionmaker() as db:
            await db.execute(update(Task).where(Task.id == task["id"]).values(checkpoint=checkpoint))
            await db.commit()
        monkeypatch.undo()
        original = stream_evidence.frame_evidence("delta", data, source, stamp)
        assert await _send(app, binding, [("delta", data, original)]) == [("delta", data)]
    finally:
        gate.release()
    await _settled(app, task["id"])


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_batch_analysis_frame_denied_when_analysis_receipt_changed(
    app, client, make_client, fake_llm, monkeypatch,
):
    """逐章分析中：只改本章分析收据（Task 行与授权不变），分析 guard 的收据只读核对拦截已产出的帧。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    hold = _Hold(lambda item: item[0] == "progress" and ANALYZING in (item[1].get("stage") or ""))
    _mount(app, [hold])
    gate0, gate_a = Gate(), Gate()
    first, second, rest = _split_analysis()
    fake_llm.push(["开头", gate0, "其余"])
    fake_llm.push([first, gate_a, second, rest])
    created = await _batch_start(guest, world, last=0)
    task = created.json()
    await gate0.wait_reached()
    await _subscribe_mid_chapter(app, guest, task, "开头")
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate0.release()
            await asyncio.wait_for(hold.reached.wait(), 10)
            await gate_a.wait_reached()
            hub = engine.hubs[task["id"]]
            assert hub.source.guard._batch_stream_shape() == "analysis"
            before = await _task_row(app, task["id"])
            async with app.state.sessionmaker() as db:
                changed = await db.execute(update(SceneReviewAnalysisReceipt).where(
                    SceneReviewAnalysisReceipt.task_key == before.statistics_key,
                    SceneReviewAnalysisReceipt.status == "running",
                ).values(status="failed"))
                assert changed.rowcount == 1
                await db.commit()
            after = await _task_row(app, task["id"])
            assert (after.checkpoint, after.step_cursor) == (before.checkpoint, before.step_cursor)
            hold.proceed.set()
            events = await stream.finish()
            assert (await _task_row(app, task["id"])).status == "running"
        finally:
            hold.proceed.set()
            gate0.release()
            gate_a.release()
            await stream.aclose()
    assert events[-1] == ("error", DENIED), events
    assert not any(hold.predicate(item) for item in events[:-1])
    await _settled(app, task["id"])


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_subscription_during_analysis_proves_stale_chapter_text_with_latest_source(
    app, client, make_client, fake_llm,
):
    """逐章分析中才订阅：快照正文是上一章 attempt 发布的，其来源已被同一执行推进；只能以同一 hub 已登记的
    最新（分析）来源复核后发出，而不是等到整批结束或超时断开。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _mount(app)
    gate_a = Gate()
    first, second, rest = _split_analysis()
    fake_llm.push("第一章完整正文")
    fake_llm.push([first, gate_a, second, rest])
    created = await _batch_start(guest, world, last=0)
    task = created.json()
    await gate_a.wait_reached()
    hub = app.state.task_engine.hubs[task["id"]]
    assert hub.text == "第一章完整正文"
    text_source, latest = hub.snapshot_source(), hub.source
    assert text_source.guard._batch_stream_shape() == "attempt"
    assert latest.guard._batch_stream_shape() == "analysis"
    assert publication_successor(text_source.guard, text_source.execution, latest.guard, latest.execution)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events), within=4)
            gate_a.release()
            events = await stream.finish()
        finally:
            gate_a.release()
            await stream.aclose()
    assert events[0][0] == "snapshot" and events[0][1]["text"] == "第一章完整正文"
    assert events[-1][0] == "done" and "permission_denied" not in stream.text


# ---------------------------------------------------------------- 发布 guard 的形状与后继


async def test_batch_publication_guards_follow_current_step_and_cannot_send(app, client, make_client, fake_llm):
    """章切换即换 guard：旧章 guard 不能签发下一章的发布身份；仅发布用 guard 不能发送、预检或写入；
    不同执行（换执行键）的来源不是后继。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    gate1, gate2 = Gate(), Gate()
    fake_llm.push(["第一章", gate1, "其余"])
    fake_llm.push(["第二章", gate2, "其余"])
    created = await _batch_start(guest, world)
    task = created.json()
    try:
        await gate1.wait_reached()
        await _subscribe_mid_chapter(app, guest, task, "第一章")
        hub = engine.hubs[task["id"]]
        first = hub.source
        assert first.guard._batch_stream_shape() == "attempt" and first.guard._stream_readable()
        gate1.release()
        await gate2.wait_reached(within=10)
        await _subscribe_mid_chapter(app, guest, task, "第二章")
        second = hub.source
        assert second.guard is not first.guard
        assert second.guard._batch_attempt != first.guard._batch_attempt
        # 交出旧章的 guard：其 attempt 与当前断点不一致，不能签发。
        assert not publication_execution_matches(first.guard, second.execution)
        with pytest.raises(ValueError):
            stream_evidence.issue_publication(second.execution, first.guard, hub=hub)
        assert publication_successor(first.guard, first.execution, second.guard, second.execution)
        assert not publication_successor(second.guard, second.execution, first.guard, first.execution)
        # 不同执行（换执行键）不是后继。
        other = replace(second.execution, execution_key=str(uuid4()))
        assert not publication_successor(first.guard, first.execution, second.guard, other)
        # 仅发布用 guard（章与章之间）：形状正确，但不能发送、预检或写入。
        between = collaboration_ai_guard.for_task(
            second.execution, second.guard._binding, second.guard._stamp, cipher=second.guard._cipher,
            allow_private=True, extra_bindings=second.guard._extra_bindings, publication_only=True,
        )
        assert between._batch_stream_shape() == "between" and between._stream_readable()
        with pytest.raises(CollaborationSendBlocked):
            collaboration_ai_guard.for_task(
                second.execution, second.guard._binding, second.guard._stamp, cipher=second.guard._cipher,
                allow_private=True, extra_bindings=second.guard._extra_bindings,
            )
        async with app.state.sessionmaker() as db:
            with pytest.raises(CollaborationSendBlocked):
                await between.require_task_write(db)
        with pytest.raises(CollaborationSendBlocked):
            await between.prepare_provider(app.state.sessionmaker, engine.ctx.factory, request_timeout=5)
        async with app.state.sessionmaker() as db:
            with pytest.raises(CollaborationSendBlocked):
                await between.verify(db)
        # 非批量 guard 不能声明仅发布。
        with pytest.raises(CollaborationSendBlocked):
            collaboration_ai_guard.for_task(
                replace(second.execution, kind="chapter.generate"), second.guard._binding, second.guard._stamp,
                cipher=second.guard._cipher, allow_private=True, publication_only=True,
            )
    finally:
        gate1.release()
        gate2.release()
    assert (await _settled(app, task["id"])).status == "succeeded"


async def test_proof_waits_for_next_published_source_instead_of_failing(
    app, client, make_client, fake_llm, monkeypatch,
):
    """持锁行已被本执行推进（下一章 attempt 已提交）、而新来源尚未发布时：帧租约退出锁、等下一个已发布来源，
    再以其 guard 复核后发出；任务仍在运行（不靠终态）。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    gate, gate2 = Gate(), Gate()
    fake_llm.push(["开头", gate, "第一章其余"])
    fake_llm.push(["第二章开头", gate2, "其余"])
    refresh_entered, refresh_release = asyncio.Event(), asyncio.Event()
    waiting = asyncio.Event()
    original_event_task = engine._event_task
    original_wait = stream_evidence.PublicationSourcePending.wait

    async def gated_event_task(db, execution, current, event):
        # 在核验会话取任何锁之前停住第二章 attempt 提交后的首个事件（阶段刷新），不持锁等待。
        checkpoint = current.checkpoint or {}
        if (event == "stage" and current.step_cursor == 1 and checkpoint.get("current") is not None
                and not refresh_entered.is_set()):
            refresh_entered.set()
            await refresh_release.wait()
        return await original_event_task(db, execution, current, event)

    async def recording_wait(self):
        waiting.set()
        await original_wait(self)

    monkeypatch.setattr(engine, "_event_task", gated_event_task)
    monkeypatch.setattr(stream_evidence.PublicationSourcePending, "wait", recording_wait)
    created = await _batch_start(guest, world)
    task = created.json()
    sending = None
    try:
        await gate.wait_reached()
        await _subscribe_mid_chapter(app, guest, task, "开头")
        gate.release()
        # 第二章 attempt 与占用已提交，本 attempt 的发布来源尚未登记（阶段刷新核验被停住）。
        await asyncio.wait_for(refresh_entered.wait(), 10)
        hub = engine.hubs[task["id"]]
        between = hub.source
        assert between.guard._batch_stream_shape() == "between"
        row = await _task_row(app, task["id"])
        assert not execution_identity.matches_running(between.execution, row)
        binding = await _binding(app, world.guest["id"], task["id"], task["task_key"])
        async with app.state.sessionmaker() as db:
            stamp = await read_visibility_stamp(db, world.project["id"])
        data = {"index": 1, "marker": "章与章之间的帧"}
        evidence = stream_evidence.frame_evidence("chapter_start", data, between, stamp)
        sending = asyncio.ensure_future(_send(app, binding, [("chapter_start", data, evidence)]))
        await asyncio.wait_for(waiting.wait(), 5)
        assert not sending.done()
        refresh_release.set()
        assert await asyncio.wait_for(sending, 5) == [("chapter_start", data)]
        await gate2.wait_reached(within=10)
        assert (await _task_row(app, task["id"])).status == "running"
    finally:
        gate.release()
        gate2.release()
        refresh_release.set()
        if sending is not None and not sending.done():
            sending.cancel()
    assert (await _settled(app, task["id"])).status == "succeeded"



def _disable_lease_owner_ai_layer(monkeypatch) -> None:
    """只屏蔽帧租约层（E5-f）的作者模型授权直接核对；中央证明与 guard 内的核对不受影响。"""
    from app.services.task_engine import stream_lease

    async def _always(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(stream_lease, "owner_ai_allowed", _always)


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_superseded_batch_frame_rechecks_current_grant_with_latest_source(
    app, client, make_client, fake_llm, monkeypatch,
):
    """后继来源不是放行通道：上一章 attempt 发布的帧改由最新（分析）来源复核时，仍按当前授权在锁内只读核对。

    只关闭作者模型授权、不递增成员轮次（帧租约本身的成员/可见性复查看不到它），无通知、运行体仍阻塞：
    唯有后继来源 guard 的 require_task_read 能拦截。改变前同一帧可发出（正对照）。
    协作者本人无模型，执行绑定为作者模型，作者模型授权才是承重条件。
    E5-f 起帧租约对活动绑定也直接核对 allow_owner_ai（该层由 test_task_sse_scope 单独证明），此处将其
    置为恒真，保持本用例只由后继来源 guard 承重。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    engine = app.state.task_engine
    _disable_lease_owner_ai_layer(monkeypatch)
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    engine.collaboration_scan_interval = 3600
    gate_a = Gate()
    first, second, rest = _split_analysis()
    fake_llm.push("第一章完整正文")
    fake_llm.push([first, gate_a, second, rest])
    created = await _batch_start(guest, world, last=0)
    task = created.json()
    try:
        await gate_a.wait_reached()
        hub = engine.hubs[task["id"]]
        superseded, latest = hub.snapshot_source(), hub.source
        assert superseded.guard._batch_stream_shape() == "attempt"
        assert latest.guard._batch_stream_shape() == "analysis"
        row = await _task_row(app, task["id"])
        assert not execution_identity.matches_running(superseded.execution, row)
        assert execution_identity.matches_running(latest.execution, row)
        binding = await _binding(app, world.guest["id"], task["id"], task["task_key"])
        async with app.state.sessionmaker() as db:
            stamp = await read_visibility_stamp(db, world.project["id"])
        data = {"text": "上一章已核验的增量"}
        evidence = stream_evidence.frame_evidence("delta", data, superseded, stamp)
        assert await _send(app, binding, [("delta", data, evidence)]) == [("delta", data)]
        await _batch_change(app, client, world, "owner_ai_direct")
        assert (await _task_row(app, task["id"])).status == "running"
        assert await _send(app, binding, [("delta", data, evidence)]) == [("error", DENIED)]
        assert (await _task_row(app, task["id"])).status == "running"
    finally:
        gate_a.release()
    await _settled(app, task["id"])
