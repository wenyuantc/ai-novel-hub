"""C4 的两应用读取契约：持久进度、真实执行轮次及待核对事实。"""

import asyncio
import time
from contextlib import aclosing
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, update

from app.models import Task, TaskWorkRecord
from app.services.task_engine.registry import get_handler
from tests.conftest import _new_client
from tests.helpers import Gate, register, task_params, wait_task, wait_until
from tests.rbac_helpers import task_quota_used
from tests.schema_helpers import isolated_worker_database
from tests.test_tasks import _generate, _setup


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


async def _read(client, path, params=None):
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _wait_output(client, path, params, text, deadline):
    while True:
        if (await _read(client, path, params))["text"] == text:
            return time.monotonic()
        assert time.monotonic() < deadline, "执行者静默后另一应用未见持久输出"
        await asyncio.sleep(0.02)


async def test_idle_text_then_stage_from_real_runner_across_apps(
    app, second_app, client, system_admin, fake_llm, monkeypatch, record_property,
):
    _, _, (chapter,) = await _setup(client)
    source, destination = app.state.task_engine, second_app.state.task_engine
    owner_id = (await _read(client, "/api/v1/auth/me"))["id"]
    handler = get_handler("chapter.generate")
    run = handler.run
    stage_ready, stage_sent, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    clear_stage, cleared = asyncio.Event(), asyncio.Event()
    last_token = Gate()
    private_stage = "调用私有插件及其未公开的名称" * 12
    expected_stage = private_stage[:100]
    text = "海边的脚印。" * 8

    async def progress_only(job, ctx):
        async with aclosing(run(job, ctx)) as stream:
            async for name, data in stream:
                yield name, data
                if name == "delta" and data["text"].endswith(text):
                    await stage_ready.wait()
                    yield "stage", {"stage": private_stage}
                    stage_sent.set()
                    await clear_stage.wait()
                    yield "stage", {"stage": ""}
                    cleared.set()
                    await finish.wait()

    monkeypatch.setattr(handler, "run", progress_only)
    fake_llm.push([text, last_token, "尾声。"])
    writes = []
    commits = []
    flush = source._flush

    async def confirmed(hub, **kwargs):
        persisted = await flush(hub, **kwargs)
        if persisted and not hub.dirty and hub.text == text:
            commits.append(time.monotonic())
        return persisted

    monkeypatch.setattr(source, "_flush", confirmed)

    def capture(conn, cursor, statement, parameters, context, many):
        if not statement.startswith("UPDATE tasks SET"):
            return
        for values in context.compiled_parameters or ():
            if values.get("output_text") == text:
                writes.append((time.monotonic(), values.get("stage")))

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
    try:
        ref = (await _generate(client, chapter, target_words=300)).json()
        params = task_params(ref)
        path = f"/api/v1/tasks/{ref['id']}"
        async with _new_client(second_app) as peer, _new_client(second_app) as peer_admin:
            peer.cookies.update(client.cookies)
            peer_admin.cookies.update(system_admin.cookies)
            await wait_until(lambda: ref["id"] in source.hubs and source.hubs[ref["id"]].text == text, within=12)
            observed = time.monotonic()
            live = source.hubs[ref["id"]]
            stale, _ = destination.ensure_hub(ref["id"], statistics_key=ref["task_key"], queued_at=live.queued_at,
                                              execution_key=live.execution_key, status="running")
            stale.text, stale.progress, stale.stage = "过期正文", 99, "过期阶段"
            assert (await _read(client, path + "/output", params))["text"] == text
            assert (await _read(peer, path + "/output", params))["text"] != stale.text
            assert (await _read(peer, path, params))["progress"] != stale.progress
            assert (await _read(peer, "/api/v1/tasks"))[0]["stage"] != stale.stage
            admin_url = f"/api/v1/admin/tasks/{ref['task_key']}"
            assert (await _read(peer_admin, admin_url))["progress"] != stale.progress
            assert await task_quota_used(system_admin, owner_id) == await task_quota_used(peer_admin, owner_id) == 1

            # 两秒按配置窗口计算。额外的 300 ms 留给独立 HTTP 采样和数据库提交。
            limit = source.flush_interval + 0.30
            at = await _wait_output(peer, path + "/output", params, text, observed + limit)
            assert at - observed <= limit
            assert len(writes) == 1
            await wait_until(lambda: len(commits) == 1)
            record_property("tail_commit_seconds", round(commits[0] - observed, 3))
            record_property("tail_http_seconds", round(at - observed, 3))
            local = await _read(client, path, params)
            assert (await _read(peer, path, params))["progress"] == local["progress"] == live.progress > 0
            stage_ready.set()
            await asyncio.wait_for(stage_sent.wait(), 8)
            observed = time.monotonic()
            assert (await _read(client, path, params))["stage"] == expected_stage
            while (await _read(peer, path, params))["stage"] != expected_stage:
                assert time.monotonic() - observed <= limit, "单独的 stage 在静默后仍未刷库"
                await asyncio.sleep(0.02)
            stage_at = time.monotonic()
            assert stage_at - observed <= limit
            assert len(writes) == 2
            await wait_until(lambda: len(commits) == 2)
            record_property("stage_commit_seconds", round(commits[1] - observed, 3))
            record_property("stage_http_seconds", round(stage_at - observed, 3))
            assert writes[-1][0] - writes[0][0] >= source.flush_interval - 0.05
            assert (await _read(peer, "/api/v1/tasks"))[0]["stage"] == expected_stage
            for admin in (system_admin, peer_admin):
                for brief in (await _read(admin, admin_url), (await _read(admin, "/api/v1/admin/tasks"))["items"][0]):
                    assert brief["progress"] == live.progress and "私有插件" not in str(brief)
                    assert not {"worker_key", "execution_key", "output_text"}.intersection(brief)
            clear_stage.set()
            await asyncio.wait_for(cleared.wait(), 8)
            observed = time.monotonic()
            assert (await _read(client, path, params))["stage"] == ""
            deadline = observed + limit
            while (await _read(peer, path, params))["stage"]:
                assert time.monotonic() <= deadline
                await asyncio.sleep(0.02)
            clear_at = time.monotonic()
            assert clear_at <= deadline
            record_property("clear_stage_http_seconds", round(clear_at - observed, 3))
            finish.set()
            last_token.release()
            assert (await wait_task(peer, ref, within=12))["status"] == "succeeded"
            assert (await _read(peer, path + "/output", params))["text"] == text + "尾声。"
            await wait_until(lambda: ref["id"] not in source._running)
            assert await task_quota_used(system_admin, owner_id) == await task_quota_used(peer_admin, owner_id) == 0
    finally:
        stage_ready.set()
        clear_stage.set()
        finish.set()
        last_token.release()
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)


