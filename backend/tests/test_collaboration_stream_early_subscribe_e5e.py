"""E5-e：受控任务流的早订阅——排队中、领取瞬间、首个已核验事件之前开始的订阅。

真实引擎执行（fake_llm + Gate/Event），经受控 `/_test` 路由（engine.subscribe(guarded=True) + task_sse_response）
与本机 TCP 实际发送；不替换中央帧校验器。
生产路由自 E5-f 起对协作者挂载同一受控管线（tasks.py），此处 /_test 路由便于插入持帧点，作者生产订阅路径不变。

三个时点：
- 排队中：快照来自同一持锁排队行（TaskViewEvidence），排队位次帧来源是 hub 登记的 QueuedSource；
- 领取瞬间：领取 CAS 已提交、领取者尚未登记 ClaimSource 时订阅——订阅侧在租约外有上限地等待该来源；
- 首个事件前：领取后、首个经 verify_event 核验的事件之前（build_job / 等待模型首个 token），hub 来源是 ClaimSource，
  只证明不含模型输出的帧（领取 progress、正文为空的快照），首个已核验来源发布后才有 delta。

撤权矩阵采用「引擎已产出带证明的帧、发送前改变授权」：持住一帧再改变，证明拒绝来自发送时的帧租约锁内复查。
"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.models import ProjectCollaborator, Task
from app.services import quota_service, task_service
from app.services.ai_dispatch import read_visibility_stamp
from app.services.task_engine import stream_evidence
from app.services.task_engine.hub import TaskHub
from app.services.task_engine.registry import get_handler
from tests.helpers import analysis_json, create_ai_config, create_project, register, task_params, wait_until
from tests.test_collaboration_stream_replay_e5b import DENIED, _send
from tests.test_collaboration_stream_running_e5c1 import _Hold, _mount, _serve, _settled, _Stream, _task_row
from tests.test_task_sse_scope import _binding

PASSWORD, NEW_PASSWORD = "secret123", "secret456new"
GUEST = "e5e-guest"
KINDS = ("chapter", "analysis", "batch", "wizard", "outline", "entity")
TASK_KINDS = {"chapter": "chapter.generate", "analysis": "chapter.analyze", "batch": "chapter.batch",
              "wizard": "wizard.outline", "outline": "outline.continue", "entity": "character.generate"}
ANSWERS = {
    "chapter": "早订阅章节正文",
    "analysis": analysis_json(summary="早订阅分析概要"),
    "batch": "早订阅批量正文",
    "wizard": json.dumps([{"title": "早订阅章纲", "summary": "开篇"}], ensure_ascii=False),
    "outline": json.dumps({"outlines": [{"title": "早订阅续章", "summary": "启程"}]}, ensure_ascii=False),
    "entity": json.dumps({"character": {"name": "早订阅角色", "profile": "剑修"}, "relationships": []},
                         ensure_ascii=False),
}
CHANGES = ("revoke", "viewer", "owner_ai", "password", "reinvite")


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


class World:
    def __init__(self, project, chapter, batch_chapter, owner, guest, guest_client):
        self.project, self.chapter, self.batch_chapter = project, chapter, batch_chapter
        self.owner, self.guest, self.guest_client = owner, guest, guest_client

    @property
    def base(self) -> str:
        return f"/api/v1/projects/{self.project['id']}"


async def _world(owner, guest, *, editor_model=True, allow_owner_ai=True) -> World:
    owner_user = await register(owner, "e5e-owner")
    guest_user = await register(guest, GUEST)
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    chapter = await owner.post(f"{base}/chapters", json={"title": "第一章", "content": "原正文内容"})
    assert chapter.status_code == 201, chapter.text
    batch_chapter = await owner.post(f"{base}/chapters", json={"title": "第二章", "content": ""})
    assert batch_chapter.status_code == 201, batch_chapter.text
    outline = await owner.post(f"{base}/outlines", json={"title": "第一章纲", "summary": "主角离家"})
    assert outline.status_code == 201, outline.text
    for name in ("甲", "乙"):
        created = await owner.post(f"{base}/characters", json={"name": name})
        assert created.status_code == 201, created.text
    await create_ai_config(owner, name="作者模型", model="owner-model", api_key="sk-owner-e5e-0001")
    if editor_model:
        await create_ai_config(guest, name="本人模型", model="editor-model", api_key="sk-editor-e5e-0002")
    invite = await owner.post(f"{base}/collaborators", json={
        "username": GUEST, "role": "editor", "allow_owner_ai": allow_owner_ai,
    })
    assert invite.status_code == 201, invite.text
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": invite.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return World(project, chapter.json(), batch_chapter.json(), owner_user, guest_user, guest)


def _launch(client, world, kind):
    if kind == "chapter":
        return client.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    if kind == "analysis":
        return client.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
    if kind == "batch":
        chapter_id = world.batch_chapter["id"]
        return client.post(f"{world.base}/batch-generate", json={
            "start_chapter_id": chapter_id, "end_chapter_id": chapter_id, "auto_continue": False,
        })
    if kind == "wizard":
        return client.post(f"{world.base}/wizard/outline", json={})
    if kind == "outline":
        return client.post(f"{world.base}/outlines/continue", json={"count": 1})
    return client.post(f"{world.base}/characters/generate", json={})


async def _start(fake_llm, client, world, kind, chunks=None, *, push=True) -> dict:
    if push:
        fake_llm.push(chunks if chunks is not None else ANSWERS[kind])
    created = await _launch(client, world, kind)
    assert created.status_code == 202, created.text
    return created.json()


async def _member(app, world):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"], ProjectCollaborator.user_id == world.guest["id"],
        ))


async def _apply(app, owner, world, change):
    if change == "password":
        response = await world.guest_client.post("/api/v1/auth/change-password", json={
            "old_password": PASSWORD, "new_password": NEW_PASSWORD,
        })
        assert response.status_code == 204, response.text
        return
    member = await _member(app, world)
    url = f"{world.base}/collaborators/{world.guest['id']}"
    if change == "owner_ai_direct":
        # 直接关闭模型授权、不递增成员轮次：只能靠帧租约锁内直接核对 allow_owner_ai。
        async with app.state.sessionmaker() as db:
            await db.execute(update(ProjectCollaborator).where(ProjectCollaborator.id == member.id).values(
                allow_owner_ai=False,
            ))
            await db.commit()
        return
    if change == "viewer":
        response = await owner.patch(url, json={"expected_revision": member.revision, "role": "viewer"})
        assert response.status_code == 200, response.text
        return
    if change == "owner_ai":
        response = await owner.patch(url, json={"expected_revision": member.revision, "allow_owner_ai": False})
        assert response.status_code == 200, response.text
        return
    response = await owner.delete(url, params={"expected_revision": member.revision})
    assert response.status_code == 204, response.text
    if change == "reinvite":
        invite = await owner.post(f"{world.base}/collaborators", json={
            "username": GUEST, "role": "editor", "allow_owner_ai": member.allow_owner_ai,
        })
        assert invite.status_code == 201, invite.text
        accepted = await world.guest_client.post(
            f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
            json={"expected_revision": invite.json()["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
    else:
        assert change == "revoke", change


class _QuotaBlock:
    """把任务并发额度暂时视为 0：任务留在排队中（不失败、不取消）；release 后由 notify 照常领取。"""

    def __init__(self, monkeypatch, engine):
        self.blocked, self.engine = True, engine
        original = quota_service.task_limits

        async def limits(db, user_ids, settings):
            result = await original(db, user_ids, settings)
            return dict.fromkeys(result, 0) if self.blocked else result

        monkeypatch.setattr(quota_service, "task_limits", limits)

    def release(self) -> None:
        if self.blocked:
            self.blocked = False
            self.engine.notify()


def _gate_build(monkeypatch, kind):
    """在 build_job 完成后、首个事件核验之前停住运行体（hub 来源仍是领取来源）。"""
    reached, release = asyncio.Event(), asyncio.Event()
    handler_type = type(get_handler(TASK_KINDS[kind]))
    original = handler_type.build_job

    async def build_job(self, ctx, task, **options):
        job = await original(self, ctx, task, **options)
        if not reached.is_set():
            reached.set()
            await release.wait()
        return job

    monkeypatch.setattr(handler_type, "build_job", build_job)
    return reached, release


def _gate_claim_commit(monkeypatch, engine):
    """领取 CAS 已提交、会话已退出，而领取者尚未登记领取来源（_claim_and_run 退出外层临界段后）时停住。"""
    reached, release = asyncio.Event(), asyncio.Event()
    original = engine.runtime.hold

    @asynccontextmanager
    async def hold():
        async with original():
            yield
        current = asyncio.current_task()
        if not reached.is_set() and current is not None and current.get_name().startswith("task-"):
            reached.set()
            await release.wait()

    monkeypatch.setattr(engine.runtime, "hold", hold)
    return reached, release


def _spy_claim_wait(monkeypatch, engine):
    waiting = asyncio.Event()
    original = engine._await_claim_source

    async def spy(hub):
        waiting.set()
        return await original(hub)

    monkeypatch.setattr(engine, "_await_claim_source", spy)
    return waiting


def _source_is(kind):
    return lambda item: type(getattr(item[2], "source", None)) is kind


def _deltas(events) -> str:
    return "".join(data["text"] for event, data in events if event == "delta")


def _assert_done(events, stream, kind):
    names = [event for event, _ in events]
    assert stream.status == 200, stream.text
    assert names[0] == "snapshot" and names[-1] == "done", events
    assert "permission_denied" not in stream.text
    # 拼接的实时正文恰为模型输出（首个已核验事件之前没有任何正文帧）。
    assert _deltas(events) == ANSWERS[kind], events
    assert events[-1][1]["task"]["status"] == "succeeded"


def _client_for(actor, client, guest):
    return guest if actor == "editor" else client


# ---------------------------------------------------------------- 正例：排队订阅（含领取帧）


@pytest.mark.parametrize("actor", ["editor", "owner"])
@pytest.mark.parametrize("kind", KINDS)
async def test_queued_subscription_follows_claim_to_done(app, client, make_client, fake_llm, monkeypatch, kind, actor):
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    block = _QuotaBlock(monkeypatch, engine)
    _mount(app)
    starter = _client_for(actor, client, guest)
    task = await _start(fake_llm, starter, world, kind)
    assert (await _task_row(app, task["id"])).status == "queued"
    async with _serve(app) as base:
        stream = _Stream(base, starter, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            first = stream.events()[0][1]
            assert first["task"]["status"] == "queued" and first["text"] == ""
            block.release()
            events = await stream.finish(within=30)
        finally:
            block.release()
            await stream.aclose()
    _assert_done(events, stream, kind)
    # 领取瞬间的帧（排队位次清空、status=running）经领取来源证明后发出。
    progress = [data for event, data in events if event == "progress"]
    assert any(data["status"] == "running" for data in progress), events
    assert (await _settled(app, task["id"])).status == "succeeded"


@pytest.mark.parametrize("actor", ["editor", "owner"])
async def test_queued_position_frames_pass_with_queued_source(app, client, make_client, fake_llm, monkeypatch, actor):
    """排队位次变化的 progress 帧来源是 hub 登记的 QueuedSource；前一个排队任务取消后位次推进照常发出。"""
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    block = _QuotaBlock(monkeypatch, engine)
    _mount(app)
    starter = _client_for(actor, client, guest)
    # 前一个排队任务会在执行前取消：不为它准备模型输出。
    ahead = await _start(fake_llm, starter, world, "entity", push=False)
    task = await _start(fake_llm, starter, world, "chapter")
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.queue_position == 1)
    assert type(engine.hubs[task["id"]].source) is stream_evidence.QueuedSource
    async with _serve(app) as base:
        stream = _Stream(base, starter, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            cancelled = await starter.post(f"/api/v1/tasks/{ahead['id']}/cancel", params=task_params(ahead))
            assert cancelled.status_code == 200, cancelled.text
            await stream.wait_for(lambda events: any(
                event == "progress" and data.get("queue_position") == 0 for event, data in events))
            assert (await _task_row(app, task["id"])).status == "queued"
            block.release()
            events = await stream.finish(within=30)
        finally:
            block.release()
            await stream.aclose()
    _assert_done(events, stream, "chapter")


# ---------------------------------------------------------------- 正例：领取瞬间订阅


@pytest.mark.parametrize("actor", ["editor", "owner"])
@pytest.mark.parametrize("kind", KINDS)
async def test_subscription_in_claim_commit_window_waits_for_claim_source(
    app, client, make_client, fake_llm, monkeypatch, kind, actor,
):
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    reached, release = _gate_claim_commit(monkeypatch, engine)
    waiting = _spy_claim_wait(monkeypatch, engine)
    _mount(app)
    starter = _client_for(actor, client, guest)
    task = await _start(fake_llm, starter, world, kind)
    try:
        await asyncio.wait_for(reached.wait(), 10)
        row = await _task_row(app, task["id"])
        # 领取已提交（running + 执行键），hub 尚无领取来源。
        assert row.status == "running" and row.execution_key is not None
        hub = engine.hubs[task["id"]]
        assert type(hub.source) is not stream_evidence.ClaimSource and hub.text == ""
        async with _serve(app) as base:
            stream = _Stream(base, starter, task)
            try:
                await asyncio.wait_for(waiting.wait(), 10)
                assert stream.events() == []
                release.set()
                events = await stream.finish(within=30)
            finally:
                release.set()
                await stream.aclose()
    finally:
        release.set()
    _assert_done(events, stream, kind)
    snapshot = events[0][1]
    assert snapshot["text"] == "" and snapshot["task"]["status"] == "running"
    assert (await _settled(app, task["id"])).status == "succeeded"


# ---------------------------------------------------------------- 正例：首个已核验事件之前订阅


@pytest.mark.parametrize("actor", ["editor", "owner"])
@pytest.mark.parametrize("kind", KINDS)
async def test_subscription_before_first_verified_event_gets_empty_snapshot(
    app, client, make_client, fake_llm, monkeypatch, kind, actor,
):
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    reached, release = _gate_build(monkeypatch, kind)
    _mount(app)
    starter = _client_for(actor, client, guest)
    task = await _start(fake_llm, starter, world, kind)
    try:
        await asyncio.wait_for(reached.wait(), 10)
        hub = engine.hubs[task["id"]]
        assert type(hub.source) is stream_evidence.ClaimSource and hub.text == ""
        async with _serve(app) as base:
            stream = _Stream(base, starter, task)
            try:
                await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                assert len(stream.events()) == 1
                snapshot = stream.events()[0][1]
                assert snapshot["text"] == "" and snapshot["task"]["status"] == "running"
                release.set()
                events = await stream.finish(within=30)
            finally:
                release.set()
                await stream.aclose()
    finally:
        release.set()
    _assert_done(events, stream, kind)
    assert (await _settled(app, task["id"])).status == "succeeded"


async def test_late_queued_snapshot_sent_after_claim_commit_is_proven(app, client, make_client, fake_llm, monkeypatch):
    """排队快照已投影、实际发送前恰逢同一轮次被领取：按不变字段 + 排队形状证明，之后领取帧与正文照常。"""
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    block = _QuotaBlock(monkeypatch, engine)
    reached, release = _gate_build(monkeypatch, "chapter")
    hold = _Hold(lambda item: item[0] == "snapshot")
    _mount(app, [hold])
    task = await _start(fake_llm, guest, world, "chapter")
    try:
        async with _serve(app) as base:
            stream = _Stream(base, guest, task)
            try:
                await asyncio.wait_for(hold.reached.wait(), 10)
                assert hold.item[1]["task"]["status"] == "queued"
                block.release()
                await asyncio.wait_for(reached.wait(), 10)
                assert (await _task_row(app, task["id"])).status == "running"
                hold.proceed.set()
                await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                release.set()
                events = await stream.finish(within=30)
            finally:
                hold.proceed.set()
                release.set()
                await stream.aclose()
    finally:
        block.release()
        release.set()
    _assert_done(events, stream, "chapter")
    assert events[0][1]["task"]["status"] == "queued"


# ---------------------------------------------------------------- 撤权矩阵（全量参数化）


async def _prepare_time_point(app, world, fake_llm, monkeypatch, time, *, actor_client):
    """把任务推进到所需时点并返回 (task, hold, gates)。hold 持住该时点引擎已产出、尚未发送的一帧。"""
    engine = app.state.task_engine
    block = _QuotaBlock(monkeypatch, engine) if time in ("queued", "claim") else None
    build_reached, build_release = _gate_build(monkeypatch, "chapter")
    if time == "queued":
        hold = _Hold(lambda item: item[0] == "progress" and _source_is(stream_evidence.QueuedSource)(item))
    elif time == "claim":
        hold = _Hold(lambda item: item[0] == "progress" and _source_is(stream_evidence.ClaimSource)(item))
    else:
        hold = _Hold(lambda item: item[0] == "snapshot" and _source_is(stream_evidence.ClaimSource)(item))
    _mount(app, [hold])
    ahead = await _start(fake_llm, actor_client, world, "entity", push=False) if time == "queued" else None
    task = await _start(fake_llm, actor_client, world, "chapter")
    if time == "queued":
        await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.queue_position == 1)
    if time == "pre_event":
        await asyncio.wait_for(build_reached.wait(), 10)
    return task, hold, block, ahead, (build_reached, build_release)


async def _reach_hold(app, stream, actor_client, time, hold, block, ahead, build):
    if time == "queued":
        await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
        cancelled = await actor_client.post(f"/api/v1/tasks/{ahead['id']}/cancel", params=task_params(ahead))
        assert cancelled.status_code == 200, cancelled.text
    elif time == "claim":
        await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
        block.release()
        await asyncio.wait_for(build[0].wait(), 10)
    await asyncio.wait_for(hold.reached.wait(), 10)


@pytest.mark.parametrize("notify", ["notify", "silent"])
@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("time", ["queued", "claim", "pre_event"])
async def test_early_frame_produced_before_change_is_denied_at_send(
    app, client, make_client, fake_llm, monkeypatch, time, change, notify,
):
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    task, hold, block, ahead, build = await _prepare_time_point(
        app, world, fake_llm, monkeypatch, time, actor_client=guest,
    )
    if notify == "silent":
        # 无通知：不唤醒订阅、不提前停止运行体；只有发送时的锁内复查能拦截。
        monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    try:
        async with _serve(app) as base:
            stream = _Stream(base, guest, task)
            try:
                await _reach_hold(app, stream, guest, time, hold, block, ahead, build)
                before = stream.events()
                await _apply(app, client, world, change)
                if notify == "notify" and change == "password":
                    engine.notify_collaboration_change(world.project["id"], world.guest["id"])
                if notify == "silent":
                    row = await _task_row(app, task["id"])
                    assert row.status == ("queued" if time == "queued" else "running"), row.status
                hold.proceed.set()
                events = await stream.finish()
                if notify == "silent":
                    # 拒绝来自发送侧：任务仍处于原状态（排队 / 运行体阻塞在 build 之后）。
                    row = await _task_row(app, task["id"])
                    assert row.status == ("queued" if time == "queued" else "running"), row.status
            finally:
                hold.proceed.set()
                build[1].set()
                await stream.aclose()
    finally:
        if block is not None:
            block.release()
        build[1].set()
    assert stream.status == 200
    assert events == [*before, ("error", DENIED)], events
    assert [event for event, _ in before] == ([] if time == "pre_event" else ["snapshot"])
    assert ANSWERS["chapter"] not in stream.text and '"result"' not in stream.text
    await _settled(app, task["id"])


@pytest.mark.parametrize("time", ["queued", "claim", "pre_event"])
async def test_held_early_frame_is_sent_when_nothing_changes(app, client, make_client, fake_llm, monkeypatch, time):
    """正对照：同样持住各时点的帧再发送，未改变授权时照常发出并到 done。"""
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    task, hold, block, ahead, build = await _prepare_time_point(
        app, world, fake_llm, monkeypatch, time, actor_client=guest,
    )
    try:
        async with _serve(app) as base:
            stream = _Stream(base, guest, task)
            try:
                await _reach_hold(app, stream, guest, time, hold, block, ahead, build)
                hold.proceed.set()
                await stream.wait_for(lambda events: len(events) >= (1 if time == "pre_event" else 2))
                if block is not None:
                    block.release()
                build[1].set()
                events = await stream.finish(within=30)
            finally:
                hold.proceed.set()
                build[1].set()
                await stream.aclose()
    finally:
        if block is not None:
            block.release()
        build[1].set()
    _assert_done(events, stream, "chapter")


# ---------------------------------------------------------------- 承重：锁内授权复查与领取行身份


@pytest.mark.parametrize("changed", [True, False])
@pytest.mark.parametrize("time", ["queued", "claim", "pre_event"])
async def test_owner_model_grant_rechecked_in_lease_without_revision(
    app, client, make_client, fake_llm, monkeypatch, time, changed,
):
    """协作者无本人模型（绑定作者模型），直接关闭 allow_owner_ai（不递增成员轮次、无通知）：
    只有帧租约锁内对作者模型授权的直接核对能拦截；未关闭时照常发送（正对照）。"""
    guest = make_client()
    world = await _world(client, guest, editor_model=False)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    task, hold, block, ahead, build = await _prepare_time_point(
        app, world, fake_llm, monkeypatch, time, actor_client=guest,
    )
    try:
        async with _serve(app) as base:
            stream = _Stream(base, guest, task)
            try:
                await _reach_hold(app, stream, guest, time, hold, block, ahead, build)
                before = stream.events()
                if changed:
                    await _apply(app, client, world, "owner_ai_direct")
                hold.proceed.set()
                if changed:
                    events = await stream.finish()
                else:
                    await stream.wait_for(lambda events: len(events) > len(before))
                    if block is not None:
                        block.release()
                    build[1].set()
                    events = await stream.finish(within=30)
            finally:
                hold.proceed.set()
                build[1].set()
                await stream.aclose()
    finally:
        if block is not None:
            block.release()
        build[1].set()
    if changed:
        assert events == [*before, ("error", DENIED)], events
        assert ANSWERS["chapter"] not in stream.text
    else:
        _assert_done(events, stream, "chapter")
    await _settled(app, task["id"])


async def _claim_state(app, client, make_client, fake_llm, monkeypatch):
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    reached, release = _gate_build(monkeypatch, "chapter")
    task = await _start(fake_llm, guest, world, "chapter")
    await asyncio.wait_for(reached.wait(), 10)
    hub = engine.hubs[task["id"]]
    source = hub.source
    assert type(source) is stream_evidence.ClaimSource
    binding = await _binding(app, world.guest["id"], task["id"], task["task_key"])
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, world.project["id"])
        row = await db.get(Task, task["id"])
        out = await task_service.out_json_live(db, engine, row)
    return world, task, hub, source, binding, stamp, out, release


async def test_claim_source_must_match_the_claimed_row(app, client, make_client, fake_llm, monkeypatch):
    """领取来源只认本次领取提交的那一行：换执行键或换输入（伪造另一领取）即拒绝；原来源可过（正对照）。"""
    world, task, hub, source, binding, stamp, _out, release = await _claim_state(
        app, client, make_client, fake_llm, monkeypatch,
    )
    try:
        data = {"status": "running", "progress": hub.progress, "stage": hub.stage}
        original = stream_evidence.frame_evidence("progress", data, source, stamp)
        assert await _send(app, binding, [("progress", data, original)]) == [("progress", data)]
        forged_inputs = (
            replace(source.execution, execution_key=str(uuid4())),
            replace(source.execution, input_json=json.dumps({"request": {"伪造": True}})),
            replace(source.execution, queued_at=source.execution.queued_at + timedelta(seconds=1)),
        )
        for forged_execution in forged_inputs:
            fake_hub = TaskHub(task["id"], statistics_key=task["task_key"], status="running",
                               queued_at=forged_execution.queued_at, execution_key=forged_execution.execution_key)
            forged = stream_evidence.issue_claim(forged_execution, hub=fake_hub)
            evidence = stream_evidence.frame_evidence("progress", data, forged, stamp)
            assert await _send(app, binding, [("progress", data, evidence)]) == [("error", DENIED)]
        # 未登记的领取来源（不经 issue_claim）不能使用。
        bare = stream_evidence.ClaimSource(source.execution)
        evidence = stream_evidence.frame_evidence("progress", data, bare, stamp)
        assert await _send(app, binding, [("progress", data, evidence)]) == [("error", DENIED)]
    finally:
        release.set()
    assert (await _settled(app, task["id"])).status == "succeeded"


async def test_claim_source_cannot_carry_model_output(app, client, make_client, fake_llm, monkeypatch):
    """领取来源不证明任何正文：delta、带正文的快照、篡改摘要、多键 progress、done 均拒绝；不得以持锁行补签。"""
    world, task, hub, source, binding, stamp, out, release = await _claim_state(
        app, client, make_client, fake_llm, monkeypatch,
    )
    try:
        good = {"task": out, "text": ""}
        evidence = stream_evidence.frame_evidence("snapshot", good, source, stamp)
        assert await _send(app, binding, [("snapshot", good, evidence)]) == [("snapshot", good)]
        bad_frames = [
            ("delta", {"text": "伪造正文"}),
            ("snapshot", {"task": out, "text": "伪造正文"}),
            ("snapshot", {"task": {**out, "title": "篡改标题"}, "text": ""}),
            ("snapshot", {"task": {**out, "error": "伪造错误"}, "text": ""}),
            ("progress", {"status": "running", "progress": 0, "stage": "", "text": "伪造"}),
            ("progress", {"status": "succeeded", "progress": 100, "stage": ""}),
            ("progress", {"status": "queued", "progress": 0, "stage": "", "queue_position": 3}),
            ("reset", {"text": "伪造"}),
            ("done", {"task": out, "result": {"text": "伪造"}}),
            ("continue", {"round": 1, "words": 1, "target": 2}),
        ]
        for event, data in bad_frames:
            evidence = stream_evidence.frame_evidence(event, data, source, stamp)
            assert await _send(app, binding, [(event, data, evidence)]) == [("error", DENIED)], event
    finally:
        release.set()
    assert (await _settled(app, task["id"])).status == "succeeded"


async def test_same_execution_advancing_only_waits_for_this_execution(app, client, make_client, fake_llm, monkeypatch):
    """「退出租约等终态证明」只认本次领取的执行：仅移除本执行输入中的占用指针、或本执行已登记管理员取消意图。

    另一执行键 / 入队轮次 / UUID / actor / 类型、输入另有改动或行已非 running 一律不等（由调用方立即拒绝），
    逐字匹配领取身份的行也不属于「推进」。复核补充（2026-11-13）：原用例的伪造来源同时改了输入，
    执行键这一条件本身不承重。
    """
    world, task, _hub, source, _binding, _stamp, _out, release = await _claim_state(
        app, client, make_client, fake_llm, monkeypatch,
    )
    try:
        execution = source.execution
        row = await _task_row(app, task["id"])
        assert "_chapter_claim" in row.input and row.admin_cancel_operation_id is None
        advancing = stream_evidence._same_execution_advancing
        assert advancing(execution, row) is False
        saved_input = {key: value for key, value in row.input.items() if key != "_chapter_claim"}

        def variant(**changes):
            detached = stream_evidence._claim_time_task(row, execution)
            for key, value in changes.items():
                setattr(detached, key, value)
            return detached

        assert advancing(execution, variant(input=saved_input)) is True
        assert advancing(execution, variant(admin_cancel_operation_id=str(uuid4()))) is True
        others = (
            {"execution_key": str(uuid4())}, {"queued_at": row.queued_at + timedelta(seconds=1)},
            {"statistics_key": str(uuid4())}, {"user_id": world.owner["id"]}, {"kind": "chapter.polish"},
            {"status": "succeeded"}, {"status": "queued"},
        )
        for changes in others:
            assert advancing(execution, variant(input=saved_input, **changes)) is False, changes
            assert advancing(execution, variant(admin_cancel_operation_id=str(uuid4()), **changes)) is False, changes
        assert advancing(execution, variant(input={**saved_input, "伪造": True})) is False
    finally:
        release.set()
    assert (await _settled(app, task["id"])).status == "succeeded"


async def test_queued_source_only_proves_same_round_position(app, client, make_client, fake_llm, monkeypatch):
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    block = _QuotaBlock(monkeypatch, engine)
    task = await _start(fake_llm, guest, world, "chapter")
    try:
        await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None
                         and type(hub.source) is stream_evidence.QueuedSource)
        hub = engine.hubs[task["id"]]
        source = hub.source
        binding = await _binding(app, world.guest["id"], task["id"], task["task_key"])
        async with app.state.sessionmaker() as db:
            stamp = await read_visibility_stamp(db, world.project["id"])
        data = {"status": "queued", "progress": 0, "stage": hub.stage, "queue_position": 0}
        evidence = stream_evidence.frame_evidence("progress", data, source, stamp)
        assert await _send(app, binding, [("progress", data, evidence)]) == [("progress", data)]
        other_round = TaskHub(task["id"], statistics_key=task["task_key"], status="queued",
                              queued_at=hub.queued_at + timedelta(seconds=1))
        forged = stream_evidence.issue_queued(other_round)
        for event, payload, frame_source in (
            ("progress", data, forged),
            ("progress", data, stream_evidence.QueuedSource(source.task_id, source.task_key, source.queued_at)),
            ("snapshot", {"task": None, "text": ""}, source),
            ("delta", {"text": "伪造"}, source),
            ("progress", {**data, "status": "running"}, source),
        ):
            evidence = stream_evidence.frame_evidence(event, payload, frame_source, stamp)
            assert await _send(app, binding, [(event, payload, evidence)]) == [("error", DENIED)], event
    finally:
        block.release()
    assert (await _settled(app, task["id"])).status == "succeeded"


async def test_claim_source_not_issued_for_unproven_kinds(app):
    """未实现 stream_guard 的类型（知识库、拆书）领取后仍是原执行身份，受控流继续失败关闭；agent.turn 已开放。"""
    from app.services.task_engine.engine import TaskEngine

    for kind in ("kb.index", "book.import"):
        assert getattr(get_handler(kind), "stream_guard", None) is None
        execution = type("E", (), {"kind": kind})()
        assert TaskEngine._claim_source(execution, None) is execution
    for kind in (*TASK_KINDS.values(), "agent.turn"):
        assert callable(getattr(get_handler(kind), "stream_guard", None))


async def test_claim_frame_in_save_to_finalize_window_waits_for_terminal_proof(
    app, client, make_client, fake_llm, monkeypatch,
):
    """单章保存事务已提交（输入中的占用指针已移除）、终态尚未提交时发送的领取帧：持锁行不再逐字匹配领取身份，
    但仍是同一执行在运行——不得立即拒绝（会误拒），也不放宽匹配；退出租约等原 hub 的终态证明后按终态分支发出。"""
    from app.services.task_engine.engine import TaskEngine

    guest = make_client()
    world = await _world(client, guest)
    build_reached, build_release = _gate_build(monkeypatch, "chapter")
    finalize_reached, finalize_release = asyncio.Event(), asyncio.Event()
    original_complete = TaskEngine.complete_state

    async def gated_complete(self, task, **kwargs):
        if kwargs.get("status") == "succeeded" and not finalize_reached.is_set():
            finalize_reached.set()
            await finalize_release.wait()
        return await original_complete(self, task, **kwargs)

    monkeypatch.setattr(TaskEngine, "complete_state", gated_complete)
    advancing = []
    original_advancing = stream_evidence._same_execution_advancing

    def spy(execution, task):
        result = original_advancing(execution, task)
        advancing.append(result)
        return result

    monkeypatch.setattr(stream_evidence, "_same_execution_advancing", spy)
    hold = _Hold(lambda item: item[0] == "snapshot" and _source_is(stream_evidence.ClaimSource)(item))
    _mount(app, [hold])
    task = await _start(fake_llm, guest, world, "chapter")
    try:
        await asyncio.wait_for(build_reached.wait(), 10)
        async with _serve(app) as base:
            stream = _Stream(base, guest, task)
            try:
                await asyncio.wait_for(hold.reached.wait(), 10)
                build_release.set()
                # 保存已提交、done 已核验，终态 CAS 尚未执行：行仍 running，输入中的占用指针已移除。
                await asyncio.wait_for(finalize_reached.wait(), 10)
                row = await _task_row(app, task["id"])
                assert row.status == "running" and "_chapter_claim" not in row.input
                hold.proceed.set()
                await wait_until(lambda: True in advancing, within=10)
                assert stream.events() == []
                finalize_release.set()
                events = await stream.finish(within=30)
            finally:
                hold.proceed.set()
                build_release.set()
                finalize_release.set()
                await stream.aclose()
    finally:
        build_release.set()
        finalize_release.set()
    _assert_done(events, stream, "chapter")
    assert events[0][1]["text"] == "" and events[0][1]["task"]["status"] == "running"
