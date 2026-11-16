"""作者订阅的异步投影不能丢失领取进度；正文快照与增量仍保持原来的同步边界。"""

import asyncio
from contextlib import aclosing
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.models import Task, User
from app.services import personal_projection, task_service
from app.services.task_engine.registry import get_handler
from tests.helpers import Gate, create_ai_config, create_project, register, wait_until
from tests.test_collaboration_stream_early_subscribe_e5e import _gate_build, _QuotaBlock


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


async def _queued(app, client, monkeypatch):
    actor = await register(client, "snapshot-owner")
    project = await create_project(client)
    await create_ai_config(client)
    response = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "快照", "content": ""})
    assert response.status_code == 201, response.text
    quota = _QuotaBlock(monkeypatch, app.state.task_engine)
    response = await client.post(f"/api/v1/chapters/{response.json()['id']}/generate", json={"auto_continue": False})
    assert response.status_code == 202, response.text
    task = response.json()
    assert task["status"] == "queued"
    async with app.state.sessionmaker() as db:
        token_version = await db.scalar(select(User.token_version).where(User.id == actor["id"]))
        row = await db.get(Task, task["id"])
        queued_at = row.queued_at
    stream = app.state.task_engine.subscribe(
        task["id"], statistics_key=task["task_key"], actor_id=actor["id"], token_version=token_version,
        active_grant_required=True, initial_project_id=project["id"], initial_queued_at=queued_at,
        initial_grant=None,
    )
    return actor, task, quota, stream, token_version


@pytest.mark.parametrize("wait_at", ["build", "text"])
@pytest.mark.parametrize("claim_at", ["projection", "final_read"])
async def test_owner_snapshot_includes_claim_published_during_projection(
    app, client, fake_llm, monkeypatch, wait_at, claim_at,
):
    _, task, quota, stream, _ = await _queued(app, client, monkeypatch)
    engine = app.state.task_engine
    build_reached, build_release = asyncio.Event(), asyncio.Event()
    text_gate = Gate()
    first, second = "快照前的文字。", "订阅后的文字。"
    fake_llm.push([first, text_gate, second])
    handler_type = type(get_handler("chapter.generate"))
    original_build = handler_type.build_job

    async def build(self, ctx, row, **options):
        if wait_at == "build":
            build_reached.set()
            await build_release.wait()
        return await original_build(self, ctx, row, **options)

    monkeypatch.setattr(handler_type, "build_job", build)
    original_out = task_service.out_json_live
    original_visible = engine._visible_task
    projected, claimed = False, False

    async def advance():
        nonlocal claimed
        claimed = True
        quota.release()
        if wait_at == "build":
            await asyncio.wait_for(build_reached.wait(), 5)
        else:
            await text_gate.wait_reached()
            await wait_until(lambda: engine.hubs[task["id"]].text == first)
        # 领取 progress 确已发布，但快照尚未准备完成，此时没有订阅者可接收。
        assert engine.hubs[task["id"]].status == "running"
        assert engine.hubs[task["id"]].subscriber_count == 0

    async def out(db, live_engine, row):
        nonlocal projected
        value = await original_out(db, live_engine, row)
        if row.id == task["id"] and value["status"] == "queued":
            projected = True
            if claim_at == "projection":
                await advance()
        return value

    async def visible(db, task_id, **options):
        row = await original_visible(db, task_id, **options)
        if projected and not claimed and claim_at == "final_read":
            assert row.status == "queued"
            # SELECT 已执行、结果返回给订阅协程之前，领取与广播完成。
            await advance()
        return row

    monkeypatch.setattr(task_service, "out_json_live", out)
    monkeypatch.setattr(engine, "_visible_task", visible)
    engine.heartbeat_seconds = 0.01
    try:
        async with aclosing(stream):
            event, snapshot = await asyncio.wait_for(anext(stream), 10)
            assert event == "snapshot"
            assert snapshot["task"]["status"] == "running"
            assert snapshot["task"]["stage"] == engine.hubs[task["id"]].stage
            assert snapshot["task"]["progress"] == engine.hubs[task["id"]].progress
            assert snapshot["task"]["queue_position"] is None
            if claim_at == "projection":
                assert snapshot["task"]["started_at"] is not None
            else:
                # 只有 hub 已知领取，原 SELECT 未读到领取时间；不能用本地时间补造。
                assert snapshot["task"]["started_at"] is None
            assert snapshot["task"]["task_key"] == task["task_key"]
            if wait_at == "build":
                assert snapshot["text"] == ""
                assert not build_release.is_set()
                assert await asyncio.wait_for(anext(stream), 2) == ("", {})
            else:
                assert snapshot["text"] == first
                text_gate.release()
                deltas = []
                while True:
                    event, data = await asyncio.wait_for(anext(stream), 10)
                    if event == "delta":
                        deltas.append(data["text"])
                    if event in ("done", "error"):
                        assert event == "done", data
                        break
                assert snapshot["text"] + "".join(deltas) == first + second
    finally:
        build_release.set()
        text_gate.release()
        quota.release()
        await wait_until(lambda: task["id"] not in engine._running)
    assert engine.hubs.get(task["id"]) is None or engine.hubs[task["id"]].subscriber_count == 0


