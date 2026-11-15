"""章节每次抢占拥有独立身份，旧清理不得影响新抢占。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from traceback import format_exception
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import DBAPIError

from app.core.errors import AppError, ConflictError
from app.models import Chapter, ProjectCollaborator, Task
from app.services import authorization_service, chapter_claim, project_lifecycle
from tests.helpers import create_project, register


async def _world(app, client, make_client, *, collaborator=False, batch=False, status="draft"):
    await app.state.task_engine.stop()
    owner = await register(client, "chapter-claim-owner")
    project = await create_project(client)
    actor = owner
    grant = None
    if collaborator:
        guest = make_client()
        actor = await register(guest, "chapter-claim-editor")
        invited = (await client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
            "username": "chapter-claim-editor", "role": "editor",
        })).json()
        accepted = await guest.post(
            f"/api/v1/collaboration/invitations/project/{invited['invitation_id']}/accept",
            json={"expected_revision": invited["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
        grant = (invited["invitation_id"], accepted.json()["revision"])
    queued_at = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    execution_key = str(uuid4()) if batch else None
    async with app.state.sessionmaker() as db:
        chapter = Chapter(project_id=project["id"], title="章", content="不应被旧清理修改", status=status)
        db.add(chapter)
        await db.flush()
        payload = {"request": {"project_id": project["id"]}}
        if grant:
            payload["_collaboration_grant"] = {"invitation_id": grant[0], "revision": grant[1]}
        if batch:
            payload["chapter_ids"] = [chapter.id]
        else:
            payload["chapter_id"] = chapter.id
            payload["request"]["chapter_id"] = chapter.id
        task = Task(
            user_id=actor["id"], project_id=project["id"], chapter_id=None if batch else chapter.id,
            kind="chapter.batch" if batch else "chapter.generate", status="running" if batch else "queued",
            resource_key=f"batch:{project['id']}" if batch else f"chapter:{chapter.id}",
            queued_at=queued_at, execution_key=execution_key, input=payload,
            checkpoint={"phase": "generate", "current": None, "items": []} if batch else None,
        )
        db.add(task)
        await db.flush()
        identity = {
            "task_id": task.id, "task_key": task.statistics_key, "actor_id": actor["id"],
            "project_id": project["id"], "chapter_id": chapter.id, "queued_at": queued_at,
            "grant_identity": grant,
        }
        await db.commit()
    return identity, execution_key


async def test_queued_claim_binds_task_then_real_execution(app, client, make_client):
    identity, _ = await _world(app, client, make_client, collaborator=True, status="done")
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_queued_chapter(db, **identity)
        assert claim.prev_status == "done" and claim.execution_key is None
        await db.commit()
    execution_key = str(uuid4())
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            status="running", execution_key=execution_key,
        ))
        running = await chapter_claim.activate_queued_chapter(db, claim, execution_key=execution_key)
        await db.commit()
    async with app.state.sessionmaker() as db:
        await chapter_claim.require_running_chapter(db, running, for_update=True)
        assert not await chapter_claim.release_chapter_claim(db, claim)
        assert await chapter_claim.release_chapter_claim(db, running)
        await db.commit()
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, identity["chapter_id"])
        assert (row.status, row.content) == ("done", "不应被旧清理修改")
        assert row.generation_claim_key is None and row.generation_claim_task_key is None


async def test_batch_same_execution_new_attempt_blocks_old_cleanup(app, client, make_client):
    identity, execution_key = await _world(app, client, make_client, batch=True)
    async with app.state.sessionmaker() as db:
        first = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution_key)
        assert await chapter_claim.release_chapter_claim(db, first)
        second = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution_key)
        assert first.claim_key != second.claim_key
        assert not await chapter_claim.release_chapter_claim(db, first)
        await chapter_claim.require_running_chapter(db, second, for_update=True)
        await db.commit()
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, identity["chapter_id"])
        assert row.status == "generating" and row.generation_claim_key == second.claim_key


@pytest.mark.parametrize("held", ["none", "user", "project"])
async def test_read_claim_without_proven_send_lease_is_rejected(app, client, make_client, held):
    identity, execution_key = await _world(app, client, make_client, batch=True)
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution_key)
        await db.commit()
    async with app.state.sessionmaker() as db:
        if held == "user":
            await authorization_service.require_authenticated_for_send(db, identity["actor_id"])
        elif held == "project":
            await project_lifecycle.lock_record(db, identity["project_id"])
        with pytest.raises(ConflictError) as error:
            await chapter_claim.require_running_chapter(db, claim, for_update=False)
        assert error.value.extra["code"] == "chapter_claim_changed"
        await db.rollback()


async def test_failed_running_claim_rolls_back_flushed_work(app, client, make_client):
    identity, execution_key = await _world(app, client, make_client, batch=True)
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution_key)
        await db.commit()
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == identity["chapter_id"]).values(content="不应提交"))
        with pytest.raises(ConflictError):
            await chapter_claim.require_running_chapter(db, replace(claim, claim_key=str(uuid4())))
        # 防止调用方捕获身份错误后提交先前已 flush 的业务修改。
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Chapter.content).where(Chapter.id == identity["chapter_id"])) \
            == "不应被旧清理修改"


async def test_failed_manual_reset_rolls_back_claim_update(app, client, make_client, monkeypatch):
    identity, execution_key = await _world(app, client, make_client, batch=True)
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution_key)
        await db.commit()

    def fail_after_update(*args, **kwargs):
        raise RuntimeError("reset interrupted after update")

    monkeypatch.setattr(chapter_claim, "_cache", fail_after_update)
    async with app.state.sessionmaker() as db:
        with pytest.raises(RuntimeError, match="reset interrupted"):
            await chapter_claim.reset_generating_chapter(
                db, identity["actor_id"], identity["project_id"], identity["chapter_id"], "done",
            )
        await db.commit()
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert (chapter.status, chapter.content, chapter.generation_claim_key) == (
            "generating", "不应被旧清理修改", claim.claim_key,
        )


async def test_invalid_manual_reset_rolls_back_previously_flushed_work(app, client, make_client):
    identity, _ = await _world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == identity["chapter_id"]).values(content="不应提交"))
        with pytest.raises(ValueError):
            await chapter_claim.reset_generating_chapter(
                db, identity["actor_id"], identity["project_id"], identity["chapter_id"], "invalid",
            )
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Chapter.content).where(Chapter.id == identity["chapter_id"])) \
            == "不应被旧清理修改"


async def test_manual_reset_invalidates_old_claim_without_modifying_content(app, client, make_client):
    identity, execution_key = await _world(app, client, make_client, batch=True)
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution_key)
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await chapter_claim.reset_generating_chapter(
            db, identity["actor_id"], identity["project_id"], identity["chapter_id"], "done",
        )
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert not await chapter_claim.release_chapter_claim(db, claim)
        with pytest.raises(ConflictError):
            await chapter_claim.require_running_chapter(db, claim, for_update=True)
        await db.rollback()
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert (chapter.status, chapter.content, chapter.generation_claim_key) == (
            "done", "不应被旧清理修改", None,
        )


async def test_manual_reset_then_new_task_is_not_released_by_old_task(app, client, make_client):
    identity, execution_key = await _world(app, client, make_client, batch=True)
    async with app.state.sessionmaker() as db:
        old = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution_key)
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await chapter_claim.reset_generating_chapter(
            db, identity["actor_id"], identity["project_id"], identity["chapter_id"], "done",
        )
        now = datetime(2026, 3, 1, tzinfo=UTC)
        task = Task(
            user_id=identity["actor_id"], project_id=identity["project_id"], chapter_id=identity["chapter_id"],
            kind="chapter.generate", status="queued", resource_key=f"chapter:{identity['chapter_id']}",
            queued_at=now, input={"chapter_id": identity["chapter_id"], "request": {
                "chapter_id": identity["chapter_id"], "project_id": identity["project_id"],
            }},
        )
        db.add(task)
        await db.flush()
        new_identity = {**identity, "task_id": task.id, "task_key": task.statistics_key, "queued_at": now}
        new = await chapter_claim.claim_queued_chapter(db, **new_identity)
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert not await chapter_claim.release_chapter_claim(db, old)
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert (chapter.status, chapter.content, chapter.generation_claim_key) == (
            "generating", "不应被旧清理修改", new.claim_key,
        )


async def test_revoked_editor_cannot_save_but_system_can_release_original_claim(app, client, make_client):
    identity, execution_key = await _world(app, client, make_client, collaborator=True, batch=True)
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution_key)
        await db.commit()
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == identity["project_id"],
            ProjectCollaborator.user_id == identity["actor_id"],
        ))
        member.status = "revoked"
        member.revision += 1
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(AppError):
            await chapter_claim.require_running_chapter(db, claim, for_update=True)
        await db.rollback()
        assert await chapter_claim.release_chapter_claim(db, claim)
        await db.commit()
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert (chapter.status, chapter.content) == ("draft", "不应被旧清理修改")


async def test_legacy_and_requeued_tasks_cannot_be_guessed(app, client, make_client):
    identity, _ = await _world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await chapter_claim.claim_queued_chapter(db, **{**identity, "queued_at": None})
        await db.rollback()
        queued = await chapter_claim.claim_queued_chapter(db, **identity)
        await db.commit()
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            queued_at=identity["queued_at"] + timedelta(seconds=1),
        ))
        await db.commit()
        assert not await chapter_claim.release_chapter_claim(db, queued)


async def test_failed_activation_rolls_back_running_cas_even_if_caller_commits(app, client, make_client):
    identity, _ = await _world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        queued = await chapter_claim.claim_queued_chapter(db, **identity)
        await db.commit()
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            status="running", execution_key=str(uuid4()),
        ))
        with pytest.raises(ConflictError):
            await chapter_claim.activate_queued_chapter(db, queued, execution_key=str(uuid4()))
        # 即使内部调用者捕获异常后提交，也不能保留未激活的 running 任务。
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert task.status == "queued" and task.execution_key is None
        assert chapter.generation_claim_key == queued.claim_key


async def test_malformed_activation_rolls_back_running_cas(app, client, make_client):
    identity, _ = await _world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            status="running", execution_key=str(uuid4()),
        ))
        with pytest.raises(ConflictError):
            await chapter_claim.activate_queued_chapter(db, None, execution_key=str(uuid4()))
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        assert task.status == "queued" and task.execution_key is None


async def test_activation_rejects_queued_snapshot_with_execution_identity(app, client, make_client):
    identity, _ = await _world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        queued = await chapter_claim.claim_queued_chapter(db, **identity)
        await db.commit()
    execution_key = str(uuid4())
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            status="running", execution_key=execution_key,
        ))
        forged = replace(queued, execution_key=str(uuid4()))
        with pytest.raises(ConflictError):
            await chapter_claim.activate_queued_chapter(db, forged, execution_key=execution_key)
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        assert task.status == "queued" and task.execution_key is None


async def test_invalid_claim_identity_rolls_back_previously_flushed_work(app, client, make_client):
    identity, _ = await _world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(queued_at=None))
        with pytest.raises(ConflictError):
            await chapter_claim.claim_queued_chapter(db, **{**identity, "queued_at": None})
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        assert task.queued_at.replace(tzinfo=UTC) == identity["queued_at"]


@pytest.mark.parametrize("forged", ["project", "missing_grant", "extra_grant", "bool_chapter"])
async def test_invalid_task_payload_is_not_an_authorization(app, client, make_client, forged):
    identity, _ = await _world(app, client, make_client, collaborator=True)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        data = dict(task.input)
        if forged == "project":
            data["request"] = {**data["request"], "project_id": identity["project_id"] + 100}
        elif forged == "missing_grant":
            data.pop("_collaboration_grant")
        elif forged == "extra_grant":
            data["_collaboration_grant"] = {**data["_collaboration_grant"], "extra": "untrusted"}
        else:
            data["chapter_id"] = True
        # Python 将 True 与整数 1 视为相等；用 SQL 写入以复现实际损坏 JSON。
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(input=data))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await chapter_claim.claim_queued_chapter(db, **identity)
        await db.commit()
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_denied_claim_does_not_flush_pending_objects(app, client, make_client):
    identity, _ = await _world(app, client, make_client, collaborator=True)
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == identity["project_id"],
            ProjectCollaborator.user_id == identity["actor_id"],
        ))
        member.role = "viewer"
        member.revision += 1
        await db.commit()
    async with app.state.sessionmaker() as db:
        pending = Chapter(project_id=identity["project_id"], title="不得提前写入")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(AppError):
            await chapter_claim.claim_queued_chapter(db, **identity)
        assert flushes == [] and pending.id is None
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Chapter.id).where(Chapter.title == "不得提前写入")) is None


async def test_failed_second_write_rolls_back_chapter_and_task_together(app, client, make_client):
    identity, _ = await _world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        def fail_task_update(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lower().lstrip().startswith("update tasks set"):
                raise RuntimeError("simulated storage failure")

        event.listen(db.bind.sync_engine, "before_cursor_execute", fail_task_update)
        try:
            with pytest.raises(RuntimeError, match="simulated storage failure"):
                await chapter_claim.claim_queued_chapter(db, **identity)
            await db.commit()
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", fail_task_update)
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, identity["chapter_id"])
        task = await db.get(Task, identity["task_id"])
        assert chapter.status == "draft" and chapter.generation_claim_key is None
        assert "_chapter_claim" not in task.input


async def test_database_error_is_sanitized_and_rolls_back_both_writes(app, client, make_client):
    identity, _ = await _world(app, client, make_client)
    secret = "chapter-sensitive-test-value"
    async with app.state.sessionmaker() as db:
        def fail_task_update(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lower().lstrip().startswith("update tasks set"):
                raise DBAPIError(f"UPDATE {secret}", {"content": secret}, RuntimeError(secret))

        event.listen(db.bind.sync_engine, "before_cursor_execute", fail_task_update)
        try:
            with pytest.raises(chapter_claim.ChapterClaimStorageError) as caught:
                await chapter_claim.claim_queued_chapter(db, **identity)
            assert secret not in "".join(format_exception(caught.value))
            assert caught.value.__suppress_context__
            await db.commit()
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", fail_task_update)
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, identity["chapter_id"])
        task = await db.get(Task, identity["task_id"])
        assert chapter.status == "draft" and chapter.generation_claim_key is None
        assert "_chapter_claim" not in task.input


async def test_batch_rejects_reused_task_uuid_as_execution(app, client, make_client):
    identity, _ = await _world(app, client, make_client, batch=True)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            execution_key=identity["task_key"],
        ))
        await db.commit()
        with pytest.raises(ConflictError):
            await chapter_claim.claim_batch_chapter(db, **identity, execution_key=identity["task_key"])
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert chapter.status == "draft" and chapter.generation_claim_key is None
