"""直接入队路径、暂停/取消与真实任务创建必须接入同一持久化统计。"""

import asyncio
from types import SimpleNamespace

import aiosqlite
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chapter, ChapterVersion, GenerationLog, Project, Task, TaskStatistic, User
from app.services import analysis_service, generation_service, retrieval_service, task_service
from app.services import task_statistics_service as stats
from app.services.authorization_service import AuthenticatedActor
from tests.helpers import Gate, create_ai_config, create_project, register, task_params, wait_task


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


async def test_enqueue_pending_statistics_roll_back_without_own_commit(app, client, fake_llm, monkeypatch):
    """在真实拆书 analyze 事务中验证派生任务及统计一同回滚。"""
    from tests.test_book_import import DIGESTS, SYNTHESIZE, _setup, _start

    rec = await _setup(client)
    original = analysis_service.enqueue_pending
    observed = []

    async def rollback_once(db, project, **kwargs):
        execution = kwargs["execution"]
        assert execution.kind == "book.import" and execution.project_id == project.id
        before = await db.scalar(select(func.count(TaskStatistic.key)).where(TaskStatistic.kind == "chapter.analyze"))
        ids = await original(db, project, **kwargs)
        assert len(ids) == 1
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(
            TaskStatistic.kind == "chapter.analyze",
        )) == before + 1
        await db.rollback()
        async with app.state.sessionmaker() as verify:
            assert await verify.scalar(select(func.count(Task.id)).where(Task.id.in_(ids))) == 0
            assert await verify.scalar(select(func.count(TaskStatistic.key)).where(
                TaskStatistic.kind == "chapter.analyze",
            )) == before
        observed.append(ids[0])
        # 重新调用同一原身份，让生产路径正常提交，确保测试覆盖真正的调用方事务。
        current = await db.get(Project, execution.project_id)
        return await original(db, current, **kwargs)

    monkeypatch.setattr(analysis_service, "enqueue_pending", rollback_once)
    # 大型 JSON 只需一个测试分片，避免在并行回归中为每七字重复开事件事务。
    fake_llm.push([DIGESTS[0]])
    fake_llm.push([SYNTHESIZE])
    response = await _start(client, rec, import_range=[1, 1], reverse_range=[1, 1],
                            deep_analyze=True, extract_style=False)
    assert response.status_code == 202, response.text
    parent = response.json()["task"]
    result = await wait_task(client, parent, within=20)
    assert result["status"] == "succeeded", result
    assert len(observed) == 1
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.analyze")) == 1
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(
            TaskStatistic.kind == "chapter.analyze",
        )) == 1


async def test_direct_auto_analysis_and_memory_enqueue_record_new_tasks(app, client, fake_llm, monkeypatch):
    owner = await register(client, "enqueue_owner")
    await create_ai_config(client)
    project = await create_project(client)
    pid = project["id"]
    chapter = await client.post(f"/api/v1/projects/{pid}/chapters", json={"content": "章节内容"})
    assert chapter.status_code == 201, chapter.text
    cid = chapter.json()["id"]
    app.state.settings.auto_analyze_enabled = True
    app.state.settings.task_max_per_user = 1
    original_enqueue = analysis_service.enqueue_auto
    captured: asyncio.Queue = asyncio.Queue()
    release = asyncio.Event()

    async def held_enqueue(ctx, project_id, chapter_id, *, execution):
        await captured.put(execution)
        await release.wait()
        return await original_enqueue(ctx, project_id, chapter_id, execution=execution)

    monkeypatch.setattr(analysis_service, "enqueue_auto", held_enqueue)
    fake_llm.push("生成章节正文")
    response = await client.post(f"/api/v1/chapters/{cid}/generate")
    assert response.status_code == 202, response.text
    source = response.json()
    execution = await asyncio.wait_for(captured.get(), 10)
    assert execution.task_key == source["task_key"]

    async def embedding(*args, **kwargs):
        return SimpleNamespace()

    async def stale(*args, **kwargs):
        return [object()]

    try:
        auto_id = await original_enqueue(app.state.task_engine.ctx, pid, cid, execution=execution)
        assert auto_id is not None
        monkeypatch.setattr(retrieval_service, "default_embedding_config", embedding)
        monkeypatch.setattr(retrieval_service, "config_model_key", lambda cfg: "test-model")
        monkeypatch.setattr(retrieval_service, "stale_chunks", stale)
        # 走真实 Cookie 认证和人工写入口，使派生任务捕获请求中的原始 actor 快照。
        response = await client.post(f"/api/v1/projects/{pid}/memories", json={"chapter_no": 1, "content": "剧情记忆"})
        assert response.status_code == 201, response.text
        async with app.state.sessionmaker() as db:
            memory_task = await db.scalar(select(Task).where(Task.kind == "kb.index"))
            assert memory_task is not None and memory_task.user_id == owner["id"]
            memory_id = memory_task.id
        assert memory_id is not None and memory_id != auto_id
        async with app.state.sessionmaker() as db:
            records = list(await db.scalars(select(TaskStatistic)))
            derived = [row for row in records if row.kind in {"chapter.analyze", "kb.index"}]
            assert {row.kind for row in derived} == {"chapter.analyze", "kb.index"}
            assert all(row.origin == "new" and row.created_event_at is not None for row in derived)
            assert len(derived) == 2
            assert any(row.key == source["task_key"] and row.kind == "chapter.generate" for row in records)
    finally:
        release.set()


