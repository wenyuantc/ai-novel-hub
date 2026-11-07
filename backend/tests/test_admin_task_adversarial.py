"""独立对抗测试：真实数据库写边界、失败事务和执行 UUID 代次。"""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import AdminAuditLog, Chapter, Project, Task, TaskStatistic, User
from app.models.entities import FINISHED_TASK_STATUSES
from app.services import task_service, task_statistics_service
from app.services.task_engine.hub import TaskHub
from app.services.task_engine.registry import get_handler
from tests.helpers import create_ai_config, create_project, register

CANCEL_BODY = {"confirm": True, "reason": "人工排查异常占用"}
PRIVATE_PARTS = (
    "IGNORE_RULES_PROMPT_9f2a",
    "sk-private-adversarial-9f2a",
    "https://private-9f2a.example.test/secret",
    "私人插件_9f2a",
    "未公开正文_9f2a",
)


async def _actors(app, admin, make_client):
    # 停止自动调度后，测试显式驱动真实 dispatch / drain，不依赖定时器碰巧触发。
    await app.state.task_engine.stop()
    manager = await register(admin, "adversarial_manager")
    owner = make_client()
    user = await register(owner, "adversarial_owner")
    async with app.state.sessionmaker() as db:
        (await db.get(User, manager["id"])).is_admin = True
        await db.commit()
    return owner, user["id"]


async def _task(app, owner_id, status="queued", **values):
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner_id, kind="agent.turn", status=status, **values)
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
        return task


async def _accept(admin, task_key):
    response = await admin.post(f"/api/v1/admin/tasks/{task_key}/cancel", json=CANCEL_BODY)
    assert response.status_code == 202, response.text
    return response.json()


def _status_update(statement, status):
    """只拦截真正的 Task 状态 UPDATE，不拦 SELECT 或统计更新。"""
    return (
        getattr(statement, "is_update", False)
        and statement.table.name == Task.__tablename__
        and statement.compile().params.get("status") == status
    )


async def _assert_open_intent(app, task, operation_id, status):
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task.id)
        statistic = await db.get(TaskStatistic, task.statistics_key)
        assert current.statistics_key == task.statistics_key
        assert current.status == status
        assert current.admin_cancel_operation_id == operation_id
        assert current.admin_cancel_requested_at is not None
        assert current.finished_at is None
        assert statistic.status == status
        assert statistic.finished_event_at is None


@pytest.mark.parametrize(
    ("operation", "initial_status", "attempted_status"),
    [
        ("cancel_task", "queued", "cancelled"),
        ("cancel_task", "paused", "cancelled"),
        ("cancel_task", "running", "cancelled"),
        ("resume_task", "paused", "queued"),
        ("finish_paused_task", "paused", "succeeded"),
    ],
)
async def test_admin_acceptance_between_owner_check_and_update_is_a_real_cas_barrier(
    app, client, make_client, monkeypatch, fake_llm, operation, initial_status, attempted_status
):
    _, owner_id = await _actors(app, client, make_client)
    task = await _task(app, owner_id, initial_status)
    engine = app.state.task_engine
    signals, aborts = [], []
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: signals.append(args))
    handler = get_handler(task.kind)

    async def revalidate(*args):
        return None

    async def abort(ctx, row):
        aborts.append(row.statistics_key)

    # 不测试 agent 的提案前置条件；只隔离恢复/结束预校验，真实条件写不能被替换。
    monkeypatch.setattr(handler, "revalidate_resume", revalidate)
    monkeypatch.setattr(handler, "finish_result", lambda row: {"ended_early": True})
    monkeypatch.setattr(handler, "on_abort", abort)
    reached, release = asyncio.Event(), asyncio.Event()
    async with app.state.sessionmaker() as db:
        actual_execute = db.execute

        async def blocked_execute(statement, *args, **kwargs):
            if not reached.is_set() and _status_update(statement, attempted_status):
                reached.set()
                await release.wait()
            return await actual_execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", blocked_execute)
        owner_write = asyncio.create_task(getattr(task_service, operation)(db, engine, owner_id, task.id))
        try:
            await asyncio.wait_for(reached.wait(), 3)
            # 第二个真实 HTTP/数据库会话先提交意图；owner 已通过所有旧值检查。
            accepted = await _accept(client, task.statistics_key)
            await _assert_open_intent(app, task, accepted["operation_id"], initial_status)
            aborts_before_acceptance = list(aborts)
            release.set()
            try:
                response = await asyncio.wait_for(owner_write, 3)
            except ConflictError:
                pass  # resume/finish 可以拒绝；cancel 可以委托同一 drain。
            else:
                assert response.status == initial_status
            await _assert_open_intent(app, task, accepted["operation_id"], initial_status)
            # 孤立 running 的既有 abort 可以在受理前发生，其他分支不能在丢失 CAS 后继续旧 abort。
            assert aborts == aborts_before_acceptance
            if initial_status != "running":
                assert aborts == []
            assert signals and all(args == (task.statistics_key, accepted["operation_id"]) for args in signals)
            assert fake_llm.calls == []
        finally:
            release.set()
            await asyncio.gather(owner_write, return_exceptions=True)