@pytest.mark.parametrize("changed", ["statistics_key", "queued_at", "execution_key"])
async def test_owner_snapshot_does_not_mix_other_hub_identity(app, client, fake_llm, monkeypatch, changed):
    _, task, quota, stream, _ = await _queued(app, client, monkeypatch)
    engine = app.state.task_engine
    reached, release = _gate_build(monkeypatch, "chapter")
    fake_llm.push("完成隔离任务")
    original_out = task_service.out_json_live
    saved = None

    async def out(db, live_engine, row):
        nonlocal saved
        value = await original_out(db, live_engine, row)
        quota.release()
        await asyncio.wait_for(reached.wait(), 5)
        hub = engine.hubs[task["id"]]
        saved = (getattr(hub, changed), hub.stage, hub.progress)
        setattr(hub, changed, hub.queued_at + timedelta(seconds=1) if changed == "queued_at" else str(uuid4()))
        hub.stage, hub.progress = "另一轮次的阶段", 97
        return value

    monkeypatch.setattr(task_service, "out_json_live", out)
    try:
        async with aclosing(stream):
            event, snapshot = await asyncio.wait_for(anext(stream), 10)
            assert event == "snapshot"
            assert snapshot["task"]["task_key"] == task["task_key"]
            assert snapshot["task"]["status"] == "running"
            assert snapshot["task"]["stage"] == task_service.STAGE_LABELS["running"]
            assert snapshot["task"]["progress"] == 0
    finally:
        if saved is not None:
            hub = engine.hubs[task["id"]]
            setattr(hub, changed, saved[0])
            hub.stage, hub.progress = saved[1:]
        release.set()
        quota.release()
        await wait_until(lambda: task["id"] not in engine._running)


@pytest.mark.parametrize("revoke_at", ["before_register", "before_send"])
async def test_owner_snapshot_rechecks_original_auth_and_cleans_subscription(
    app, client, fake_llm, monkeypatch, revoke_at,
):
    actor, task, quota, stream, token_version = await _queued(app, client, monkeypatch)
    engine = app.state.task_engine
    original_out = task_service.out_json_live
    original_project = personal_projection.project_payload
    projected = False

    async def revoke():
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == actor["id"]).values(token_version=token_version + 1))
            await db.commit()

    async def out(db, live_engine, row):
        nonlocal projected
        value = await original_out(db, live_engine, row)
        projected = True
        if revoke_at == "before_register":
            await revoke()
        return value

    async def project(db, user_id, project_id, payload):
        value = await original_project(db, user_id, project_id, payload)
        if projected and revoke_at == "before_send":
            assert engine.hubs[task["id"]].subscriber_count == 1
            await revoke()
        return value

    monkeypatch.setattr(task_service, "out_json_live", out)
    monkeypatch.setattr(personal_projection, "project_payload", project)
    try:
        async with aclosing(stream):
            event, data = await asyncio.wait_for(anext(stream), 10)
            assert event == "error"
            assert data["task"] is None
            assert data["code"] == "permission_denied"
        assert engine.hubs[task["id"]].subscriber_count == 0
    finally:
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == actor["id"]).values(token_version=token_version))
            await db.commit()
        fake_llm.push("清理测试任务")
        quota.release()
        await wait_until(lambda: task["id"] not in engine._running)
