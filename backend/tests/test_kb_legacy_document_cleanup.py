"""E4-e：0045 前入队、文档侧没有占用的旧 kb.index 文档任务的受限系统清理（SQLite）。

可证明无执行体时：引擎领取记 failed、用户取消记 cancelled，文档 CAS 为 failed；不写正文/分块/向量、零模型调用。
任一证明条件不成立时保持原失败关闭（排队不动、取消 409 task_cleanup_unproven）。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.models import KbChunk, KbDocument, RetrievalChunk, Task, User
from app.models.dashboard import TaskStatistic
from app.services import kb_index_service, project_lifecycle, task_statistics_service
from tests.helpers import create_project, register, task_params, wait_task, wait_until

FAILED = kb_index_service.LEGACY_TASK_MESSAGES["failed"]
CANCELLED = kb_index_service.LEGACY_TASK_MESSAGES["cancelled"]


async def make_legacy(app, client, username="legacy_owner", *, user=None, project=None, document_project=None):
    """直接构造 0045 前形态：文档 pending 且三列占用为空，排队任务没有执行键；附带既有分块与向量。"""
    await app.state.task_engine.stop()
    if project is None:
        user = await register(client, username)
        project = await create_project(client)
    doc_pid = document_project or project["id"]
    async with app.state.sessionmaker() as db:
        document = KbDocument(project_id=doc_pid, filename="历史.md", mime="text/markdown", sha256="0" * 64,
                              text="旧文档正文", status="pending", chunk_count=1)
        db.add(document)
        await db.flush()
        chunk = KbChunk(document_id=document.id, seq=0, content="旧分块正文")
        db.add(chunk)
        await db.flush()
        db.add(RetrievalChunk(project_id=doc_pid, source="kb", ref_id=chunk.id, text="旧分块正文",
                              embedding=b"\x01\x02\x03\x04", embedding_key="old-model"))
        token_version = (await db.get(User, user["id"])).token_version
        task = Task(user_id=user["id"], project_id=project["id"], kind="kb.index", status="queued",
                    resource_key=f"kbdoc:{document.id}", serial_key=kb_index_service.serial_key(project["id"]),
                    input={"request": {"project_id": project["id"], "mode": "document",
                                       "document_id": document.id}, "document_id": document.id,
                           "_task_actor_token_version": token_version})
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
    async with app.state.sessionmaker() as db:
        # 引擎按数据库读回的入队轮次比较（SQLite 读回为无时区值），不能用内存中的默认值。
        task = await db.get(Task, task.id)
        return {
            "user": user, "project": project, "document_id": document.id, "task_id": task.id,
            "task_key": task.statistics_key, "queued_at": task.queued_at,
        }


async def content_snapshot(app, document_id):
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, document_id)
        chunks = [(c.id, c.seq, c.content) for c in await db.scalars(
            select(KbChunk).where(KbChunk.document_id == document_id).order_by(KbChunk.id))]
        vectors = [(r.id, r.text, r.embedding, r.embedding_key, r.version) for r in await db.scalars(
            select(RetrievalChunk).where(RetrievalChunk.source == "kb").order_by(RetrievalChunk.id))]
        return doc.text, doc.chunk_count, chunks, vectors


async def state(app, legacy):
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, legacy["task_id"])
        doc = await db.get(KbDocument, legacy["document_id"])
        return task, doc


async def claim_once(app, legacy):
    engine = app.state.task_engine
    engine._stopping = False
    engine._running_rounds[legacy["task_id"]] = legacy["queued_at"]
    try:
        await engine._claim_and_run(legacy["task_id"], legacy["task_key"])
    finally:
        engine._running_rounds.pop(legacy["task_id"], None)
        engine._stopping = True


def ref(legacy):
    return {"id": legacy["task_id"], "task_key": legacy["task_key"]}


async def test_engine_claim_cleans_up_legacy_task_and_reindex_creates_claimed_task(app, client, fake_llm):
    legacy = await make_legacy(app, client)
    before = await content_snapshot(app, legacy["document_id"])
    engine = app.state.task_engine
    await engine.start()
    finished = await wait_task(client, ref(legacy))
    assert finished["status"] == "failed" and finished["error"] == FAILED
    task, doc = await state(app, legacy)
    assert task.execution_key is None and task.started_at is None
    assert task.input[kb_index_service.LEGACY_CLEANUP_KEY] is True
    assert (doc.status, doc.error) == ("failed", kb_index_service.LEGACY_DOCUMENT_ERROR)
    assert (doc.index_task_key, doc.index_task_queued_at, doc.index_claim_key) == (None, None, None)
    assert await content_snapshot(app, legacy["document_id"]) == before
    assert not fake_llm.calls and not fake_llm.embed_calls
    async with app.state.sessionmaker() as db:
        stat = await db.get(TaskStatistic, legacy["task_key"])
        assert stat.status == "failed" and stat.finished_event_at is not None
    # 失败后不再被调度（无热循环），旧任务本身不能重试。
    assert legacy["task_id"] not in engine._running
    retried = await client.post(f"/api/v1/tasks/{legacy['task_id']}/retry", params=task_params(ref(legacy)))
    assert retried.status_code == 409 and retried.json()["code"] == "kb_legacy_task_not_retryable", retried.text
    # 作者重新索引：新受理建立带占用的新任务并正常完成。
    pid = legacy["project"]["id"]
    await engine.stop()
    reindexed = await client.post(f"/api/v1/projects/{pid}/kb/documents/{legacy['document_id']}/reindex")
    assert reindexed.status_code == 202, reindexed.text
    new_ref = reindexed.json()
    async with app.state.sessionmaker() as db:
        new_task = await db.get(Task, new_ref["id"])
        doc = await db.get(KbDocument, legacy["document_id"])
        assert new_task.status == "queued" and kb_index_service.LEGACY_CLEANUP_KEY not in new_task.input
        assert (doc.status, doc.index_task_key, doc.index_task_queued_at) == (
            "pending", new_task.statistics_key, new_task.queued_at,
        )
        assert doc.index_claim_key
    await engine.start()
    assert (await wait_task(client, new_ref))["status"] == "succeeded"
    _, doc = await state(app, legacy)
    assert doc.status in ("ready", "ready_keyword_only") and doc.index_claim_key is None


async def test_user_cancel_cleans_up_legacy_task_as_cancelled(app, client, fake_llm):
    legacy = await make_legacy(app, client)
    before = await content_snapshot(app, legacy["document_id"])
    cancelled = await client.post(f"/api/v1/tasks/{legacy['task_id']}/cancel", params=task_params(ref(legacy)))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled" and cancelled.json()["error"] == CANCELLED
    task, doc = await state(app, legacy)
    assert task.status == "cancelled" and task.execution_key is None and task.finished_at is not None
    assert (doc.status, doc.error) == ("failed", kb_index_service.LEGACY_DOCUMENT_ERROR)
    assert await content_snapshot(app, legacy["document_id"]) == before
    assert not fake_llm.calls and not fake_llm.embed_calls
    async with app.state.sessionmaker() as db:
        assert (await db.get(TaskStatistic, legacy["task_key"])).status == "cancelled"


def _claimed(doc, legacy):
    doc.index_task_key, doc.index_task_queued_at, doc.index_claim_key = str(uuid4()), datetime.now(UTC), str(uuid4())


def _not_pending(doc, legacy):
    doc.status = "ready_keyword_only"


NEGATIVE_DOC = {"document_claimed": _claimed, "document_not_pending": _not_pending}


async def _apply(app, legacy, case):
    async with app.state.sessionmaker() as db:
        task, doc = await db.get(Task, legacy["task_id"]), await db.get(KbDocument, legacy["document_id"])
        if case in NEGATIVE_DOC:
            NEGATIVE_DOC[case](doc, legacy)
        elif case == "other_open_task_by_request":
            # resource_key 不同（避开活动唯一索引），但请求指向同一文档。
            db.add(Task(user_id=task.user_id, project_id=task.project_id, kind="kb.index", status="paused",
                        input={"request": {"project_id": task.project_id, "mode": "document",
                                           "document_id": doc.id}}))
        elif case == "other_task_cleanup_pending":
            # 终态但执行键未清（仍在安全收尾），同样算未结束。
            db.add(Task(user_id=task.user_id, project_id=task.project_id, kind="kb.index", status="failed",
                        resource_key=f"kbdoc:{doc.id}", execution_key=str(uuid4()),
                        input={"request": {"project_id": task.project_id, "mode": "document",
                                           "document_id": doc.id}}))
        elif case == "admin_cancel_intent":
            task.admin_cancel_operation_id, task.admin_cancel_requested_at = str(uuid4()), datetime.now(UTC)
        elif case == "execution_key_set":
            task.execution_key = str(uuid4())
        elif case == "resource_key_mismatch":
            task.resource_key = f"kbdoc:{doc.id + 1000}"
        else:
            raise AssertionError(case)
        await db.commit()


CASES = ["document_claimed", "document_not_pending", "other_open_task_by_request", "other_task_cleanup_pending",
         "admin_cancel_intent", "execution_key_set", "resource_key_mismatch"]


@pytest.mark.parametrize("case", CASES)
async def test_unproven_legacy_task_keeps_original_barrier(app, client, fake_llm, case):
    legacy = await make_legacy(app, client)
    await _apply(app, legacy, case)
    task_before, doc_before = await state(app, legacy)
    before = await content_snapshot(app, legacy["document_id"])
    await claim_once(app, legacy)
    # 共用函数在锁内同样拒绝（覆盖调用方形状检查之外的 SQL 条件）。
    async with app.state.sessionmaker() as db:
        await project_lifecycle.lock_record(db, legacy["project"]["id"])
        task = await db.get(Task, legacy["task_id"])
        captured = kb_index_service.LegacyDocumentTask(
            legacy["task_id"], legacy["task_key"], task.user_id, legacy["project"]["id"], legacy["document_id"],
            kb_index_service._utc(legacy["queued_at"]),
        )
        assert await kb_index_service.cleanup_legacy_unclaimed_document_task(db, captured, status="failed") is None
        await db.rollback()
    if case not in ("admin_cancel_intent", "execution_key_set"):
        cancelled = await client.post(f"/api/v1/tasks/{legacy['task_id']}/cancel", params=task_params(ref(legacy)))
        assert cancelled.status_code == 409 and cancelled.json()["code"] == "task_cleanup_unproven", cancelled.text
    task, doc = await state(app, legacy)
    assert (task.status, task.execution_key, task.error) == (
        task_before.status, task_before.execution_key, task_before.error,
    ) and task.status == "queued"
    assert kb_index_service.LEGACY_CLEANUP_KEY not in task.input
    assert (doc.status, doc.error, doc.index_task_key, doc.index_claim_key) == (
        doc_before.status, doc_before.error, doc_before.index_task_key, doc_before.index_claim_key,
    )
    assert await content_snapshot(app, legacy["document_id"]) == before
    assert not fake_llm.calls and not fake_llm.embed_calls


async def test_cross_project_document_is_not_cleaned(app, client, fake_llm):
    await app.state.task_engine.stop()
    user = await register(client, "legacy_cross")
    other = await create_project(client)
    project = await create_project(client)
    legacy = await make_legacy(app, client, user=user, project=project, document_project=other["id"])
    await claim_once(app, legacy)
    cancelled = await client.post(f"/api/v1/tasks/{legacy['task_id']}/cancel", params=task_params(ref(legacy)))
    assert cancelled.status_code == 409 and cancelled.json()["code"] == "task_cleanup_unproven", cancelled.text
    task, doc = await state(app, legacy)
    assert task.status == "queued" and doc.status == "pending" and doc.project_id == other["id"]


async def test_captured_round_mismatch_is_not_cleaned(app, client):
    legacy = await make_legacy(app, client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, legacy["task_id"])
        stale = legacy["queued_at"] - timedelta(seconds=5)
        assert kb_index_service.capture_legacy_document_task(task, task_key=legacy["task_key"],
                                                             queued_at=stale) is None
        assert kb_index_service.capture_legacy_document_task(task, task_key=str(uuid4()),
                                                             queued_at=legacy["queued_at"]) is None
        # 读取后被重新入队（新轮次）：按原轮次捕获的身份在锁内不再匹配。
        captured = kb_index_service.capture_legacy_document_task(task, task_key=legacy["task_key"],
                                                                 queued_at=legacy["queued_at"])
        await db.execute(update(Task).where(Task.id == task.id).values(queued_at=datetime.now(UTC) + timedelta(1)))
        await project_lifecycle.lock_record(db, legacy["project"]["id"])
        assert await kb_index_service.cleanup_legacy_unclaimed_document_task(db, captured, status="failed") is None
        await db.rollback()


@pytest.mark.parametrize("flag", ["stopping", "cancel_requested"])
async def test_engine_does_not_clean_up_when_stopping_or_cancel_requested(app, client, monkeypatch, flag):
    legacy = await make_legacy(app, client)
    engine = app.state.task_engine
    original = kb_index_service.queued_document_claim

    async def flip(db, task):
        # 领取已越过入口检查、在锁内时才发生关停或登记取消意图。
        if flag == "stopping":
            engine._stopping = True
        else:
            engine._cancel_requested.add(legacy["task_id"])
        return await original(db, task)

    monkeypatch.setattr(kb_index_service, "queued_document_claim", flip)
    try:
        await claim_once(app, legacy)
    finally:
        engine._cancel_requested.discard(legacy["task_id"])
    task, doc = await state(app, legacy)
    assert task.status == "queued" and doc.status == "pending"


async def test_cleanup_and_new_admission_race(app, client):
    legacy = await make_legacy(app, client)
    pid = legacy["project"]["id"]
    # 旧任务仍占用资源键时新受理被拒且不清空分块。
    early = await client.post(f"/api/v1/projects/{pid}/kb/documents/{legacy['document_id']}/reindex")
    assert early.status_code == 409, early.text
    before = await content_snapshot(app, legacy["document_id"])
    cancel, reindex = await asyncio.gather(
        client.post(f"/api/v1/tasks/{legacy['task_id']}/cancel", params=task_params(ref(legacy))),
        client.post(f"/api/v1/projects/{pid}/kb/documents/{legacy['document_id']}/reindex"),
    )
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancelled", cancel.text
    assert reindex.status_code in (202, 409), reindex.text
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, legacy["document_id"])
        open_tasks = list(await db.scalars(select(Task).where(
            Task.kind == "kb.index", Task.status.in_(("queued", "running", "paused")),
        )))
        if reindex.status_code == 202:
            assert [t.id for t in open_tasks] == [reindex.json()["id"]]
            assert (doc.status, doc.index_task_key) == ("pending", open_tasks[0].statistics_key)
        else:
            assert open_tasks == [] and doc.status == "failed" and doc.index_task_key is None
            assert await content_snapshot(app, legacy["document_id"]) == before
    if reindex.status_code == 409:
        again = await client.post(f"/api/v1/projects/{pid}/kb/documents/{legacy['document_id']}/reindex")
        assert again.status_code == 202, again.text


async def test_direct_engine_claim_cleans_up_legacy_task(app, client, fake_llm):
    legacy = await make_legacy(app, client)
    await claim_once(app, legacy)
    task, doc = await state(app, legacy)
    assert (task.status, task.error, doc.status) == ("failed", FAILED, "failed")
    assert not fake_llm.calls and not fake_llm.embed_calls


async def test_engine_cleanup_after_user_cancel_is_noop(app, client):
    legacy = await make_legacy(app, client)
    cancelled = await client.post(f"/api/v1/tasks/{legacy['task_id']}/cancel", params=task_params(ref(legacy)))
    assert cancelled.status_code == 200
    await claim_once(app, legacy)
    task, _ = await state(app, legacy)
    assert task.status == "cancelled" and task.error == CANCELLED
    await wait_until(lambda: legacy["task_id"] not in app.state.task_engine._running)


async def test_null_queued_at_legacy_task_fails_closed(app, client, fake_llm):
    """0026 前没有入队时钟的更早任务无法按轮次 CAS：引擎与取消都保持原屏障。"""
    legacy = await make_legacy(app, client)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == legacy["task_id"]).values(queued_at=None))
        await db.commit()
    legacy["queued_at"] = None
    _, doc_before = await state(app, legacy)
    before = await content_snapshot(app, legacy["document_id"])
    await claim_once(app, legacy)
    cancelled = await client.post(f"/api/v1/tasks/{legacy['task_id']}/cancel", params=task_params(ref(legacy)))
    assert cancelled.status_code == 409, cancelled.text
    task, doc = await state(app, legacy)
    assert task.status == "queued" and task.execution_key is None and task.queued_at is None
    assert kb_index_service.LEGACY_CLEANUP_KEY not in task.input
    assert (doc.status, doc.error) == ("pending", doc_before.error)
    assert await content_snapshot(app, legacy["document_id"]) == before
    assert not fake_llm.calls and not fake_llm.embed_calls


async def test_unproven_legacy_task_is_not_hot_looped_and_retries_on_notify(app, client, fake_llm):
    """不可证明的旧任务不会因自身执行体结束而被立即重新领取；外部 notify 后按数据库事实重试。"""
    legacy = await make_legacy(app, client)
    await _apply(app, legacy, "document_claimed")
    engine = app.state.task_engine
    claims: list[int] = []
    original = engine._claim_and_run

    async def counting(task_id, statistics_key):
        claims.append(task_id)
        return await original(task_id, statistics_key)

    engine._claim_and_run = counting
    try:
        await engine.start()
        await wait_until(lambda: claims and legacy["task_id"] not in engine._running)
        await asyncio.sleep(0.5)
        # 修复前 _on_done → notify 会在 0.5 秒内重领上百次。
        assert len(claims) == 1, len(claims)
        assert engine._stalled.get(legacy["task_id"]) == (legacy["task_key"], legacy["queued_at"])
        task, doc = await state(app, legacy)
        assert task.status == "queued" and doc.status == "pending"
        # 作者侧占用被释放后（例如管理员修复），外部 notify 让引擎重新证明并清理。
        async with app.state.sessionmaker() as db:
            await db.execute(update(KbDocument).where(KbDocument.id == legacy["document_id"]).values(
                index_task_key=None, index_task_queued_at=None, index_claim_key=None,
            ))
            await db.commit()
        engine.notify()
        finished = await wait_task(client, ref(legacy))
        assert finished["status"] == "failed" and finished["error"] == FAILED
        assert len(claims) == 2 and legacy["task_id"] not in engine._stalled
    finally:
        engine._claim_and_run = original
    assert not fake_llm.calls and not fake_llm.embed_calls