async def test_admin_acceptance_after_finalize_observed_null_does_not_ignore_failed_abort(
    app, client, make_client, monkeypatch
):
    _, owner_id = await _actors(app, client, make_client)
    task = await _task(app, owner_id, "running", resource_key="adversarial:finalize")
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    aborts = []

    async def failed_abort(ctx, row):
        aborts.append(row.statistics_key)
        raise RuntimeError(PRIVATE_PARTS[1])

    handler = get_handler(task.kind)
    monkeypatch.setattr(handler, "on_abort", failed_abort)
    reached, release = asyncio.Event(), asyncio.Event()
    actual_execute = AsyncSession.execute
    finalizing = None

    async def blocked_execute(db, statement, *args, **kwargs):
        if asyncio.current_task() is finalizing and not reached.is_set() and _status_update(statement, "cancelled"):
            reached.set()
            await release.wait()
        return await actual_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", blocked_execute)
    hub = TaskHub(task.id, statistics_key=task.statistics_key, status="running")
    finalizing = asyncio.create_task(engine._finalize(
        task.id, task, handler, hub, status="cancelled", result=None, error="任务已取消"
    ))
    try:
        await asyncio.wait_for(reached.wait(), 3)
        accepted = await _accept(client, task.statistics_key)
        release.set()
        await asyncio.wait_for(finalizing, 3)
        await _assert_open_intent(app, task, accepted["operation_id"], "running")
        assert aborts == [task.statistics_key]
        async with app.state.sessionmaker() as db:
            assert (await db.get(Task, task.id)).resource_key == "adversarial:finalize"
    finally:
        release.set()
        await asyncio.gather(finalizing, return_exceptions=True)


@pytest.mark.parametrize("failure_at", ["audit_flush", "commit"])
async def test_real_audit_transaction_failure_has_no_cancel_side_effect_or_private_log(
    app, client, make_client, monkeypatch, caplog, fake_llm, failure_at
):
    _, owner_id = await _actors(app, client, make_client)
    task = await _task(app, owner_id)
    engine = app.state.task_engine
    signals, aborts = [], []
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: signals.append(args))

    async def abort(*args):
        aborts.append(args)

    monkeypatch.setattr(get_handler(task.kind), "on_abort", abort)
    actual_flush, actual_commit = AsyncSession.flush, AsyncSession.commit
    failed = False

    async def fail_flush(db, *args, **kwargs):
        nonlocal failed
        if failure_at == "audit_flush" and not failed and any(
            isinstance(row, AdminAuditLog) and row.result == "success" for row in db.new
        ):
            failed = True
            raise RuntimeError(" / ".join(PRIVATE_PARTS))
        return await actual_flush(db, *args, **kwargs)

    async def fail_commit(db):
        nonlocal failed
        if failure_at == "commit" and not failed and any(
            isinstance(row, AdminAuditLog) and row.result == "success" for row in db.identity_map.values()
        ):
            failed = True
            raise RuntimeError(" / ".join(PRIVATE_PARTS))
        return await actual_commit(db)

    monkeypatch.setattr(AsyncSession, "flush", fail_flush)
    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    response = await client.post(f"/api/v1/admin/tasks/{task.statistics_key}/cancel", json=CANCEL_BODY)
    assert failed, "故障必须注入真实审计 flush / commit，而不是绕过事务的其他分支"
    assert response.status_code == 500
    assert response.json() == {"detail": "服务器内部错误"}
    assert signals == aborts == fake_llm.calls == []
    assert all(private not in response.text + caplog.text for private in PRIVATE_PARTS)
    assert all(record.exc_info is None for record in caplog.records)
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task.id)
        statistic = await db.get(TaskStatistic, task.statistics_key)
        assert current.status == "queued"
        assert current.admin_cancel_operation_id is current.admin_cancel_requested_at is None
        assert current.finished_at is statistic.finished_event_at is None
        logs = list(await db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "task.cancel_request")))
        assert len(logs) == 1 and logs[0].result == "failure"
        assert logs[0].error_code == "system_error"
        assert all(private not in json.dumps(logs[0].summary, ensure_ascii=False) for private in PRIVATE_PARTS)


