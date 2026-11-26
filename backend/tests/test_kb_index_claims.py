"""索引运行态需要同时绑定任务、入队代次和本次占用，不得清理后来占用。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.errors import ConflictError
from app.models import KbDocument, Task, User
from app.services import kb_index_service as indexes
from tests.helpers import create_project, register
from tests.worker_helpers import fence_worker


async def _seed(app, client, *, queued_at=datetime(2026, 1, 1, tzinfo=UTC)):
    await app.state.task_engine.stop()
    user = await register(client, "claim-owner")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        doc = KbDocument(project_id=project["id"], filename="文档", sha256="a" * 64, text="原文")
        db.add(doc)
        await db.flush()
        task = Task(
            user_id=user["id"], project_id=project["id"], kind="kb.index", status="queued",
            resource_key=f"kbdoc:{doc.id}", queued_at=queued_at,
            input={"request": {"project_id": project["id"], "document_id": doc.id, "mode": "document"}},
        )
        db.add(task)
        await db.flush()
        task_id, task_key, doc_id = task.id, task.statistics_key, doc.id
        await db.commit()
    return user["id"], project["id"], doc_id, task_id, task_key


async def test_queue_claim_rotates_on_execution_and_rejects_old_cleanup(app, client):
    _, pid, did, tid, key = await _seed(app, client)
    queue_key, execution_key = str(uuid4()), str(uuid4())
    async with app.state.sessionmaker() as db:
        queued = await indexes.bind_queued_document_claim(db, tid, key, queue_claim_key=queue_key)
        assert queued.project_id == pid and queued.document_id == did
        await db.commit()
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == tid).values(status="running", execution_key=execution_key))
        running = await indexes.activate_document_claim(db, queued, execution_key=execution_key)
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert not await indexes.release_document_claim(db, queued)
        assert await indexes.document_claim_is_current(db, running)
        assert await indexes.release_document_claim(db, running)
        await db.commit()
        doc = await db.get(KbDocument, did)
        assert (doc.status, doc.error, doc.text) == ("failed", "建索引未完成（失败、取消或中断），请重试", "原文")
        assert (doc.index_task_key, doc.index_task_queued_at, doc.index_claim_key) == (None, None, None)


async def test_cancelled_old_task_cannot_release_new_task_claim(app, client):
    uid, pid, did, old_id, old_key = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        old = await indexes.bind_queued_document_claim(db, old_id, old_key, queue_claim_key=str(uuid4()))
        await db.execute(update(Task).where(Task.id == old_id).values(status="cancelled"))
        newer = Task(user_id=uid, project_id=pid, kind="kb.index", status="queued", resource_key=f"kbdoc:{did}",
                     queued_at=datetime(2026, 1, 2, tzinfo=UTC),
                     input={"request": {"project_id": pid, "document_id": did, "mode": "document"}})
        db.add(newer)
        await db.flush()
        new = await indexes.bind_queued_document_claim(db, newer.id, newer.statistics_key, queue_claim_key=str(uuid4()))
        assert not await indexes.release_document_claim(db, old)
        assert await indexes.document_claim_is_current(db, new)
        await db.commit()
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, did)
        assert doc.index_task_key == new.task_key and doc.index_claim_key == new.claim_key and doc.status == "pending"


@pytest.mark.parametrize("taken_over", [False, True], ids=["fenced", "adopted"])
async def test_old_worker_cannot_release_an_indexing_document(app, second_app, client, taken_over):
    _, _, doc_id, task_id, task_key = await _seed(app, client)
    engine = app.state.task_engine
    old_owner = await engine.lease.register()
    new_owner = await second_app.state.task_engine.lease.register()
    execution_key = str(uuid4())
    maker = app.state.sessionmaker
    async with maker() as db:
        queued = await indexes.bind_queued_document_claim(db, task_id, task_key, queue_claim_key=str(uuid4()))
        await db.execute(update(Task).where(Task.id == task_id).values(
            status="running", execution_key=execution_key, worker_key=old_owner,
        ))
        old_claim = await indexes.activate_document_claim(db, queued, execution_key=execution_key,
                                                         worker_key=old_owner)
        await db.commit()
    await fence_worker(engine, mirror=False)
    if taken_over:
        async with maker.begin() as db:
            (await db.get(Task, task_id)).worker_key = new_owner
    async with maker() as db:
        assert not await indexes.release_document_claim(db, old_claim)
        # 历史终态清理必须在数据库里锁到指定终态，不能用它绕过 running 的围栏。
        assert not await indexes.release_document_claim(db, old_claim, terminal_status="interrupted")
        await db.commit()
        doc = await db.get(KbDocument, doc_id)
        assert (doc.status, doc.index_task_key, doc.index_claim_key) == ("pending", task_key, execution_key)
    async with second_app.state.sessionmaker() as db:
        if taken_over:
            new_claim = indexes.running_document_claim(await db.get(Task, task_id))
            assert await indexes.release_document_claim(db, new_claim)
        else:
            # 后续系统修复精确命中已中断的原轮次时，旧进程可以已经退出。
            (await db.get(Task, task_id)).status = "interrupted"
            await db.commit()
            assert await indexes.release_document_claim(db, old_claim, terminal_status="interrupted")
        await db.commit()
        doc = await db.get(KbDocument, doc_id)
        assert (doc.status, doc.index_task_key, doc.index_claim_key) == ("failed", None, None)


async def test_claim_requires_original_execution_and_queue_round(app, client):
    _, _, _, tid, key = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        queued = await indexes.bind_queued_document_claim(db, tid, key, queue_claim_key=str(uuid4()))
        actual, wrong = str(uuid4()), str(uuid4())
        await db.execute(update(Task).where(Task.id == tid).values(status="running", execution_key=actual))
        with pytest.raises(ConflictError):
            await indexes.activate_document_claim(db, queued, execution_key=wrong)
        active = await indexes.activate_document_claim(db, queued, execution_key=actual)
        await db.commit()
    async with app.state.sessionmaker() as db:
        old_time = active.queued_at
        await db.execute(update(Task).where(Task.id == tid).values(
            status="queued", execution_key=None, queued_at=old_time + timedelta(seconds=1),
        ))
        await db.commit()
        assert not await indexes.document_claim_is_current(db, active)
        assert not await indexes.release_document_claim(db, active)


async def test_legacy_queue_without_proven_round_is_not_claimed(app, client):
    _, _, _, tid, key = await _seed(app, client, queued_at=None)
    # SQLAlchemy Python default normally fills an explicit None on insert; emulate an actual pre-0026 row.
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == tid).values(queued_at=None))
        await db.commit()
        with pytest.raises(ConflictError):
            await indexes.bind_queued_document_claim(db, tid, key, queue_claim_key=str(uuid4()))
        await db.execute(update(Task).where(Task.id == tid).values(statistics_key=None))
        await db.commit()
        with pytest.raises(ConflictError):
            await indexes.bind_queued_document_claim(db, tid, None, queue_claim_key=str(uuid4()))


async def test_claim_rejects_forged_task_document_and_invalid_keys(app, client):
    _, _, did, tid, key = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await indexes.bind_queued_document_claim(db, tid, key, queue_claim_key="-" * 36)
        task = await db.scalar(select(Task).where(Task.id == tid))
        task.input = {"request": {"project_id": task.project_id, "document_id": did + 100, "mode": "document"}}
        await db.commit()
        with pytest.raises(ConflictError):
            await indexes.bind_queued_document_claim(db, tid, key, queue_claim_key=str(uuid4()))


async def test_claim_is_bound_to_actor_and_pending_admin_cancel(app, client):
    _, _, _, tid, key = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        queued = await indexes.bind_queued_document_claim(db, tid, key, queue_claim_key=str(uuid4()))
        await db.commit()
        await db.execute(update(Task).where(Task.id == tid).values(admin_cancel_operation_id=str(uuid4())))
        await db.commit()
        assert not await indexes.document_claim_is_current(db, queued)
        # 系统清理允许在受控取消中清理该轮，不允许写入新的模型结果。
        assert await indexes.release_document_claim(db, queued)
        await db.rollback()
        other = User(username="claim-other", password_hash="hash")
        db.add(other)
        await db.flush()
        await db.execute(update(Task).where(Task.id == tid).values(
            admin_cancel_operation_id=None, user_id=other.id,
        ))
        # 当前任务被错误改属其他账号时，SQL 谓词不能把它视为原 actor 的占用。
        assert not await indexes.document_claim_is_current(db, queued)
        await db.rollback()


async def test_book_backup_omits_claim_and_import_never_restores_injected_claim(app, client):
    _, pid, did, tid, key = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        claim = await indexes.bind_queued_document_claim(db, tid, key, queue_claim_key=str(uuid4()))
        await db.commit()
    response = await client.get(f"/api/v1/projects/{pid}/backup")
    assert response.status_code == 200, response.text
    backup = response.json()
    assert backup["app_schema"] == "0079"
    doc = backup["tables"]["kb_documents"][0]
    fields = {"index_task_key", "index_task_queued_at", "index_claim_key"}
    assert not fields.intersection(doc)
    # 备份允许未知字段；即使被恶意夹带，导入仍不能恢复他人的运行态。
    doc.update(index_task_key=claim.task_key, index_task_queued_at=claim.queued_at.isoformat(),
               index_claim_key=claim.claim_key)
    raw = json.dumps(backup, ensure_ascii=False).encode()
    preview = await client.post("/api/v1/projects/import/preview", files={"file": ("backup.json", raw)})
    assert preview.status_code == 200, preview.text
    imported = await client.post("/api/v1/projects/import", json={"token": preview.json()["token"]})
    assert imported.status_code == 201, imported.text
    async with app.state.sessionmaker() as db:
        new_doc = await db.scalar(select(KbDocument).where(KbDocument.project_id == imported.json()["project"]["id"]))
        assert new_doc is not None and new_doc.id != did and new_doc.text == "原文"
        assert (new_doc.index_task_key, new_doc.index_task_queued_at, new_doc.index_claim_key) == (None, None, None)
