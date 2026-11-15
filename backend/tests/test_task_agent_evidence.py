"""任务清理不能丢失仍在执行的外部调用身份。"""

import pytest
from sqlalchemy import update

from app.core.errors import ConflictError
from app.models import AgentChange, AgentConversation, Task
from app.services import task_service, task_statistics_service
from tests.helpers import create_project, register


async def _facts(app, client, *, linked):
    await app.state.task_engine.stop()
    actor = await register(client, "agent-evidence-owner")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        original = Task(user_id=actor["id"], project_id=project["id"], kind="agent.turn", status="cancelled")
        unrelated = Task(user_id=actor["id"], project_id=project["id"], kind="chapter.analyze", status="cancelled")
        db.add_all([original, unrelated])
        await task_statistics_service.record_created(db, original)
        await task_statistics_service.record_created(db, unrelated)
        conversation = AgentConversation(user_id=actor["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        change = AgentChange(
            project_id=project["id"], conversation_id=conversation.id,
            turn_task_id=original.id if linked == "id" else None, tool_call_id="external",
            turn_task_key=original.statistics_key if linked in {"id", "turn_uuid"} else None,
            tool_name="call", kind="external", status="executing", arguments={}, preview={},
            # 缺失或损坏的 key 只要 Task ID 匹配也必须保留；丢失 ID 时按 UUID 找回。
            result={"_external_task_key": original.statistics_key} if linked == "uuid" else {},
        )
        db.add(change)
        await db.commit()
        return actor["id"], original.id, original.statistics_key, unrelated.id, change.id


@pytest.mark.parametrize("linked", ["id", "uuid", "turn_uuid"])
async def test_delete_and_bulk_clear_preserve_unfinished_external_evidence(app, client, linked):
    actor, task_id, task_key, unrelated, change_id = await _facts(app, client, linked=linked)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await task_service.delete_task(db, actor, task_id, expected_task_key=task_key)
        await db.rollback()
        assert await task_service.clear_finished(db, actor) == 1
        assert await db.get(Task, task_id) is not None
        assert await db.get(Task, unrelated) is None
        await db.execute(update(AgentChange).where(AgentChange.id == change_id).values(
            status="executed", result={"message": "已确认"},
        ))
        await db.commit()
        await task_service.delete_task(db, actor, task_id, expected_task_key=task_key)
        assert await db.get(Task, task_id) is None


@pytest.mark.parametrize("linked", ["id", "uuid", "turn_uuid"])
async def test_retention_pruning_preserves_unfinished_external_evidence(app, client, monkeypatch, linked):
    actor, task_id, _, unrelated, _ = await _facts(app, client, linked=linked)
    monkeypatch.setattr(task_service, "MAX_FINISHED_PER_USER", 0)
    async with app.state.sessionmaker() as db:
        await task_service.prune_finished(db, actor)
        assert await db.get(Task, task_id) is not None
        assert await db.get(Task, unrelated) is None


@pytest.mark.parametrize("marker", ["_external_outcome_unknown", "_external_claim_id", "_external_task_key",
                                    "_external_execution"])
async def test_failed_unknown_outcome_with_damaged_marker_cannot_be_deleted(app, client, marker):
    actor, task_id, task_key, _, change_id = await _facts(app, client, linked="id")
    async with app.state.sessionmaker() as db:
        await db.execute(update(AgentChange).where(AgentChange.id == change_id).values(
            status="failed", result={marker: None},
        ))
        await db.commit()
        with pytest.raises(ConflictError):
            await task_service.delete_task(db, actor, task_id, expected_task_key=task_key)
        await db.rollback()
        assert await db.get(Task, task_id) is not None


async def test_unbound_external_execution_preserves_all_tasks_in_its_project(app, client):
    actor, task_id, task_key, unrelated, change_id = await _facts(app, client, linked="unbound")
    separate_project = await create_project(client, title="独立作品")
    async with app.state.sessionmaker() as db:
        separate = Task(user_id=actor, project_id=separate_project["id"], kind="chapter.analyze", status="cancelled")
        db.add(separate)
        await task_statistics_service.record_created(db, separate)
        await db.execute(update(AgentChange).where(AgentChange.id == change_id).values(
            status="failed", result={"_external_execution": None},
        ))
        await db.commit()
        separate_id = separate.id
        with pytest.raises(ConflictError):
            await task_service.delete_task(db, actor, task_id, expected_task_key=task_key)
        await db.rollback()
        assert await task_service.clear_finished(db, actor) == 1
        assert await db.get(Task, task_id) is not None and await db.get(Task, unrelated) is not None
        assert await db.get(Task, separate_id) is None