async def test_already_scanned_claim_loses_to_acceptance_and_failed_abort_keeps_real_chapter_resource(
    app, client, make_client, monkeypatch, fake_llm
):
    owner, _ = await _actors(app, client, make_client)
    project = await create_project(owner)
    await create_ai_config(owner)
    chapter_response = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "资源代次"})
    assert chapter_response.status_code == 201, chapter_response.text
    chapter_id = chapter_response.json()["id"]
    generated = await owner.post(f"/api/v1/chapters/{chapter_id}/generate", json={})
    assert generated.status_code == 202, generated.text
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, generated.json()["id"])
    engine = app.state.task_engine
    reached, release = asyncio.Event(), asyncio.Event()
    actual_claim = engine._claim_and_run

    async def claim_after_scan(*args):
        reached.set()
        await release.wait()
        await actual_claim(*args)

    monkeypatch.setattr(engine, "_claim_and_run", claim_after_scan)
    monkeypatch.setattr(engine, "_stopping", False)
    handler = get_handler(task.kind)
    actual_abort = handler.on_abort
    aborts = []
    allow_abort = False

    async def fail_until_repaired(ctx, row):
        aborts.append(row.statistics_key)
        if not allow_abort:
            raise RuntimeError(PRIVATE_PARTS[1])
        await actual_abort(ctx, row)

    monkeypatch.setattr(handler, "on_abort", fail_until_repaired)
    await engine._dispatch()
    execution = engine._running[task.id]
    try:
        await asyncio.wait_for(reached.wait(), 3)
        accepted = await _accept(client, task.statistics_key)
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)
        await _assert_open_intent(app, task, accepted["operation_id"], "queued")
        release.set()
        await asyncio.wait_for(execution, 3)
        await _assert_open_intent(app, task, accepted["operation_id"], "queued")
        conflict = await owner.post(f"/api/v1/chapters/{chapter_id}/generate", json={})
        assert conflict.status_code == 409, conflict.text
        async with app.state.sessionmaker() as db:
            assert (await db.get(Chapter, chapter_id)).status == "generating"
            assert (await db.get(Task, task.id)).resource_key == task.resource_key
        assert aborts == [task.statistics_key]
        assert fake_llm.calls == []

        allow_abort = True
        await engine.scan_admin_cancellations()
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)
        async with app.state.sessionmaker() as db:
            old = await db.get(Task, task.id)
            assert old.status == "cancelled" and old.admin_cancel_operation_id is None
            assert (await db.get(Chapter, chapter_id)).status == "draft"
        replacement = await owner.post(f"/api/v1/chapters/{chapter_id}/generate", json={})
        assert replacement.status_code == 202, replacement.text
        assert replacement.json()["id"] != task.id
        engine.request_admin_cancel(task.statistics_key, accepted["operation_id"])
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)
        async with app.state.sessionmaker() as db:
            assert (await db.get(Chapter, chapter_id)).status == "generating"
            assert (await db.get(Task, replacement.json()["id"])).status == "queued"
        assert aborts == [task.statistics_key, task.statistics_key]
        assert fake_llm.calls == []
    finally:
        release.set()
        await asyncio.gather(execution, return_exceptions=True)


