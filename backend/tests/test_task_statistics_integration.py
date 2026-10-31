"""直接入队路径、暂停/取消与真实任务创建必须接入同一持久化统计。"""

import asyncio
from types import SimpleNamespace

import aiosqlite
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chapter, ChapterVersion, GenerationLog, Project, Task, TaskStatistic, User
from app.services import analysis_service, kb_index_service, retrieval_service, task_service
from app.services import task_statistics_service as stats
from tests.helpers import Gate, create_ai_config, create_project, register, wait_task


async def _content(app):
    async with app.state.sessionmaker() as db:
        owner = User(username="enqueue_owner", password_hash="x")
        db.add(owner)
        await db.flush()
        project = Project(user_id=owner.id, title="统计入口")
        db.add(project)
        await db.flush()
        chapter = Chapter(project_id=project.id, chapter_no=1, title="正文", content="章节内容", word_count=4)
        db.add(chapter)
        await db.commit()
        return owner.id, project.id, chapter.id


async def test_enqueue_pending_statistics_roll_back_without_own_commit(app):
    await app.state.task_engine.stop()
    _, pid, _ = await _content(app)
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, pid)
        ids = await analysis_service.enqueue_pending(db, project, ai_config_id=None)
        assert len(ids) == 1
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 1
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 0


async def test_direct_auto_analysis_and_memory_enqueue_record_new_tasks(app, monkeypatch):
    await app.state.task_engine.stop()
    _, pid, cid = await _content(app)
    app.state.settings.auto_analyze_enabled = True
    auto_id = await analysis_service.enqueue_auto(app.state.task_engine.ctx, pid, cid)
    assert auto_id is not None

    async def embedding(*args, **kwargs):
        return SimpleNamespace()

    async def stale(*args, **kwargs):
        return [object()]

    monkeypatch.setattr(retrieval_service, "default_embedding_config", embedding)
    monkeypatch.setattr(retrieval_service, "config_model_key", lambda cfg: "test-model")
    monkeypatch.setattr(retrieval_service, "stale_chunks", stale)
    memory_id = await kb_index_service.enqueue_memory(app.state.sessionmaker, pid)
    assert memory_id is not None and memory_id != auto_id
    async with app.state.sessionmaker() as db:
        records = list(await db.scalars(select(TaskStatistic)))
        assert {row.kind for row in records} == {"chapter.analyze", "kb.index"}
        assert all(row.origin == "new" and row.created_event_at is not None for row in records)
        assert len(records) == 2


@pytest.mark.parametrize("status", ["queued", "paused", "running"])
async def test_all_direct_cancel_paths_record_exact_terminal(app, status):
    await app.state.task_engine.stop()
    owner_id, _, _ = await _content(app)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner_id, kind="chapter.analyze", status=status)
        db.add(task)
        await stats.record_created(db, task)
        await db.commit()
        cancelled = await task_service.cancel_task(db, app.state.task_engine, owner_id, task.id)
        record = await db.get(TaskStatistic, task.statistics_key)
        assert cancelled.status == record.status == "cancelled"
        assert record.finished_event_at is not None
        assert record.finished_event_at == (await db.get(Task, task.id)).finished_at
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 1


async def test_resume_and_finish_paused_do_not_double_count_creation(app):
    await app.state.task_engine.stop()
    owner_id, _, _ = await _content(app)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner_id, kind="chapter.analyze", status="paused")
        db.add(task)
        await stats.record_created(db, task)
        await db.commit()
        resumed = await task_service.resume_task(db, app.state.task_engine, owner_id, task.id)
        assert resumed.status == "queued"
        task.status = "paused"
        await stats.record_state_change(db, task, "paused")
        await db.commit()
        ended = await task_service.finish_paused_task(db, app.state.task_engine, owner_id, task.id)
        record = await db.get(TaskStatistic, task.statistics_key)
        assert ended.status == record.status == "succeeded"
        assert record.finished_event_at is not None
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 1


async def test_statistics_integrity_error_is_not_misreported_as_resource_conflict(app, client, monkeypatch):
    await app.state.task_engine.stop()
    owner = await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "事务"})).json()

    async def fail(db, task):
        await db.flush()
        raise IntegrityError("INSERT statistics", {}, Exception("statistics storage failed"))

    monkeypatch.setattr(stats, "record_created", fail)
    with pytest.raises(IntegrityError):
        async with app.state.sessionmaker() as db:
            await task_service.create_task(
                db, app.state.task_engine, owner["id"], "chapter.generate", {"chapter_id": chapter["id"]}
            )
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter["id"])).status == "draft"
        assert await db.scalar(select(func.count(Task.id))) == 0
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 0


