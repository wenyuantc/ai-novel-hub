"""C3 本人、管理员及下一入队轮次竞争时，跨进程读取只返回原请求可见的结果。"""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.models import AdminAuditLog, Chapter, GenerationLog, Task, TaskStatistic, User
from app.services import task_statistics_service
from app.services.task_engine import owner_cancellation
from app.services.task_engine.registry import get_handler
from tests.conftest import _new_client
from tests.helpers import Gate, register, task_params, wait_task, wait_until
from tests.schema_helpers import isolated_worker_database
from tests.test_admin_task_adversarial import CANCEL_BODY
from tests.test_batch_generate import _start as start_batch
from tests.test_tasks import _generate, _setup


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


@asynccontextmanager
async def accepted_owner_request(app, peer_app, peer, ref, monkeypatch):
    """让 B 提交本人意图，A 的本人扫描晚一个周期；在 B 首次读取结果前交给测试决定顺序。"""
    engine = app.state.task_engine
    waiting = Gate()
    original_wait = owner_cancellation.wait_for_result
    original_scan = owner_cancellation.scan

    async def wait(db, live_engine, *args, **kwargs):
        if live_engine is peer_app.state.task_engine:
            waiting.reached.set()
            await waiting.released.wait()
        return await original_wait(db, live_engine, *args, **kwargs)

    async def scan(live_engine):
        if live_engine is not engine or waiting.released.is_set():
            await original_scan(live_engine)

    monkeypatch.setattr(owner_cancellation, "wait_for_result", wait)
    monkeypatch.setattr(owner_cancellation, "scan", scan)
    request = asyncio.create_task(peer.post(f"/api/v1/tasks/{ref['id']}/cancel", params=task_params(ref)))
    try:
        await waiting.wait_reached()
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, ref["id"])
            assert row.status == "running" and row.worker_key == engine.lease.worker_key
            assert row.statistics_key == ref["task_key"] and row.cancel_requested_at is not None
            assert row.admin_cancel_operation_id is None
        yield request, waiting
    finally:
        waiting.release()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("disconnect", [True, False], ids=["disconnected-owner", "revoked-owner"])
async def test_remote_admin_finishes_same_running_round_after_owner_acceptance(
    app, second_app, client, system_admin, fake_llm, monkeypatch, disconnect,
):
    _, _, (chapter,) = await _setup(client, content="原稿")
    model = Gate()
    fake_llm.push([model, "不应写入的替换正文"])
    created = await _generate(client, chapter)
    assert created.status_code == 202, created.text
    ref = created.json()
    owner = app.state.task_engine
    try:
        await model.wait_reached()
        await second_app.state.task_engine.start()
        async with _new_client(second_app) as peer, _new_client(second_app) as manager:
            peer.cookies.update(client.cookies)
            manager.cookies.update(system_admin.cookies)
            async with accepted_owner_request(app, second_app, peer, ref, monkeypatch) as (request, waiting):
                if disconnect:
                    request.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await request
                accepted = await manager.post(f"/api/v1/admin/tasks/{ref['task_key']}/cancel", json=CANCEL_BODY)
                assert accepted.status_code == 202, accepted.text
                operation = accepted.json()["operation_id"]
                if not disconnect:
                    async with second_app.state.sessionmaker.begin() as db:
                        actor = await db.scalar(select(Task.user_id).where(Task.id == ref["id"]))
                        await db.execute(update(User).where(User.id == actor).values(
                            token_version=User.token_version + 1))
                await owner.scan_admin_cancellations()
                await owner.wait_admin_cancellations()
                await wait_until(lambda: ref["id"] not in owner._running)
                if not disconnect:
                    waiting.release()
                    response = await asyncio.wait_for(request, 5)
                    assert response.status_code == 401, response.text
                    assert "task_key" not in response.json()
        async with second_app.state.sessionmaker() as db:
            saved = await db.get(Task, ref["id"])
            assert saved.status == "cancelled"
            assert saved.admin_cancel_operation_id is None and saved.cancel_requested_at is None
            assert (await db.get(TaskStatistic, ref["task_key"])).status == "cancelled"
            logs = list(await db.scalars(select(GenerationLog.status).where(GenerationLog.task_id == ref["id"])))
            assert logs == ["cancelled"]
            saved_chapter = await db.get(Chapter, chapter["id"])
            assert (saved_chapter.status, saved_chapter.content, saved_chapter.generation_claim_key) == (
                "draft", "原稿", None,
            )
            audits = list(await db.scalars(select(AdminAuditLog.result).where(
                AdminAuditLog.action == "task.cancel_request", AdminAuditLog.operation_id == operation,
            )))
            assert audits == ["success"]
        assert fake_llm.aborted == 1
    finally:
        model.release()


async def test_owner_wait_for_original_round_does_not_cancel_resumed_batch(
    app, second_app, client, fake_llm, monkeypatch,
):
    _, base, chapters = await _setup(client, chapters=2)
    first, second = Gate(), Gate()
    fake_llm.push([first, "第一章定稿"])
    fake_llm.push([second, "第二章定稿"])
    created = await start_batch(client, base, *chapters, review_every=1)
    assert created.status_code == 202, created.text
    ref = created.json()
    engine = app.state.task_engine
    try:
        await first.wait_reached()
        # B 的 HTTP 生命周期可用，调度仍留给 A，使两次真实执行的 owner 都已知。
        async with _new_client(second_app) as peer:
            peer.cookies.update(client.cookies)
            async with accepted_owner_request(app, second_app, peer, ref, monkeypatch) as (request, waiting):
                first.release()
                paused = await wait_task(client, ref, statuses=("paused",))
                assert paused["task_key"] == ref["task_key"]
                await wait_until(lambda: ref["id"] not in engine._running)
                resumed = await client.post(f"/api/v1/tasks/{ref['id']}/resume", params=task_params(ref))
                assert resumed.status_code == 202, resumed.text
                await second.wait_reached()
                waiting.release()
                response = await asyncio.wait_for(request, 5)
                assert response.status_code == 409, response.text
                assert response.json()["code"] == "task_round_changed"
                async with second_app.state.sessionmaker() as db:
                    current = await db.get(Task, ref["id"])
                    assert (current.statistics_key, current.status, current.cancel_requested_at) == (
                        ref["task_key"], "running", None,
                    )
                assert not second.released.is_set() and fake_llm.aborted == 0
                second.release()
                assert (await wait_task(client, ref))["status"] == "succeeded"
    finally:
        first.release()
        second.release()