async def test_old_drain_and_old_uuid_cannot_clear_new_intent_after_actual_sqlite_id_reuse(
    app, client, make_client, monkeypatch
):
    _, owner_id = await _actors(app, client, make_client)
    original = await _task(app, owner_id)
    engine = app.state.task_engine
    reached, release = asyncio.Event(), asyncio.Event()
    aborts = []

    async def blocked_abort(ctx, row):
        aborts.append(row.statistics_key)
        reached.set()
        await release.wait()

    monkeypatch.setattr(get_handler(original.kind), "on_abort", blocked_abort)
    accepted = await _accept(client, original.statistics_key)
    try:
        await asyncio.wait_for(reached.wait(), 3)
        # 保留第一会话中的旧ORM对象，第二会话真正删除并插入；不手工指定整数ID。
        async with app.state.sessionmaker() as stale_db:
            stale = await stale_db.get(Task, original.id)
            async with app.state.sessionmaker() as replacement_db:
                await task_statistics_service.mark_removed(replacement_db, Task.statistics_key == stale.statistics_key)
                await replacement_db.execute(delete(Task).where(Task.statistics_key == stale.statistics_key))
                replacement = Task(user_id=owner_id, kind="agent.turn", status="queued", output_text=PRIVATE_PARTS[-1])
                replacement_db.add(replacement)
                await task_statistics_service.record_created(replacement_db, replacement)
                await replacement_db.commit()
            assert replacement.id == stale.id
            assert replacement.statistics_key != stale.statistics_key
            signals = []
            monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: signals.append(args))
            new_acceptance = await _accept(client, replacement.statistics_key)
            assert new_acceptance["operation_id"] != accepted["operation_id"]
            gone = await client.post(f"/api/v1/admin/tasks/{stale.statistics_key}/cancel", json=CANCEL_BODY)
            assert gone.status_code == 404
            assert gone.json()["detail"] == "任务不存在或已移除"
            detail = await client.get(f"/api/v1/admin/tasks/{stale.statistics_key}")
            assert detail.status_code == 404
            release.set()
            await asyncio.wait_for(engine.wait_admin_cancellations(), 3)
            await _assert_open_intent(app, replacement, new_acceptance["operation_id"], "queued")
            async with app.state.sessionmaker() as db:
                assert (await db.get(Task, replacement.id)).output_text == PRIVATE_PARTS[-1]
                previous = await db.get(TaskStatistic, original.statistics_key)
                assert previous.removed_at is not None
                assert previous.finished_event_at is None
            assert signals == [(replacement.statistics_key, new_acceptance["operation_id"])]
            assert aborts == [original.statistics_key]
    finally:
        release.set()
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)