async def test_running_cancel_and_completion_race_keep_one_result(app, client, fake_llm):
    await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "竞争"})).json()
    gate = Gate()
    fake_llm.push(["开始", gate, "结束"])
    task = (await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})).json()
    await gate.wait_reached()
    cancel = asyncio.create_task(client.post(f"/api/v1/tasks/{task['id']}/cancel"))
    gate.release()
    response = await cancel
    assert response.status_code in (200, 409)
    ended = await wait_task(client, task["id"])
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
        record = await db.get(TaskStatistic, row.statistics_key)
        assert ended["status"] == record.status in ("succeeded", "cancelled")
        assert record.finished_event_at is not None
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(
            TaskStatistic.finished_event_at.is_not(None)
        )) == 1


@pytest.mark.parametrize("cancel_at", ["returning", "committed"])
async def test_running_cancel_drains_atomic_save_before_cleanup(app, client, fake_llm, monkeypatch, cancel_at):
    """精确取消活RETURNING游标或已提交事务：不留写锁，终态与提交事实一致。"""
    await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "落库取消"})).json()
    save_gate = Gate()
    stream_gate = Gate()
    cancel_sent = asyncio.Event()
    driver_cancelled = False
    original_execute = aiosqlite.Cursor.execute
    original_commit = AsyncSession.commit
    original_cancel = app.state.task_engine.cancel_running
    original_get = task_service.get_owned_task
    request_db = None

    async def execute(cursor, sql, parameters=None):
        nonlocal driver_cancelled
        result = await original_execute(cursor, sql, parameters)
        if cancel_at == "returning" and sql.startswith("INSERT INTO generation_logs"):
            # 真实INSERT已执行，RETURNING尚未被SQLAlchemy收尽，SQLite仍持写锁。
            try:
                save_gate.reached.set()
                await save_gate.released.wait()
            except asyncio.CancelledError:
                driver_cancelled = True
                raise
        return result

    async def commit(db):
        saving = any(isinstance(row, Chapter) and row.status == "done" for row in db.identity_map.values())
        await original_commit(db)
        if cancel_at == "committed" and saving:
            save_gate.reached.set()
            await save_gate.released.wait()

    async def get(db, *args):
        nonlocal request_db
        request_db = db
        return await original_get(db, *args)

    async def cancel(*args, **kwargs):
        # 不仅检查SQLite legacy模式的读操作；请求逻辑事务也必须结束后再等待执行体。
        assert request_db is not None and not request_db.in_transaction()
        waiting = asyncio.create_task(original_cancel(*args, **kwargs))
        await asyncio.sleep(0)
        assert args[0] in app.state.task_engine._cancel_requested
        cancel_sent.set()
        await waiting

    monkeypatch.setattr(aiosqlite.Cursor, "execute", execute)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(task_service, "get_owned_task", get)
    monkeypatch.setattr(app.state.task_engine, "cancel_running", cancel)
    fake_llm.push(["开始", stream_gate, "结束"])
    task = (await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})).json()
    await stream_gate.wait_reached()
    stream_gate.release()
    await save_gate.wait_reached()
    cancel_request = asyncio.create_task(client.post(f"/api/v1/tasks/{task['id']}/cancel"))
    try:
        await asyncio.wait_for(cancel_sent.wait(), timeout=2)
    finally:
        save_gate.release()
    response = await cancel_request
    assert response.status_code == 200
    ended = await wait_task(client, task["id"])
    expected = "cancelled" if cancel_at == "returning" else "succeeded"
    assert ended["status"] == expected
    assert not driver_cancelled
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
        record = await db.get(TaskStatistic, row.statistics_key)
        assert record.status == expected and record.finished_event_at == row.finished_at
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(
            TaskStatistic.finished_event_at.is_not(None)
        )) == 1
        saved = await db.get(Chapter, chapter["id"])
        assert saved.content == ("" if cancel_at == "returning" else "开始结束")
        assert saved.status == ("draft" if cancel_at == "returning" else "done")
        logs = list(await db.scalars(select(GenerationLog).where(GenerationLog.chapter_id == chapter["id"])))
        assert [log.status for log in logs] == ["cancelled" if cancel_at == "returning" else "success"]
        versions = list(await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter["id"])))
        assert bool(versions) == (cancel_at == "committed")
        # 独立新事务真实写入成功，不能靠丢弃统计或吞掉database locked通过。
        saved.title = "写锁已释放"
        await db.commit()
