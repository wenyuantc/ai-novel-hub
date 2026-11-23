"""C3：删除协调器等待真正执行端，启动恢复只释放原请求的意图。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.sql.dml import Update

from app.core.errors import ConflictError
from app.models import AdminAuditLog, Project, Task, TaskExecutionSlot
from app.services import project_lifecycle, recycle_bin_service, task_service
from app.services.task_engine import execution_slots
from tests.conftest import _new_client
from tests.helpers import Gate, create_project, register, task_params
from tests.schema_helpers import isolated_worker_database
from tests.test_recycle_bin_tasks import generate, setup_chapter
from tests.worker_helpers import expire_worker

TIMEOUT = 20


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


async def test_peer_delete_stops_owner_before_trashing(app, second_app, client, fake_llm):
    owner = app.state.task_engine
    _, project, chapter = await setup_chapter(client)
    pid = project["id"]
    model = Gate()
    fake_llm.push(["保留原稿", model, "迟到的输出"])
    task = await generate(client, chapter)
    try:
        await model.wait_reached(within=TIMEOUT)
        await second_app.state.task_engine.start()
        async with _new_client(second_app) as peer:
            peer.cookies.update(client.cookies)
            result = await asyncio.wait_for(peer.delete(f"/api/v1/projects/{pid}"), TIMEOUT)
        assert result.status_code == 204, result.text
        assert fake_llm.aborted == 1
        assert task["id"] not in owner._running
        async with second_app.state.sessionmaker() as db:
            assert (await db.get(Project, pid)).deleted_at is not None
            assert (await db.get(Task, task["id"])).status == "cancelled"
    finally:
        model.release()


async def test_peer_delete_waits_after_slot_release_until_coroutine_exits(
    app, second_app, client, fake_llm, monkeypatch,
):
    owner, peer_engine = app.state.task_engine, second_app.state.task_engine
    released, leave = asyncio.Event(), asyncio.Event()
    peer_observed_twice = asyncio.Event()
    release_slot = execution_slots.release
    original_work = peer_engine.project_work
    ensure_idle = recycle_bin_service._ensure_idle
    visits = 0

    async def pause_after_commit(db, execution):
        await release_slot(db, execution)
        released.set()
        await leave.wait()

    def observe_work(project_id):
        nonlocal visits
        visits += 1
        if visits >= 2:
            peer_observed_twice.set()
        return original_work(project_id)

    async def final_check(db, engine, project_id):
        assert task["id"] not in owner._running, "B 最终检查时 A 仍在等待执行体退出"
        await ensure_idle(db, engine, project_id)

    actor, project, chapter = await setup_chapter(client)
    other_project = await create_project(client, title="后继轮次所在的作品")
    pid = project["id"]
    monkeypatch.setattr(execution_slots, "release", pause_after_commit)
    fake_llm.push("已经保存的正文")
    task = await generate(client, chapter)
    try:
        await asyncio.wait_for(released.wait(), TIMEOUT)
        assert task["id"] in owner._running
        async with second_app.state.sessionmaker() as db:
            assert (await db.get(Task, task["id"])).status == "succeeded"
            assert await db.scalar(select(TaskExecutionSlot).where(TaskExecutionSlot.task_id == task["id"])) is None
        erased = await client.delete(f"/api/v1/tasks/{task['id']}", params=task_params(task))
        assert erased.status_code == 204, erased.text
        async with second_app.state.sessionmaker.begin() as db:
            db.add(Task(id=task["id"], statistics_key=str(uuid4()), user_id=actor["id"],
                        project_id=other_project["id"], kind="chapter.analyze", status="succeeded"))
        await peer_engine.start()
        monkeypatch.setattr(peer_engine, "project_work", observe_work)
        monkeypatch.setattr(recycle_bin_service, "_ensure_idle", final_check)
        async with _new_client(second_app) as peer:
            peer.cookies.update(client.cookies)
            deletion = asyncio.create_task(peer.delete(f"/api/v1/projects/{pid}"))
            waiting = asyncio.create_task(peer_observed_twice.wait())
            try:
                done, _ = await asyncio.wait({deletion, waiting}, timeout=TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
                if deletion in done:
                    result = await deletion
                    pytest.fail(f"原执行体尚未退出，删除已返回 {result.status_code}")
                assert waiting in done and not deletion.done()
                async with app.state.sessionmaker() as db:
                    row = await db.get(Project, pid)
                    assert row.deletion_token and row.deleted_at is None
                leave.set()
                result = await asyncio.wait_for(deletion, TIMEOUT)
                assert result.status_code == 204, result.text
            finally:
                leave.set()
                waiting.cancel()
                await asyncio.gather(waiting, deletion, return_exceptions=True)
    finally:
        leave.set()


async def test_healthy_deletion_survives_peer_start_then_finishes_once(
    app, second_app, client, system_admin, monkeypatch,
):
    await register(client)
    pid = (await create_project(client))["id"]
    entered, resume = asyncio.Event(), asyncio.Event()
    stop = task_service.stop_project_tasks

    async def hold(db, tasks, project_id):
        entered.set()
        await resume.wait()
        return await stop(db, tasks, project_id)

    monkeypatch.setattr(task_service, "stop_project_tasks", hold)
    deletion = asyncio.create_task(system_admin.delete(f"/api/v1/projects/{pid}"))
    try:
        await asyncio.wait_for(entered.wait(), TIMEOUT)
        async with second_app.state.sessionmaker() as db:
            token = (await db.get(Project, pid)).deletion_token
        await second_app.state.task_engine.start()
        await recycle_bin_service.recover_pending_deletions(second_app.state.sessionmaker)
        async with second_app.state.sessionmaker() as db:
            row = await db.get(Project, pid)
            assert token is not None and row.deletion_token == token and row.deleted_at is None
        resume.set()
        result = await asyncio.wait_for(deletion, TIMEOUT)
        assert result.status_code == 204, result.text
        async with second_app.state.sessionmaker() as db:
            assert (await db.get(Project, pid)).deleted_at is not None
            assert len(list(await db.scalars(select(AdminAuditLog.id).where(
                AdminAuditLog.action == "project.delete", AdminAuditLog.project_id == pid,
            )))) == 1
    finally:
        resume.set()
        await asyncio.gather(deletion, return_exceptions=True)


async def test_recovery_requires_confirmed_original_delete_worker(app, second_app, client):
    await register(client)
    projects = [await create_project(client, title=f"意图{i}") for i in range(3)]
    origin = app.state.task_engine.request_worker
    keys = [origin.worker_key, str(uuid4()), None]
    assert keys[0]
    async with app.state.sessionmaker.begin() as db:
        for project, key in zip(projects, keys, strict=True):
            row = await db.get(Project, project["id"])
            row.deletion_token = str(uuid4())
            row.deletion_started_at = project_lifecycle.now_utc()
            row.deletion_worker_key = key
    await second_app.state.task_engine.start()
    await origin.stop()
    await second_app.state.task_engine.recover()
    async with second_app.state.sessionmaker() as db:
        rows = [await db.get(Project, i["id"]) for i in projects]
        assert rows[0].deletion_token is rows[0].deletion_worker_key is rows[0].deletion_started_at is None
        assert all(row.deletion_token and row.deleted_at is None for row in rows[1:])


async def test_fenced_delete_cannot_cancel_replacement_or_clear_its_token(
    app, second_app, client, monkeypatch,
):
    await register(client)
    pid = (await create_project(client))["id"]
    owner, peer_engine = app.state.task_engine, second_app.state.task_engine
    release = {owner: Gate(), peer_engine: Gate()}
    stop = task_service.stop_project_tasks

    async def hold(db, engine, project_id):
        release[engine].reached.set()
        await release[engine].released.wait()
        return await stop(db, engine, project_id)

    monkeypatch.setattr(task_service, "stop_project_tasks", hold)
    await peer_engine.start()
    async with _new_client(second_app) as peer:
        peer.cookies.update(client.cookies)
        first = asyncio.create_task(client.delete(f"/api/v1/projects/{pid}"))
        second = None
        try:
            await release[owner].wait_reached(within=TIMEOUT)
            async with second_app.state.sessionmaker() as db:
                old = (await db.get(Project, pid)).deletion_token

            async def no_renewal():
                return True

            monkeypatch.setattr(owner.request_worker.lease, "heartbeat", no_renewal)
            await expire_worker(SimpleNamespace(
                lease=owner.request_worker.lease, ctx=owner.ctx, lease_seconds=owner.lease_seconds,
            ))
            await peer_engine.recover()
            async with second_app.state.sessionmaker() as db:
                assert (await db.get(Project, pid)).deletion_token is None
            second = asyncio.create_task(peer.delete(f"/api/v1/projects/{pid}"))
            await release[peer_engine].wait_reached(within=TIMEOUT)
            async with app.state.sessionmaker() as db:
                replacement = (await db.get(Project, pid)).deletion_token
                assert replacement and old and replacement != old
            release[owner].release()
            rejected = await asyncio.wait_for(first, TIMEOUT)
            assert rejected.status_code == 409, rejected.text
            async with app.state.sessionmaker() as db:
                row = await db.get(Project, pid)
                assert row.deleted_at is None
                assert row.deletion_token == replacement
                assert row.deletion_worker_key == peer_engine.request_worker.worker_key
            release[peer_engine].release()
            result = await asyncio.wait_for(second, TIMEOUT)
            assert result.status_code == 204, result.text
        finally:
            for gate in release.values():
                gate.release()
            await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)


async def test_live_app_retries_its_failed_token_release_without_clearing_other_intents(
    app, second_app, client, monkeypatch,
):
    await register(client)
    project_id = (await create_project(client))["id"]
    await app.state.task_engine.stop()  # 保留接收 HTTP 的请求租约，排除调度唤醒的影响。
    armed = False
    rejected = False

    def fail_clear(conn, cursor, statement, params, context, executemany):
        nonlocal rejected
        operation = getattr(context.compiled, "statement", None)
        if armed and not rejected and isinstance(operation, Update) and operation.table.name == "projects":
            rejected = True
            raise RuntimeError("注入释放 token 时数据库暂不可写")

    async def fail_drain(db, engine, pid):
        nonlocal armed
        assert pid == project_id
        armed = True
        raise ConflictError("注入已受理删除的排空失败", extra={"code": "project_tasks_not_stopped"})

    with monkeypatch.context() as patches:
        patches.setattr(task_service, "stop_project_tasks", fail_drain)
        event.listen(app.state.engine.sync_engine, "before_cursor_execute", fail_clear)
        try:
            result = await client.delete(f"/api/v1/projects/{project_id}")
            assert result.status_code == 409 and rejected
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", fail_clear)
    await recycle_bin_service.recover_pending_deletions(second_app.state.sessionmaker)
    async with second_app.state.sessionmaker() as db:
        saved = await db.get(Project, project_id)
        assert saved.deletion_token is not None and saved.deleted_at is None
        assert saved.deletion_worker_key == app.state.task_engine.request_worker.worker_key
    await recycle_bin_service.recover_pending_deletions(app.state.sessionmaker)
    async with second_app.state.sessionmaker() as db:
        saved = await db.get(Project, project_id)
        assert saved.deletion_token is saved.deletion_worker_key is saved.deletion_started_at is None
    result = await client.delete(f"/api/v1/projects/{project_id}")
    assert result.status_code == 204, result.text