async def test_private_title_hub_stage_error_and_content_are_neither_projected_searched_nor_audited(
    app, client, make_client, monkeypatch, fake_llm
):
    owner, owner_id = await _actors(app, client, make_client)
    task = await _task(
        app, owner_id, "running", title=PRIVATE_PARTS[0], stage=f"调用{PRIVATE_PARTS[3]}",
        error=" / ".join(PRIVATE_PARTS[1:]), input={"request": PRIVATE_PARTS[0]},
        output_text=PRIVATE_PARTS[-1], checkpoint={"private": PRIVATE_PARTS[-1]},
        result={"private": PRIVATE_PARTS[-1]},
    )
    engine = app.state.task_engine
    hub, _ = engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="running", progress=63)
    hub.stage = f"调用{PRIVATE_PARTS[3]}：{PRIVATE_PARTS[2]}"
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    forbidden_fields = {"input", "result", "output_text", "checkpoint", "error", "resource_key", "serial_key"}
    listing = await client.get("/api/v1/admin/tasks")
    detail = await client.get(f"/api/v1/admin/tasks/{task.statistics_key}")
    assert listing.status_code == detail.status_code == 200
    projected = listing.json()["items"][0]
    assert projected["progress"] == 63 and projected["stage"] == "调用工具"
    assert projected["title"] == f"项目智能体 #{task.id}"
    # 原始 error 可能是旧值；running 不能因此被管理投影误标为执行失败。
    assert projected["error_code"] is projected["error_message"] is None
    assert detail.json()["error_code"] is detail.json()["error_message"] is None
    assert not forbidden_fields.intersection(projected)
    assert not forbidden_fields.intersection(detail.json())
    for private in PRIVATE_PARTS:
        assert private not in listing.text + detail.text
        search = await client.get("/api/v1/admin/tasks", params={"q": private})
        assert search.status_code == 200
        assert search.json()["items"] == [] and search.json()["total"] == 0
        assert sum(search.json()["counts"].values()) == 0
    # 秘密仍在真实拥有者记录，不是通过清空源字段让安全测试碰巧通过。
    owned = await owner.get(f"/api/v1/tasks/{task.id}")
    assert owned.status_code == 200 and owned.json()["title"] == PRIVATE_PARTS[0]
    accepted = await _accept(client, task.statistics_key)
    async with app.state.sessionmaker() as db:
        audit = await db.scalar(select(AdminAuditLog).where(AdminAuditLog.operation_id == accepted["operation_id"]))
        assert audit.target_label == f"项目智能体 #{task.id}"
        assert audit.summary["cancel_reason"] == CANCEL_BODY["reason"]
        encoded = json.dumps({"label": audit.target_label, "summary": audit.summary}, ensure_ascii=False)
        assert all(private not in encoded for private in PRIVATE_PARTS)
    assert fake_llm.calls == []


async def test_pending_projectless_task_blocks_account_cascade_until_actual_drain_finishes(
    app, client, make_client, monkeypatch
):
    _, owner_id = await _actors(app, client, make_client)
    task = await _task(app, owner_id)
    engine = app.state.task_engine
    reached, release = asyncio.Event(), asyncio.Event()

    async def blocked_abort(ctx, row):
        reached.set()
        await release.wait()

    monkeypatch.setattr(get_handler(task.kind), "on_abort", blocked_abort)
    accepted = await _accept(client, task.statistics_key)
    try:
        await asyncio.wait_for(reached.wait(), 3)
        response = await client.delete(f"/api/v1/admin/users/{owner_id}")
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "user_has_pending_tasks"
        await _assert_open_intent(app, task, accepted["operation_id"], "queued")
        async with app.state.sessionmaker() as db:
            assert await db.get(User, owner_id) is not None
        release.set()
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)
        assert (await client.delete(f"/api/v1/admin/users/{owner_id}")).status_code == 204
        async with app.state.sessionmaker() as db:
            assert await db.get(User, owner_id) is None
            assert await db.get(Task, task.id) is None
            statistic = await db.get(TaskStatistic, task.statistics_key)
            assert statistic.status == "cancelled" and statistic.removed_at is not None
            audit = await db.scalar(select(AdminAuditLog).where(
                AdminAuditLog.operation_id == accepted["operation_id"]
            ))
            assert audit.result == "success" and audit.owner_user_id == owner_id
    finally:
        release.set()
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)


async def test_terminal_pending_anomalies_survive_real_prune_clear_and_single_delete(
    app, client, make_client, monkeypatch
):
    owner, owner_id = await _actors(app, client, make_client)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    protected = []
    for status in FINISHED_TASK_STATUSES:
        task = await _task(app, owner_id)
        accepted = await _accept(client, task.statistics_key)
        async with app.state.sessionmaker() as db:
            # 模拟历史/故障产生的不一致组合；此时绝不能当普通终态回收恢复依据。
            await db.execute(update(Task).where(Task.statistics_key == task.statistics_key).values(
                status=status, finished_at=task_statistics_service.now_utc()
            ))
            await db.commit()
        protected.append((task, accepted["operation_id"], status))

    normal = await _task(app, owner_id, "failed")
    monkeypatch.setattr(task_service, "MAX_FINISHED_PER_USER", 0)
    async with app.state.sessionmaker() as db:
        await task_service.prune_finished(db, owner_id)
    async with app.state.sessionmaker() as db:
        assert await db.get(Task, normal.id) is None  # 确认真正执行了裁剪，而不是空操作。
    normal = await _task(app, owner_id, "interrupted")
    cleared = await owner.delete("/api/v1/tasks")
    assert cleared.status_code == 200 and cleared.json()["deleted"] == 1
    for task, operation_id, status in protected:
        refused = await owner.delete(f"/api/v1/tasks/{task.id}")
        assert refused.status_code == 409
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, task.id)
            assert row.status == status and row.admin_cancel_operation_id == operation_id
            assert (await db.get(TaskStatistic, task.statistics_key)).removed_at is None
    async with app.state.sessionmaker() as db:
        assert await db.get(Task, normal.id) is None


