"""E5-d：收据受限收尾（章节 / 分析 / 批量 / 向导）的终态帧改为带任务快照的 `done`。

内容已随完成收据提交、完成事件核验因授权或设置变化未通过时，任务按收据受限收尾为 succeeded（error 为固定 NOTE）。
终态帧现以按原轮次重读的已提交 Task 行构造：`done {task, result: None, unpublished: True, detail}`，与持久回放
（`persisted_replay_events`）同一映射；受控订阅按 E5-b 持久回放证明在发送时锁内重算并按订阅原绑定复核授权。

触发方式：完成事件核验取锁之前（`_event_task` 的 done），先等订阅者实际收到运行体至今发布的全部帧，再递增作品
可见性 revision（作者改设定），使 done 核验失败而订阅授权不变。真实引擎（fake_llm + Gate）经本机 TCP 发送。
"""

import asyncio
import json
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.models import ProjectCollaborator
from app.services import batch_service, setting_ai_visibility
from app.services.task_engine import engine as engine_module
from app.services.task_engine.hub import TaskHub
from tests.helpers import Gate, analysis_json, wait_until
from tests.test_collaboration_batch_entry_e4g2 import _start as _batch_start
from tests.test_collaboration_batch_entry_e4g2 import _world as _batch_world
from tests.test_collaboration_chapter_entry_e4f import _world as _chapter_world
from tests.test_collaboration_entity_entry_e4g3b import _world as _wizard_world
from tests.test_collaboration_stream_replay_e5b import DENIED
from tests.test_collaboration_stream_running_e5c1 import _Hold, _mount, _serve, _settled, _Stream

KINDS = ("chapter", "analysis", "batch", "wizard")
MARK = "收据收尾未发布的结果标记"
NOTES = {
    "chapter": (engine_module.RECEIPT_FINALIZED_NOTE, engine_module.RECEIPT_FINALIZED_DETAIL),
    "analysis": (engine_module.ANALYSIS_RECEIPT_FINALIZED_NOTE, engine_module.ANALYSIS_RECEIPT_FINALIZED_DETAIL),
    "batch": (engine_module.BATCH_RECEIPT_FINALIZED_NOTE, engine_module.BATCH_RECEIPT_FINALIZED_DETAIL),
    "wizard": (engine_module.WIZARD_RECEIPT_FINALIZED_NOTE, engine_module.WIZARD_RECEIPT_FINALIZED_DETAIL),
}


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))


class _ProductionStream(_Stream):
    """作者生产订阅入口（非受控管线）。"""

    async def _run(self, task):
        try:
            async with self._client.stream("GET", f"/api/v1/tasks/{task['id']}/stream",
                                           params={"task_key": task["task_key"]}) as response:
                self.status = response.status_code
                async for chunk in response.aiter_text():
                    self.text += chunk
        finally:
            self.closed.set()


class _Recorder:
    """记录每个 hub 实际发布的帧及最近一次订阅的位置，用于判断订阅者已收齐（不靠计时）。"""

    def __init__(self, monkeypatch):
        self.frames: dict[int, list[tuple[str, dict]]] = {}
        self.marks: dict[int, int] = {}
        publish, subscribe = TaskHub.publish, TaskHub.subscribe
        recorder = self

        def recording_publish(hub, event, data):
            recorder.frames.setdefault(hub.task_id, []).append((event, deepcopy(data)))
            return publish(hub, event, data)

        def recording_subscribe(hub, *, guarded=False):
            result = subscribe(hub, guarded=guarded)
            recorder.marks[hub.task_id] = len(recorder.frames.get(hub.task_id, []))
            return result

        monkeypatch.setattr(TaskHub, "publish", recording_publish)
        monkeypatch.setattr(TaskHub, "subscribe", recording_subscribe)

    def caught_up(self, task_id: int, events) -> bool:
        if not events or events[0][0] != "snapshot" or task_id not in self.marks:
            return False
        published = self.frames.get(task_id, [])[self.marks[task_id]:]
        received = events[1:]

        def text(frames):
            return "".join(data["text"] for event, data in frames if event == "delta")

        def names(frames):
            return [event for event, _ in frames if event != "delta"]

        return bool(published) and text(received) == text(published) and names(received) == names(published)


def _arm_before_done(app, monkeypatch, change, wait):
    """done 核验取锁之前：先等订阅者收齐至今已发布的帧（批量末章保存后的 chapter_end/progress），
    再完整提交 change（内容已随收据提交）。"""
    engine = app.state.task_engine
    original = type(engine)._event_task
    fired: list[str] = []

    async def run():
        await wait()
        await change()

    async def wrapped(self, db, execution, current, event):
        if event == "done" and not fired:
            fired.append(event)
            await asyncio.shield(asyncio.ensure_future(run()))
        return await original(self, db, execution, current, event)

    monkeypatch.setattr(type(engine), "_event_task", wrapped)
    return fired