async def test_projectless_unproven_receipt_is_idempotent_and_scoped(app, second_app, client):
    user = await register(client)
    engine = app.state.task_engine
    now = datetime.now(UTC)
    async with app.state.sessionmaker.begin() as db:
        pending, other = [Task(user_id=user["id"], kind="book.import", status="running", queued_at=now,
                               started_at=now, worker_key=engine.lease.worker_key, execution_key=str(uuid4()),
                               stage="处理中") for _ in range(2)]
        db.add_all([pending, other])
        await db.flush()
    await engine.record_uncertain_execution(pending)
    await engine.record_uncertain_execution(pending)
    assert not engine.has_cleanup_failure(None)
    async with _new_client(second_app) as peer:
        peer.cookies.update(client.cookies)
        for observer in (client, peer):
            rows = {r["id"]: r for r in await _read(observer, "/api/v1/tasks")}
            assert rows[pending.id]["stage"] == "结果待核对"
            assert rows[other.id]["stage"] == "处理中"
            detail = await _read(observer, f"/api/v1/tasks/{pending.id}", {"task_key": pending.statistics_key})
            assert (detail["stage"], detail["error"]) == ("结果待核对", "任务保存结果尚未确认，请稍后核对")
        async with app.state.sessionmaker.begin() as db:
            await db.execute(update(Task).where(Task.id == pending.id).values(
                status="succeeded", stage="已完成", execution_key=None, worker_key=None,
            ))
            await db.execute(update(Task).where(Task.id == other.id).values(status="cancelled", stage="已取消"))
        # 迟到的原执行确认不能覆盖成功或取消，也不能为不同任务补一条失败收据。
        await engine.record_uncertain_execution(pending)
        await engine.record_uncertain_execution(other)
        async with second_app.state.sessionmaker() as db:
            assert await db.scalar(select(func.count()).select_from(TaskWorkRecord).where(
                TaskWorkRecord.kind == "abort_failure", TaskWorkRecord.project_id.is_(None),
            )) == 1
        settled = {r["id"]: (r["status"], r["stage"]) for r in await _read(peer, "/api/v1/tasks")}
        assert settled == {pending.id: ("succeeded", "已完成"), other.id: ("cancelled", "已取消")}