async def _recycled_record(admin, project_id):
    response = await admin.get("/api/v1/admin/recycle-bin/projects")
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["items"] if row["id"] == project_id)


async def _purge(admin, record):
    return await admin.post(
        f"/api/v1/admin/recycle-bin/projects/{record['id']}/purge",
        json={"deleted_at": record["deleted_at"], "title": record["title"], "confirm": True},
    )


async def test_soft_delete_timeout_keeps_independent_drain_then_waits_before_real_purge(
    app, client, make_client, monkeypatch, fake_llm
):
    owner, owner_id = await _actors(app, client, make_client)
    project = await create_project(owner)
    task = await _task(app, owner_id, project_id=project["id"])
    engine = app.state.task_engine
    reached, release, deleting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    aborted = []

    async def blocked_abort(ctx, row):
        aborted.append(row.statistics_key)
        reached.set()
        await release.wait()

    actual_stop = task_service.stop_project_tasks

    async def observed_stop(*args):
        deleting.set()
        return await actual_stop(*args)

    monkeypatch.setattr(get_handler(task.kind), "on_abort", blocked_abort)
    monkeypatch.setattr(task_service, "stop_project_tasks", observed_stop)
    monkeypatch.setattr(engine, "cancel_wait_seconds", 0.03)
    accepted = await _accept(client, task.statistics_key)
    deletion = None
    try:
        await asyncio.wait_for(reached.wait(), 3)
        assert engine.project_work(project["id"])
        deletion = asyncio.create_task(owner.delete(f"/api/v1/projects/{project['id']}"))
        await asyncio.wait_for(deleting.wait(), 3)
        response = await asyncio.wait_for(deletion, 3)
        assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped"
        await _assert_open_intent(app, task, accepted["operation_id"], "queued")
        async with app.state.sessionmaker() as db:
            row = await db.get(Project, project["id"])
            assert row.deleted_at is row.deletion_started_at is row.deletion_token is None
        assert engine.project_work(project["id"]), "HTTP 等待超时不能取消独立清理"

        monkeypatch.setattr(engine, "cancel_wait_seconds", 3)
        deleting.clear()
        deletion = asyncio.create_task(owner.delete(f"/api/v1/projects/{project['id']}"))
        await asyncio.wait_for(deleting.wait(), 3)
        assert not deletion.done()
        release.set()
        response = await asyncio.wait_for(deletion, 3)
        assert response.status_code == 204, response.text
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)
        assert not engine.project_work(project["id"])
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, task.id)
            assert row.status == "cancelled" and row.admin_cancel_operation_id is None
            assert (await db.get(Project, project["id"])).deleted_at is not None
        record = await _recycled_record(client, project["id"])
        purged = await _purge(client, record)
        assert purged.status_code == 204, purged.text
        async with app.state.sessionmaker() as db:
            assert await db.get(Project, project["id"]) is None
            assert await db.get(Task, task.id) is None
            assert (await db.get(TaskStatistic, task.statistics_key)).removed_at is not None
            audit = await db.scalar(select(AdminAuditLog).where(
                AdminAuditLog.operation_id == accepted["operation_id"]
            ))
            assert audit.result == "success"
        assert aborted == [task.statistics_key] and fake_llm.calls == []
    finally:
        release.set()
        if deletion is not None:
            await asyncio.gather(deletion, return_exceptions=True)
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)


