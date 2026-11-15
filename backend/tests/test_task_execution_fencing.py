"""任务领取、知识库占用和后台收尾的持久执行身份。"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KbDocument, Task, User
from app.services import kb_index_service, task_statistics_service
from app.services.task_engine.hub import TaskHub
from tests.helpers import Gate, create_ai_config, create_project, register, task_params, wait_task, wait_until


async def _queued_task(app, client):
    engine = app.state.task_engine
    await engine.stop()
    user = await register(client, "execution_owner")
    async with app.state.sessionmaker() as db:
        # 领取只信任受理时冻结的原认证版本，不能从当前用户补签。
        token_version = (await db.get(User, user["id"])).token_version
        task = Task(user_id=user["id"], kind="chapter.analyze", status="queued", queued_at=datetime.now(UTC),
                    input={"_task_actor_token_version": token_version})
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
        await db.refresh(task)
        return task.id, task.statistics_key, task.queued_at


async def test_claim_confirmation_loss_never_adopts_another_execution(app, client, monkeypatch):
    task_id, task_key, queued_at = await _queued_task(app, client)
    engine, factory = app.state.task_engine, app.state.sessionmaker
    original_commit = AsyncSession.commit
    competing_key = str(uuid4())
    lost = False
    started = []

    async def lose_and_compete(db):
        nonlocal lost
        if not lost and await db.scalar(select(Task.status).where(Task.id == task_id)) == "running":
            lost = True
            # 本领取并未提交；释放它的事务后由另一执行体真正提交相同任务与入队轮次。
            await db.rollback()
            async with factory() as competing:
                await competing.execute(update(Task).where(Task.id == task_id).values(
                    status="running", execution_key=competing_key,
                ))
                await original_commit(competing)
            raise RuntimeError("claim acknowledgement lost")
        await original_commit(db)

    async def run(*args):
        started.append(args)

    monkeypatch.setattr(AsyncSession, "commit", lose_and_compete)
    monkeypatch.setattr(engine, "_run", run)
    monkeypatch.setattr(engine, "_stopping", False)
    engine._running_rounds[task_id] = queued_at
    await engine._claim_and_run(task_id, task_key)
    assert lost and not started
    async with factory() as db:
        row = await db.get(Task, task_id)
        assert (row.status, row.execution_key) == ("running", competing_key)


async def test_claim_confirmation_loss_accepts_its_own_committed_execution(app, client, monkeypatch):
    task_id, task_key, queued_at = await _queued_task(app, client)
    engine, factory = app.state.task_engine, app.state.sessionmaker
    original_commit = AsyncSession.commit
    lost = False
    started = []

    async def commit_then_lose(db):
        nonlocal lost
        if not lost and await db.scalar(select(Task.status).where(Task.id == task_id)) == "running":
            lost = True
            await original_commit(db)
            raise RuntimeError("claim acknowledgement lost")
        await original_commit(db)

    async def run(*args):
        started.append(args)

    monkeypatch.setattr(AsyncSession, "commit", commit_then_lose)
    monkeypatch.setattr(engine, "_run", run)
    monkeypatch.setattr(engine, "_stopping", False)
    engine._running_rounds[task_id] = queued_at
    await engine._claim_and_run(task_id, task_key)
    async with factory() as db:
        row = await db.get(Task, task_id)
        assert lost and len(started) == 1
        assert row.status == "running" and row.execution_key
        assert started[0][1].execution_key == row.execution_key


async def test_kb_upload_binds_queued_claim_in_task_transaction(app, client):
    await app.state.task_engine.stop()
    await register(client, "index_execution_owner")
    project = await create_project(client)
    response = await client.post(
        f"/api/v1/projects/{project['id']}/kb/documents",
        files={"file": ("book.txt", "安全测试资料".encode(), "text/plain")},
    )
    assert response.status_code == 202, response.text
    data = response.json()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, data["task"]["id"])
        document = await db.get(KbDocument, data["document"]["id"])
        assert task.status == "queued"
        assert document.status == "pending"
        assert document.index_task_key == task.statistics_key
        assert document.index_task_queued_at == task.queued_at
        assert document.index_claim_key and document.index_claim_key != task.statistics_key
        assert task.execution_key is None


async def test_owner_index_success_clears_document_and_execution_claims(app, client):
    await register(client, "index_complete_owner")
    project = await create_project(client)
    response = await client.post(
        f"/api/v1/projects/{project['id']}/kb/documents",
        files={"file": ("book.txt", "可以检索的正文".encode(), "text/plain")},
    )
    assert response.status_code == 202, response.text
    task_ref = response.json()["task"]
    task_id, document_id = task_ref["id"], response.json()["document"]["id"]
    result = await wait_task(client, task_ref)
    assert result["status"] == "succeeded"
    await wait_until(lambda: task_id not in app.state.task_engine._running)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        doc = await db.get(KbDocument, document_id)
        assert task.execution_key is None and doc.status in ("ready", "ready_keyword_only")
        assert (doc.index_task_key, doc.index_task_queued_at, doc.index_claim_key) == (None, None, None)


async def test_queued_document_cancel_releases_claim_in_terminal_transaction(app, client):
    await app.state.task_engine.stop()
    await register(client, "index_cancel_owner")
    project = await create_project(client)
    response = await client.post(
        f"/api/v1/projects/{project['id']}/kb/documents",
        files={"file": ("book.txt", "正文".encode(), "text/plain")},
    )
    task_ref = response.json()["task"]
    task_id, document_id = task_ref["id"], response.json()["document"]["id"]
    cancelled = await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task_ref))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        doc = await db.get(KbDocument, document_id)
        assert task.status == "cancelled" and task.execution_key is None and doc.status == "failed"
        assert (doc.index_task_key, doc.index_task_queued_at, doc.index_claim_key) == (None, None, None)


async def test_kb_claim_failure_rolls_back_task_running_transition(app, client, monkeypatch):
    await app.state.task_engine.stop()
    await register(client, "index_claim_rollback")
    project = await create_project(client)
    response = await client.post(
        f"/api/v1/projects/{project['id']}/kb/documents",
        files={"file": ("book.txt", "正文".encode(), "text/plain")},
    )
    payload = response.json()
    task_ref = payload["task"]
    task_id, document_id = task_ref["id"], payload["document"]["id"]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        doc = await db.get(KbDocument, document_id)
        task_key, queued_at, claim = task.statistics_key, task.queued_at, doc.index_claim_key

    async def fail(*_args, **_kwargs):
        raise RuntimeError("模拟占用轮换失败")

    monkeypatch.setattr(kb_index_service, "activate_document_claim", fail)
    engine = app.state.task_engine
    engine._stopping = False
    engine._running_rounds[task_id] = queued_at
    with pytest.raises(RuntimeError, match="占用轮换失败"):
        await engine._claim_and_run(task_id, task_key)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        doc = await db.get(KbDocument, document_id)
        assert (task.status, task.execution_key, doc.index_claim_key) == ("queued", None, claim)


async def _legacy_unclaimed_upload(app, client, username):
    await app.state.task_engine.stop()
    await register(client, username)
    project = await create_project(client)
    response = await client.post(
        f"/api/v1/projects/{project['id']}/kb/documents",
        files={"file": ("book.txt", "正文".encode(), "text/plain")},
    )
    payload = response.json()
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, payload["document"]["id"])
        # 模拟 0045 前入队：文档侧没有占用三列。
        doc.index_task_key = doc.index_task_queued_at = doc.index_claim_key = None
        await db.commit()
    return project, payload["task"], payload["document"]["id"]


async def test_legacy_unclaimed_document_cancel_fails_closed_without_claim_proof(app, client):
    # E4-e：可证明无执行体时取消走受限系统清理（任务 cancelled、文档 failed）；旧任务本身仍不能重试。
    project, task_ref, document_id = await _legacy_unclaimed_upload(app, client, "index_legacy_recovery")
    task_id = task_ref["id"]
    cancelled = await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task_ref))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
    assert cancelled.json()["error"] == kb_index_service.LEGACY_TASK_MESSAGES["cancelled"]
    async with app.state.sessionmaker() as db:
        task, doc = await db.get(Task, task_id), await db.get(KbDocument, document_id)
        assert task.status == "cancelled" and task.execution_key is None
        assert (doc.status, doc.error) == ("failed", kb_index_service.LEGACY_DOCUMENT_ERROR)
        assert doc.index_task_key is None and doc.index_claim_key is None and doc.text == "正文"
    retried = await client.post(f"/api/v1/tasks/{task_id}/retry", params=task_params(task_ref))
    assert retried.status_code == 409 and retried.json()["code"] == "kb_legacy_task_not_retryable", retried.text
    reindexed = await client.post(f"/api/v1/projects/{project['id']}/kb/documents/{document_id}/reindex")
    assert reindexed.status_code == 202, reindexed.text
    async with app.state.sessionmaker() as db:
        new_task, doc = await db.get(Task, reindexed.json()["id"]), await db.get(KbDocument, document_id)
        assert (doc.status, doc.index_task_key) == ("pending", new_task.statistics_key) and doc.index_claim_key


async def test_legacy_unclaimed_document_cancel_still_fails_closed_when_unproven(app, client):
    # 文档已不是 pending（无法证明没有其他写入者）：保持原失败关闭。
    _, task_ref, document_id = await _legacy_unclaimed_upload(app, client, "index_legacy_unproven")
    task_id = task_ref["id"]
    async with app.state.sessionmaker() as db:
        (await db.get(KbDocument, document_id)).status = "ready_keyword_only"
        await db.commit()
    cancelled = await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task_ref))
    assert cancelled.status_code == 409 and cancelled.json()["code"] == "task_cleanup_unproven", cancelled.text
    async with app.state.sessionmaker() as db:
        task, doc = await db.get(Task, task_id), await db.get(KbDocument, document_id)
        assert task.status == "queued" and task.execution_key is None
        assert doc.status == "ready_keyword_only" and doc.index_task_key is None and doc.index_claim_key is None
    retried = await client.post(f"/api/v1/tasks/{task_id}/retry", params=task_params(task_ref))
    assert retried.status_code == 409, retried.text


async def test_old_kb_abort_cannot_fail_newer_document_claim(app, client):
    engine = app.state.task_engine
    await engine.stop()
    user = await register(client, "index_old_execution")
    project = await create_project(client)
    factory = app.state.sessionmaker
    async with factory() as db:
        document = KbDocument(project_id=project["id"], filename="book.txt", sha256="b" * 64, text="正文")
        db.add(document)
        await db.flush()
        request = {"project_id": project["id"], "document_id": document.id, "mode": "document"}
        old = Task(user_id=user["id"], project_id=project["id"], kind="kb.index", status="queued",
                   resource_key=f"kbdoc:{document.id}", input={"request": request})
        db.add(old)
        await db.flush()
        await kb_index_service.bind_queued_document_claim(db, old.id, old.statistics_key, queue_claim_key=str(uuid4()))
        old.status = "cancelled"
        await db.flush()
        newer = Task(user_id=user["id"], project_id=project["id"], kind="kb.index", status="queued",
                     resource_key=f"kbdoc:{document.id}", input={"request": request})
        db.add(newer)
        await db.flush()
        current = await kb_index_service.bind_queued_document_claim(
            db, newer.id, newer.statistics_key, queue_claim_key=str(uuid4()),
        )
        await db.commit()
        document_id = document.id

    await kb_index_service.on_abort(factory, old)
    async with factory() as db:
        document = await db.get(KbDocument, document_id)
        assert (document.status, document.index_task_key, document.index_claim_key) == (
            "pending", current.task_key, current.claim_key,
        )


async def test_stale_runner_cannot_flush_or_complete_newer_execution(app, client):
    task_id, task_key, queued_at = await _queued_task(app, client)
    factory, engine = app.state.sessionmaker, app.state.task_engine
    old_key, new_key = str(uuid4()), str(uuid4())
    async with factory() as db:
        task = await db.get(Task, task_id)
        task.status, task.execution_key, task.output_text = "running", old_key, "原输出"
        await db.commit()
    async with factory() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(execution_key=new_key, output_text="新输出"))
        await db.commit()
    hub = TaskHub(task_id, statistics_key=task_key, queued_at=queued_at, execution_key=old_key, status="running")
    hub.append("旧输出")
    await engine._flush(hub)
    _, committed = await engine.complete_state(
        task, expected_status="running", status="failed", result=None, error="旧失败",
        text="旧输出", progress=10, abort_ok=True,
    )
    assert not committed
    async with factory() as db:
        current = await db.get(Task, task_id)
        assert (current.status, current.execution_key, current.output_text) == ("running", new_key, "新输出")


async def test_success_hook_keeps_finished_task_until_hook_returns(app, client, fake_llm, monkeypatch):
    await register(client, "success_hook_owner")
    project = await create_project(client)
    await create_ai_config(client)
    chapter = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
    assert chapter.status_code == 201, chapter.text
    factory, engine = app.state.sessionmaker, app.state.task_engine
    original_success = engine._on_success
    entered, release = asyncio.Event(), asyncio.Event()
    generation = Gate()

    async def on_success(handler, task, *, execution=None):
        assert execution is not None and execution.task_key == task_ref["task_key"]
        entered.set()
        await release.wait()
        await original_success(handler, task, execution=execution)

    monkeypatch.setattr(engine, "_on_success", on_success)
    fake_llm.push([generation, "真实生成正文"])
    accepted = await client.post(f"/api/v1/chapters/{chapter.json()['id']}/generate")
    assert accepted.status_code == 202, accepted.text
    task_ref = accepted.json()
    task_id = task_ref["id"]
    try:
        await generation.wait_reached(within=10)
        generation.release()
        await asyncio.wait_for(entered.wait(), 10)
        async with factory() as db:
            current = await db.get(Task, task_id)
            assert current.status == "succeeded" and current.execution_key is not None
        assert (await client.delete(f"/api/v1/tasks/{task_id}", params=task_params(task_ref))).status_code == 409
        assert (await client.delete("/api/v1/tasks", params={"finished": "true"})).json()["deleted"] == 0
    finally:
        generation.release()
        release.set()
        await wait_until(lambda: task_id not in engine._running, within=10)
    async with factory() as db:
        current = await db.get(Task, task_id)
        assert current.execution_key is None
    assert (await client.delete(f"/api/v1/tasks/{task_id}", params=task_params(task_ref))).status_code == 204