async def test_batch_cursor_reset_and_late_timer_keep_new_short_text(
    app, second_app, client, fake_llm, monkeypatch,
):
    from app.services.task_engine.output_flush import OutputSnapshot
    from tests.test_batch_generate import _start

    _, base, (first, second) = await _setup(client, chapters=2)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "flush_interval", 0.35)
    handler = get_handler("chapter.batch")
    run = handler.run
    first_end, allow_reset = asyncio.Event(), asyncio.Event()
    first_gate, second_gate = Gate(), Gate()
    old_text, new_text = "同一开头，后文已经很长。", "同一开头"
    fake_llm.push([old_text, first_gate])
    fake_llm.push([new_text, second_gate])
    first_snapshot = []
    original_flush = engine._flush

    async def capture(hub, **kwargs):
        if hub.text == old_text and not first_snapshot:
            # 此发布可恰好越过节流截止时间而同步刷写。两条路径冻结同一份内容与来源。
            snapshot = kwargs.get("idle_snapshot") or OutputSnapshot.capture(hub)
            first_snapshot.append((hub, {**kwargs, "idle_snapshot": snapshot}))
        return await original_flush(hub, **kwargs)

    async def pause_after_checkpoint(job, ctx):
        async with aclosing(run(job, ctx)) as stream:
            async for name, data in stream:
                if name == "chapter_end" and data["index"] == 0:
                    first_end.set()
                    await allow_reset.wait()
                yield name, data

    monkeypatch.setattr(engine, "_flush", capture)
    monkeypatch.setattr(handler, "run", pause_after_checkpoint)
    try:
        ref = (await _start(client, base, first, second)).json()
        path, params = f"/api/v1/tasks/{ref['id']}/output", task_params(ref)
        async with _new_client(second_app) as peer:
            peer.cookies.update(client.cookies)
            await first_gate.wait_reached(within=10)
            await _wait_output(peer, path, params, old_text, time.monotonic() + 3)
            assert first_snapshot
            first_gate.release()
            await asyncio.wait_for(first_end.wait(), 10)
            hub, previous = first_snapshot[0]
            # cursor 已在 A 业务事务提交，hub 暂时还持第一章文本。
            assert hub.text == old_text
            assert await original_flush(hub, **previous)
            for observer in (client, peer):
                assert (await _read(observer, path, params))["text"] == ""
            allow_reset.set()
            await second_gate.wait_reached(within=10)
            await _wait_output(peer, path, params, new_text, time.monotonic() + 3)
            assert await original_flush(hub, **previous)
            assert (await _read(peer, path, params))["text"] == new_text
            second_gate.release()
            assert (await wait_task(peer, ref, within=12))["status"] == "succeeded"
            assert await original_flush(hub, **previous)
            assert (await _read(peer, path, params))["text"] == new_text
    finally:
        first_gate.release()
        allow_reset.set()
        second_gate.release()


async def test_fenced_runner_never_overlays_unflushed_content(
    app, second_app, client, system_admin, fake_llm, monkeypatch,
):
    from tests.worker_helpers import fence_worker, freeze_heartbeat

    _, _, (chapter,) = await _setup(client)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "flush_interval", 3600)
    gate = Gate()
    unpublished = "只存在于原执行进程的文字。" * 5
    fake_llm.push([unpublished, gate])
    ref = (await _generate(client, chapter, target_words=300)).json()
    params, path = task_params(ref), f"/api/v1/tasks/{ref['id']}"
    try:
        await gate.wait_reached(within=10)
        hub = engine.hubs[ref["id"]]
        await wait_until(lambda: hub.text == unpublished, within=10)
        assert (await _read(client, path + "/output", params))["text"] == unpublished
        await freeze_heartbeat(engine)
        await fence_worker(engine, mirror=False)
        assert engine.lease.valid and not hub.closed and hub.progress > 0
        async with _new_client(second_app) as peer:
            peer.cookies.update(client.cookies)
            for reader in (client, peer):
                assert (await _read(reader, path + "/output", params))["text"] == ""
                assert (await _read(reader, path, params))["progress"] == 0
                assert (await _read(reader, "/api/v1/tasks"))[0]["progress"] == 0
            assert (await _read(system_admin, f"/api/v1/admin/tasks/{ref['task_key']}"))["progress"] == 0
            assert (await _read(system_admin, "/api/v1/admin/tasks"))["items"][0]["progress"] == 0
    finally:
        gate.release()