async def test_late_owner_wait_cannot_return_or_cancel_another_users_reused_integer_id(
    app, second_app, client, make_client, fake_llm, monkeypatch,
):
    _, _, (chapter,) = await _setup(client)
    stranger = await register(make_client(), "c3-independent-owner")
    model = Gate()
    fake_llm.push([model, "已保存"])
    accepted = await _generate(client, chapter)
    assert accepted.status_code == 202, accepted.text
    ref = accepted.json()
    engine = app.state.task_engine
    try:
        await model.wait_reached()
        async with _new_client(second_app) as peer:
            peer.cookies.update(client.cookies)
            async with accepted_owner_request(app, second_app, peer, ref, monkeypatch) as (request, waiting):
                model.release()
                assert (await wait_task(client, ref))["status"] == "succeeded"
                await wait_until(lambda: ref["id"] not in engine._running)
                removed = await client.delete(f"/api/v1/tasks/{ref['id']}", params=task_params(ref))
                assert removed.status_code == 204, removed.text
                # 显式复用 SQLite 可重新分配的整数 ID，在 PostgreSQL 复现同一数据边界。
                async with app.state.sessionmaker.begin() as db:
                    replacement = Task(id=ref["id"], user_id=stranger["id"], kind="agent.turn", status="paused",
                                       title="另一用户的私有内容")
                    db.add(replacement)
                waiting.release()
                response = await asyncio.wait_for(request, 5)
                assert response.status_code == 404, response.text
                assert replacement.title not in response.text and replacement.statistics_key not in response.text
                async with second_app.state.sessionmaker() as db:
                    current = await db.get(Task, ref["id"])
                    assert current.statistics_key == replacement.statistics_key != ref["task_key"]
                    assert current.status == "paused" and current.cancel_requested_at is None
    finally:
        model.release()


async def test_owner_and_admin_join_an_orphan_cleanup_that_has_already_started(
    app, second_app, client, system_admin, fake_llm, monkeypatch,
):
    actor = await register(client)
    engine = app.state.task_engine
    # 不自动重扫；下方显式让真实 C1 接管和管理员扫描先后到达同一轮。
    await engine.stop()
    await engine.lease.register()
    cleanup = Gate()
    aborts = []
    handler = get_handler("agent.turn")
    original = handler.on_abort

    async def count_abort(ctx, task, **options):
        aborts.append((task.statistics_key, task.execution_key))
        if len(aborts) == 1:
            cleanup.reached.set()
            await cleanup.released.wait()
        await original(ctx, task, **options)

    monkeypatch.setitem(vars(handler), "on_abort", count_abort)
    async with app.state.sessionmaker.begin() as db:
        row = Task(user_id=actor["id"], kind="agent.turn", status="running", execution_key=str(uuid4()), input={})
        db.add(row)
        await db.flush()
        await task_statistics_service.record_created(db, row)
    ref = {"id": row.id, "task_key": row.statistics_key}
    recovery = asyncio.create_task(engine.recover())
    try:
        await cleanup.wait_reached()
        async with _new_client(second_app) as peer, _new_client(second_app) as manager:
            peer.cookies.update(client.cookies)
            manager.cookies.update(system_admin.cookies)
            async with accepted_owner_request(app, second_app, peer, ref, monkeypatch) as (request, waiting):
                accepted = await manager.post(f"/api/v1/admin/tasks/{row.statistics_key}/cancel", json=CANCEL_BODY)
                assert accepted.status_code == 202, accepted.text
                await engine.scan_admin_cancellations()
                await engine.wait_admin_cancellations()
                async with second_app.state.sessionmaker() as db:
                    current = await db.get(Task, row.id)
                    assert current.status == "running" and current.worker_key == engine.lease.worker_key
                    assert current.admin_cancel_operation_id == accepted.json()["operation_id"]
                cleanup.release()
                await asyncio.wait_for(recovery, 5)
                await engine.scan_admin_cancellations()
                await engine.wait_admin_cancellations()
                waiting.release()
                response = await asyncio.wait_for(request, 5)
                assert response.status_code == 200 and response.json()["status"] == "interrupted", response.text
        assert aborts == [(row.statistics_key, row.execution_key)] and not fake_llm.calls
        async with second_app.state.sessionmaker() as db:
            current = await db.get(Task, row.id)
            assert (current.cancel_requested_at, current.admin_cancel_operation_id, current.execution_key) == (
                None, None, None,
            )
            assert (await db.get(TaskStatistic, row.statistics_key)).status == "interrupted"
            audits = list(await db.scalars(select(AdminAuditLog.result).where(
                AdminAuditLog.action == "task.cancel_request",
                AdminAuditLog.operation_id == accepted.json()["operation_id"],
            )))
            assert audits == ["success"]
    finally:
        cleanup.release()
        await asyncio.gather(recovery, return_exceptions=True)
        await engine.lease.stop()
