"""Agent 快照和撤销不能携带或恢复章节生成的运行态 claim。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select

from app.core.errors import ConflictError
from app.models import Chapter, GenerationLog, RetrievalChunk, StoryMemory
from app.services import memory_service
from app.services.agent import effects
from tests.helpers import create_project, register

CLAIM_FIELDS = {"generation_claim_key", "generation_claim_task_key", "generation_claim_prev_status"}


def _snapshot(row):
    snapshot = effects.Snapshot()
    snapshot["chapters"] = {"1": row}
    return snapshot


def test_chapter_claim_is_not_snapshotted_diffed_or_restored():
    table = Table("example_chapters", MetaData(), Column("id", Integer, primary_key=True),
                  Column("title", String), *(Column(name, String) for name in sorted(CLAIM_FIELDS)))
    rule = SimpleNamespace(table="chapters", model=SimpleNamespace(__table__=table))
    assert CLAIM_FIELDS.isdisjoint(column.name for column in effects._columns(rule))
    assert CLAIM_FIELDS.isdisjoint(effects._values(rule, {"id": 1, **dict.fromkeys(CLAIM_FIELDS, "private")}))

    before = {"id": 1, "title": "原章节", **dict.fromkeys(CLAIM_FIELDS, "old-secret")}
    after = {"id": 1, "title": "新章节", **dict.fromkeys(CLAIM_FIELDS, "new-secret")}
    delta = effects.diff(_snapshot(before), _snapshot(after))
    assert len(delta) == 1
    assert CLAIM_FIELDS.isdisjoint(delta[0]["before"])
    assert CLAIM_FIELDS.isdisjoint(delta[0]["after"])
    assert effects.diff(_snapshot(before), _snapshot({**before, **dict.fromkeys(CLAIM_FIELDS, "other")})) == []


async def test_undo_ignores_forged_claim_and_restores_legitimate_legacy_effect(app, client):
    await register(client, "claim-snapshot-owner")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        chapter = Chapter(project_id=project["id"], title="旧标题", status="draft")
        db.add(chapter)
        await db.commit()
        before = await effects.snapshot(db, project["id"])
        assert CLAIM_FIELDS.isdisjoint(before["chapters"][str(chapter.id)])
        chapter.title = "新标题"
        after = await effects.snapshot(db, project["id"])
        await db.commit()
        delta = effects.diff(before, after)
        effect = next(item for item in delta if item["table"] == "chapters")
        forged = {"generation_claim_key": str(uuid4()), "generation_claim_task_key": str(uuid4()),
                  "generation_claim_prev_status": "draft"}
        effect["before"].update(forged)
        effect["after"].update(forged)

        await effects.undo(db, project["id"], delta)
        await db.commit()
        await db.refresh(chapter)
        assert chapter.title == "旧标题" and chapter.status == "draft"
        assert all(getattr(chapter, key) is None for key in CLAIM_FIELDS)


async def test_undo_rejects_cross_project_memory_index_before_deleting_source(app, client):
    await register(client, "memory-snapshot-owner")
    project = await create_project(client)
    foreign = await create_project(client, title="不可显示的其他作品")
    async with app.state.sessionmaker() as db:
        before = await effects.snapshot(db, project["id"])
        memory = StoryMemory(project_id=project["id"], chapter_no=1, kind="manual", origin="manual",
                             content="需要撤销的记忆")
        db.add(memory)
        await db.flush()
        await memory_service.index_memories(db, project["id"], [memory])
        indexed = await db.scalar(select(RetrievalChunk).where(RetrievalChunk.ref_id == memory.id,
                                                               RetrievalChunk.source == "memory"))
        after = await effects.snapshot(db, project["id"])
        await db.commit()
        memory_id, index_id = memory.id, indexed.id
        indexed.project_id = foreign["id"]
        await db.commit()

        with pytest.raises(ConflictError) as error:
            await effects.undo(db, project["id"], effects.diff(before, after))
        assert "不可显示的其他作品" not in str(error.value.extra)
        await db.rollback()
        assert (await db.get(StoryMemory, memory_id)).project_id == project["id"]
        assert (await db.get(RetrievalChunk, index_id)).project_id == foreign["id"]


@pytest.mark.parametrize("foreign_index", [False, True])
async def test_undo_deleted_memory_cannot_adopt_old_orphan_index(app, client, foreign_index):
    await register(client, f"memory-orphan-{foreign_index}")
    project = await create_project(client)
    foreign = await create_project(client, title="不可显示的其他作品")
    async with app.state.sessionmaker() as db:
        memory = StoryMemory(project_id=project["id"], chapter_no=1, kind="manual", origin="manual",
                             content="原来的记忆")
        db.add(memory)
        await db.flush()
        await memory_service.index_memories(db, project["id"], [memory])
        indexed = await db.scalar(select(RetrievalChunk).where(RetrievalChunk.ref_id == memory.id,
                                                               RetrievalChunk.source == "memory"))
        await db.commit()
        before = await effects.snapshot(db, project["id"])
        memory_id, index_id = memory.id, indexed.id
        await db.delete(memory)
        await db.flush()
        after = await effects.snapshot(db, project["id"])
        if foreign_index:
            indexed.project_id = foreign["id"]
        await db.commit()

        with pytest.raises(ConflictError) as error:
            await effects.undo(db, project["id"], effects.diff(before, after))
        assert "不可显示的其他作品" not in str(error.value.extra)
        await db.rollback()
        assert await db.get(StoryMemory, memory_id) is None
        assert (await db.get(RetrievalChunk, index_id)).project_id == (
            foreign["id"] if foreign_index else project["id"]
        )


async def test_undo_log_reference_never_restores_forged_model_source(app, client):
    owner = await register(client, "effect-log-source")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        chapter = Chapter(project_id=project["id"], title="章节")
        db.add(chapter)
        await db.flush()
        log = GenerationLog(project_id=project["id"], actor_user_id=owner["id"], chapter_id=chapter.id,
                            kind="generate", status="success", provider="真实 provider", model="真实 model")
        db.add(log)
        await db.commit()
        before = await effects.snapshot(db, project["id"])
        log.chapter_id = None
        after = await effects.snapshot(db, project["id"])
        await db.commit()
        delta = effects.diff(before, after)
        effect = next(item for item in delta if item["table"] == "generation_logs")
        effect["before"].update(model_source="actor", model_config_owner_id=owner["id"], model="伪造 model")
        await effects.undo(db, project["id"], delta)
        await db.commit()
        await db.refresh(log)
        assert log.chapter_id == chapter.id and log.model_source is None and log.model_config_owner_id is None
        assert log.provider == "真实 provider" and log.model == "真实 model"