async def _bump_visibility(app, project_id):
    async with app.state.sessionmaker() as db:
        await setting_ai_visibility.bump_visibility_revision(db, project_id, tighten=False)
        await db.commit()


async def _member_change(app, owner, world, change):
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"], ProjectCollaborator.user_id == world.guest["id"],
        ))
    url = f"/api/v1/projects/{world.project['id']}/collaborators/{world.guest['id']}"
    if change == "revoked":
        response = await owner.delete(url, params={"expected_revision": member.revision})
        assert response.status_code == 204, response.text
    else:
        response = await owner.patch(url, json={"expected_revision": member.revision, "role": "viewer"})
        assert response.status_code == 200, response.text


async def _launch(kind, owner, guest, fake_llm, actor):
    """启动一个首段后停在 gate 的任务；之后的输出都带 MARK（也会进入收据重建的 result）。

    末尾 end：运行体在模型流结束（保存）之前停住，直到订阅者实际收到全部已发布帧。保存提交后才发送的在途帧
    只能由终态证明（E5-c1），而收据收尾不签发执行体终态证明，这类帧会失败关闭（见规范「已知窗口」）。
    """
    client = guest if actor == "editor" else owner
    gate, end = Gate(), Gate()
    if kind in ("chapter", "analysis"):
        world = await _chapter_world(owner, guest, allow_owner_ai=True)
        if kind == "chapter":
            fake_llm.push(["收尾开头", gate, MARK, end])
            created = await client.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
        else:
            output = analysis_json(summary=MARK)
            fake_llm.push([output[:30], gate, output[30:], end])
            created = await client.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
    elif kind == "batch":
        world = await _batch_world(owner, guest)
        fake_llm.push(["第一章开头", gate, MARK + "一"])
        fake_llm.push([MARK + "二", end])
        created = await _batch_start(client, world)
    else:
        world = await _wizard_world(owner, guest, allow_owner_ai=True)
        answer = json.dumps([{"title": "新章纲", "summary": MARK}], ensure_ascii=False)
        fake_llm.push([answer[:8], gate, answer[8:], end])
        created = await client.post(f"{world.base}/wizard/outline", json={})
    assert created.status_code == 202, created.text
    return world, created.json(), gate, end, client


def _assert_unpublished_done(kind, frame, task):
    event, data = frame
    note, detail = NOTES[kind]
    assert event == "done", frame
    # 形状：任务快照 + 空 result + 未发布标记 + 固定说明；不含收据重建的结果。
    assert set(data) == {"task", "result", "unpublished", "detail"}, data
    assert data["result"] is None and data["unpublished"] is True and data["detail"] == detail
    assert data["task"]["id"] == task["id"] and data["task"]["task_key"] == task["task_key"]
    assert data["task"]["status"] == "succeeded" and data["task"]["error"] == note
    assert MARK not in json.dumps(frame, ensure_ascii=False)


async def _assert_receipt_row(app, kind, task_id):
    row = await _settled(app, task_id)
    note, _ = NOTES[kind]
    assert row.status == "succeeded" and row.error == note and row.execution_key is None, (row.status, row.error)
    # 收据重建的结果保存在持久行（详情/裁剪证明用），但不进入任何帧。
    assert row.result is not None
    return row


async def _run_live(app, monkeypatch, kind, actor, route, client, make_client, fake_llm, *, change=None, holds=None,
                    on_launch=None):
    guest = make_client()
    recorder = _Recorder(monkeypatch)
    if route == "guarded":
        _mount(app, holds)
    world_box: dict = {}

    async def wait():
        stream = world_box["stream"]
        await wait_until(lambda: recorder.caught_up(world_box["task"]["id"], stream.events()), within=15)

    async def do_change():
        if change is None:
            await _bump_visibility(app, world_box["world"].project["id"])
        else:
            await change(world_box["world"])

    fired = _arm_before_done(app, monkeypatch, do_change, wait)
    world, task, gate, end, starter = await _launch(kind, client, guest, fake_llm, actor)
    world_box["world"], world_box["task"] = world, task
    if on_launch is not None:
        on_launch(world, task)
    engine = app.state.task_engine
    await gate.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text)
    stream_type = _Stream if route == "guarded" else _ProductionStream
    async with _serve(app) as base:
        stream = stream_type(base, starter, task)
        world_box["stream"] = stream
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            await end.wait_reached()
            await stream.wait_for(lambda events: recorder.caught_up(task["id"], events), within=15)
            end.release()
            events = await stream.finish(within=20)
        finally:
            gate.release()
            end.release()
            for hold in holds or ():
                hold.proceed.set()
            await stream.aclose()
    assert fired == ["done"]
    return world, task, starter, stream, events