@pytest.mark.parametrize("status", ["queued", "paused", "running"])
async def test_all_direct_cancel_paths_record_exact_terminal(app, status):
    await app.state.task_engine.stop()
    owner_id, _, _ = await _content(app)
    # 遗留 running 的 chapter.analyze 须先核对原执行收据才可取消（失败关闭）；无执行体的运行中
    # 取消路径改用普通任务类型，排队/待审核仍覆盖分析任务。
    kind = "outline.continue" if status == "running" else "chapter.analyze"
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner_id, kind=kind, status=status)
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
            # 与真实路由一致：受理需请求认证快照，章节任务经中央模型预检入口创建。
            token_version = (await db.get(User, owner["id"])).token_version
            db.info["authenticated_actor"] = AuthenticatedActor(owner["id"], token_version)
            await generation_service.create_chapter_task(
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
    cancel = asyncio.create_task(client.post(f"/api/v1/tasks/{task['id']}/cancel", params=task_params(task)))
    gate.release()
    response = await cancel
    assert response.status_code in (200, 409)
    ended = await wait_task(client, task)
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
    """取消与活RETURNING游标或已提交事务竞争：不留写锁，终态与提交事实一致。"""
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

    async def get(db, *args, **kwargs):
        nonlocal request_db
        request_db = db
        return await original_get(db, *args, **kwargs)

    async def cancel(*args, **kwargs):
        # 不仅检查SQLite legacy模式的读操作；请求逻辑事务也必须结束后再等待执行体。
        assert request_db is not None and not request_db.in_transaction()
        waiting = asyncio.create_task(original_cancel(*args, **kwargs))
        await asyncio.sleep(0)
        if cancel_at == "committed":
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
    cancel_request = asyncio.create_task(client.post(f"/api/v1/tasks/{task['id']}/cancel", params=task_params(task)))
    try:
        if cancel_at == "returning":
            # 协作改造后本人取消须先取得共同授权门禁并重验作品身份；原子保存在写事务内持有同一
            # 门禁，取消只能排在保存之后，不能打断活 RETURNING 游标。
            for _ in range(50):
                await asyncio.sleep(0)
            assert not cancel_sent.is_set() and not cancel_request.done()
        else:
            await asyncio.wait_for(cancel_sent.wait(), timeout=2)
    finally:
        save_gate.release()
    response = await cancel_request
    assert response.status_code in ((200, 409) if cancel_at == "returning" else (200,)), response.text
    ended = await wait_task(client, task)
    # 两种窗口下保存都已提交：保留真实 succeeded 赢家（见 error-handling.md 任务 SSE 契约）。
    expected = "succeeded"
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
        assert saved.content == "开始结束"
        assert saved.status == "done"
        logs = list(await db.scalars(select(GenerationLog).where(GenerationLog.chapter_id == chapter["id"])))
        assert [log.status for log in logs] == ["success"]
        versions = list(await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter["id"])))
        assert versions
        # 独立新事务真实写入成功，不能靠丢弃统计或吞掉database locked通过。
        saved.title = "写锁已释放"
        await db.commit()