async def test_recycled_project_cannot_be_purged_while_admin_drain_is_actual_project_work(
    app, client, make_client, monkeypatch
):
    owner, owner_id = await _actors(app, client, make_client)
    project = await create_project(owner)
    assert (await owner.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    record = await _recycled_record(client, project["id"])
    # 代表旧版本/删除协调期间仍有真实 Task 的回收站作品，管理接口仍须安全处置。
    task = await _task(app, owner_id, project_id=project["id"])
    engine = app.state.task_engine
    reached, release = asyncio.Event(), asyncio.Event()

    async def blocked_abort(ctx, row):
        reached.set()
        await release.wait()

    monkeypatch.setattr(get_handler(task.kind), "on_abort", blocked_abort)
    accepted = await _accept(client, task.statistics_key)
    try:
        await asyncio.wait_for(reached.wait(), 3)
        assert engine.project_work(project["id"])
        blocked = await _purge(client, record)
        assert blocked.status_code == 409 and blocked.json()["code"] == "project_tasks_not_stopped"
        await _assert_open_intent(app, task, accepted["operation_id"], "queued")
        release.set()
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)
        assert (await _purge(client, record)).status_code == 204
        async with app.state.sessionmaker() as db:
            assert await db.get(Project, project["id"]) is None
            assert await db.get(Task, task.id) is None
    finally:
        release.set()
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)


async def test_terminal_pending_anomaly_in_recycled_project_cannot_be_purged_or_blindly_aborted(
    app, client, make_client, monkeypatch
):
    owner, owner_id = await _actors(app, client, make_client)
    project = await create_project(owner)
    assert (await owner.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    record = await _recycled_record(client, project["id"])
    marker = str(uuid4())
    task = await _task(
        app, owner_id, "succeeded", project_id=project["id"], admin_cancel_operation_id=marker,
        admin_cancel_requested_at=task_statistics_service.now_utc(),
    )
    aborts = []

    async def forbidden_abort(*args):
        aborts.append(args)
        raise AssertionError("不能盲目回滚已成功任务")

    monkeypatch.setattr(get_handler(task.kind), "on_abort", forbidden_abort)
    engine = app.state.task_engine
    await engine.scan_admin_cancellations()
    await asyncio.wait_for(engine.wait_admin_cancellations(), 3)
    refused = await _purge(client, record)
    assert refused.status_code == 409 and refused.json()["code"] == "project_tasks_not_stopped"
    assert aborts == []
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, project["id"]) is not None
        row = await db.get(Task, task.id)
        assert row.status == "succeeded" and row.admin_cancel_operation_id == marker
        assert (await db.get(TaskStatistic, task.statistics_key)).removed_at is None


async def test_owner_task_projection_handles_arbitrary_historical_input_without_repairing_it(app, client):
    user = await register(client)
    project = await create_project(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={})).json()
    scenes = (await client.get(f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}/scenes")).json()
    scene_id = scenes["items"][0]["id"]
    payloads = [
        (None, None), ("legacy-root", None), ([], None), (17, None), (True, None),
        ({"request": "legacy-request"}, None), ({"request": []}, None), ({"request": False}, None),
        ({"request": {"target": []}}, None),
        ({"request": {"target": {"kind": "scene", "scene_id": True}}}, None),
        ({"request": {"target": {"kind": "scene", "scene_id": str(scene_id)}}}, None),
        ({"request": {"target": {"kind": "scene", "scene_id": scene_id, "unexpected": True}}}, None),
        ({"request": {"target": {"kind": "scene", "scene_id": scene_id}}}, scene_id),
    ]
    for payload, expected_scene in payloads:
        async with app.state.sessionmaker() as db:
            task = Task(user_id=user["id"], project_id=project["id"], chapter_id=chapter["id"],
                        kind="chapter.generate", status="failed", input=payload)
            db.add(task)
            await db.commit()
            original = json.dumps(task.input, sort_keys=True)
            assert task_service.to_out(task).scene_id == expected_scene
            assert json.dumps(task.input, sort_keys=True) == original and task not in db.dirty
            task_id = task.id
        response = await client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        assert response.json()["scene_id"] == expected_scene
        async with app.state.sessionmaker() as db:
            stored = await db.get(Task, task_id)
            assert json.dumps(stored.input, sort_keys=True) == original