async def _replay(app, route, starter, task):
    stream_type = _Stream if route == "guarded" else _ProductionStream
    async with _serve(app) as base:
        stream = stream_type(base, starter, task)
        try:
            events = await stream.finish(within=15)
        finally:
            await stream.aclose()
    return stream, events


# ---------------------------------------------------------------- 正例：实时与回放形状一致


@pytest.mark.parametrize(("actor", "route"), [("editor", "guarded"), ("owner", "guarded"), ("owner", "production")])
@pytest.mark.parametrize("kind", KINDS)
async def test_receipt_completion_closes_with_unpublished_done_live_and_replay(
    app, client, make_client, fake_llm, monkeypatch, kind, actor, route,
):
    world, task, starter, stream, events = await _run_live(
        app, monkeypatch, kind, actor, route, client, make_client, fake_llm,
    )
    assert stream.status == 200, stream.text
    assert events[0][0] == "snapshot" and "permission_denied" not in stream.text, str([e for e, _ in events])
    _assert_unpublished_done(kind, events[-1], task)
    row = await _assert_receipt_row(app, kind, task["id"])
    assert row.id not in app.state.task_engine.hubs
    # 回放（重新订阅已结束任务）：快照 + 与实时完全相同的 done 帧。
    replay_stream, replay = await _replay(app, route, starter, task)
    assert replay_stream.status == 200, replay_stream.text
    assert [event for event, _ in replay] == ["snapshot", "done"], replay
    assert replay[0][1]["task"]["status"] == "succeeded"
    assert replay[-1] == events[-1]
    assert "permission_denied" not in replay_stream.text


async def test_author_production_frame_keeps_original_refresh_hint(app, client, make_client, fake_llm, monkeypatch):
    """作者原有提示（「正文已保存，请刷新后查看」）仍在终态帧中，且任务快照的 error 携带 NOTE 说明。"""
    _, task, _, _, events = await _run_live(
        app, monkeypatch, "chapter", "owner", "production", client, make_client, fake_llm,
    )
    done = events[-1][1]
    assert done["detail"] == "正文已保存，请刷新后查看"
    assert "结果未发布" in done["task"]["error"]


# ---------------------------------------------------------------- 失权订阅者被拒


@pytest.mark.parametrize("change", ["revoked", "viewer"])
@pytest.mark.parametrize("kind", KINDS)
async def test_revoked_subscriber_never_receives_receipt_done(
    app, client, make_client, fake_llm, monkeypatch, kind, change,
):
    async def member_change(world):
        await _member_change(app, client, world, change)

    _, task, _, stream, events = await _run_live(
        app, monkeypatch, kind, "editor", "guarded", client, make_client, fake_llm, change=member_change,
    )
    await _assert_receipt_row(app, kind, task["id"])
    assert events[-1] == ("error", DENIED), events
    assert not any(event == "done" for event, _ in events)
    assert '"unpublished"' not in stream.text and NOTES[kind][1] not in stream.text


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("change", [None, "revoked", "viewer"])
async def test_held_receipt_done_is_rechecked_at_send(
    app, client, make_client, fake_llm, monkeypatch, kind, change,
):
    """done 帧已由引擎产出（订阅侧已核对），发送前撤权或降为 viewer：只能由发送时帧租约锁内复查拦截。

    降为 viewer 时成员仍为 accepted，作品级 `project_payload` 的本人项目校验照常通过；只有帧租约按订阅原绑定
    （活动态 editor）的复查与 `_bound_task_visible` 能拒绝，因此该参数单独证明帧租约复查承重。
    """
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *_a, **_k: None)
    hold = _Hold(lambda item: item[0] == "done")
    box: dict = {}

    async def released():
        await asyncio.wait_for(hold.reached.wait(), 20)
        await _settled(app, box["task_id"])
        if change is not None:
            await _member_change(app, client, box["world"], change)
        hold.proceed.set()

    def on_launch(world, task):
        box["world"], box["task_id"] = world, task["id"]
        box["releaser"] = asyncio.ensure_future(released())

    try:
        _, task, _, stream, events = await _run_live(
            app, monkeypatch, kind, "editor", "guarded", client, make_client, fake_llm, holds=[hold],
            on_launch=on_launch,
        )
    finally:
        hold.proceed.set()
        if "releaser" in box:
            await asyncio.gather(box["releaser"], return_exceptions=True)
    await _assert_receipt_row(app, kind, task["id"])
    assert hold.item is not None and hold.item[0] == "done"
    if change is not None:
        assert events[-1] == ("error", DENIED), events
        assert '"unpublished"' not in stream.text
    else:
        _assert_unpublished_done(kind, events[-1], task)
