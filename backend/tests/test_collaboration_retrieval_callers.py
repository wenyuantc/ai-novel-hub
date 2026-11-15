"""知识库与记忆人工写路径不得修改异常跨书的派生检索行。"""

import json

import pytest
from sqlalchemy import func, select, update

from app.core.errors import ConflictError
from app.models import (
    Chapter,
    ChapterAnalysis,
    ChapterPlan,
    KbChunk,
    KbDocument,
    Outline,
    OutlineSnapshot,
    Project,
    RetrievalChunk,
    StoryMemory,
)
from app.schemas.memory import MemoryCreate, MemoryUpdate
from app.schemas.outline import PlansSync
from app.services import chapter_numbering, kb_service, memory_service, plan_service, retrieval_service
from app.services.agent.tools.base import TransactionSession
from app.services.project_transfer import importer
from tests.helpers import create_project, register


async def _seed(app, client):
    await app.state.task_engine.stop()
    user = await register(client, "retrieval-caller")
    first, second = await create_project(client), await create_project(client)
    base = f"/api/v1/projects/{first['id']}"
    chapter = await client.post(f"{base}/chapters", json={"chapter_no": 1, "title": "章节", "content": "正文"})
    assert chapter.status_code == 201, chapter.text
    memory = await client.post(f"{base}/memories", json={"chapter_no": 1, "content": "本书记忆"})
    assert memory.status_code == 201, memory.text
    async with app.state.sessionmaker() as db:
        doc = KbDocument(project_id=first["id"], filename="本书.txt", sha256="a" * 64, text="本书文档")
        db.add(doc)
        await db.flush()
        chunks = [KbChunk(document_id=doc.id, seq=1, content="甲"), KbChunk(document_id=doc.id, seq=2, content="乙")]
        db.add_all(chunks)
        await db.flush()
        await retrieval_service.upsert_many(db, first["id"], "kb", [
            retrieval_service.ChunkInput(ref_id=c.id, text=c.content) for c in chunks
        ])
        await db.commit()
        return {"user": user["id"], "project": first["id"], "foreign": second["id"],
                "chapter": chapter.json()["id"], "memory": memory.json()["id"],
                "document": doc.id, "chunks": [c.id for c in chunks]}


async def _corrupt_index(app, source, ref_id, foreign_project):
    async with app.state.sessionmaker() as db:
        await db.execute(update(RetrievalChunk).where(
            RetrievalChunk.source == source, RetrievalChunk.ref_id == ref_id,
        ).values(project_id=foreign_project, text="他书索引秘密"))
        await db.commit()


@pytest.mark.parametrize("action", ["patch", "delete"])
async def test_kb_http_rejects_foreign_index_without_partial_changes(app, client, action):
    data = await _seed(app, client)
    await _corrupt_index(app, "kb", data["chunks"][1], data["foreign"])
    url = f"/api/v1/projects/{data['project']}/kb/documents/{data['document']}"
    response = await client.patch(url, json={"inject": False}) if action == "patch" else await client.delete(url)
    assert response.status_code == 409 and response.json()["code"] == "retrieval_reference_invalid"
    assert "他书索引秘密" not in response.text and "project_id" not in response.text
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, data["document"])
        chunks = list(await db.scalars(select(KbChunk).where(KbChunk.document_id == doc.id)))
        rows = list(await db.scalars(select(RetrievalChunk).where(
            RetrievalChunk.source == "kb", RetrievalChunk.ref_id.in_(data["chunks"]),
        )))
        assert doc.inject is True and len(chunks) == len(rows) == 2
        assert all(row.enabled for row in rows)
        assert next(row for row in rows if row.ref_id == data["chunks"][1]).project_id == data["foreign"]


async def test_memory_delete_and_caught_update_reject_foreign_index_before_mutation(app, client):
    data = await _seed(app, client)
    await _corrupt_index(app, "memory", data["memory"], data["foreign"])
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await memory_service.update_memory(db, data["user"], data["project"], data["memory"],
                                               MemoryUpdate(content="不可提交的修改"))
        # service 直接调用者捕获错误后提交，也不能落入本次授权失败路径修改的记忆。
        await db.commit()
    response = await client.delete(f"/api/v1/projects/{data['project']}/memories/{data['memory']}")
    assert response.status_code == 409 and response.json()["code"] == "retrieval_reference_invalid"
    async with app.state.sessionmaker() as db:
        memory = await db.get(StoryMemory, data["memory"])
        index = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == data["memory"],
        ))
        assert memory.content == "本书记忆" and index.project_id == data["foreign"] and index.text == "他书索引秘密"


async def test_kb_caught_update_does_not_flush_or_commit_document_change(app, client):
    data = await _seed(app, client)
    await _corrupt_index(app, "kb", data["chunks"][1], data["foreign"])
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await kb_service.set_inject(db, data["user"], data["project"], data["document"], False)
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert (await db.get(KbDocument, data["document"])).inject is True


@pytest.mark.parametrize("row_type", ["chunk", "index"])
async def test_kb_rejects_dirty_identity_without_flushing_it(app, client, row_type):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        if row_type == "chunk":
            foreign_doc = KbDocument(project_id=data["foreign"], filename="他书.txt", sha256="c" * 64, text="他书")
            db.add(foreign_doc)
            await db.flush()
            chunk = await db.get(KbChunk, data["chunks"][0])
            chunk.document_id = foreign_doc.id
        else:
            index = await db.scalar(select(RetrievalChunk).where(
                RetrievalChunk.source == "kb", RetrievalChunk.ref_id == data["chunks"][0],
            ))
            index.project_id = data["foreign"]
        with pytest.raises(ConflictError):
            await kb_service.set_inject(db, data["user"], data["project"], data["document"], False)
        with db.no_autoflush:
            document_id = await db.scalar(select(KbChunk.document_id).where(KbChunk.id == data["chunks"][0]))
            owner_id = await db.scalar(select(RetrievalChunk.project_id).where(
                RetrievalChunk.source == "kb", RetrievalChunk.ref_id == data["chunks"][0],
            ))
            assert document_id == data["document"] and owner_id == data["project"]
        await db.rollback()


async def test_cross_book_chapter_memory_helpers_fail_before_mutating_any_row(app, client):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        foreign = StoryMemory(project_id=data["foreign"], chapter_id=data["chapter"], chapter_no=1,
                              kind="summary", origin="auto", content="他书记忆")
        db.add(foreign)
        await db.flush()
        await retrieval_service.upsert_many(db, data["foreign"], "memory", [memory_service._chunk(foreign)])
        analysis = ChapterAnalysis(project_id=data["project"], chapter_id=data["chapter"], content_hash="a" * 64,
                                   chapter_no=1, summary="替换记忆")
        db.add(analysis)
        await db.commit()
        foreign_id, analysis_id = foreign.id, analysis.id
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await memory_service.on_chapter_renumbered(db, data["chapter"], 7)
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await memory_service.on_chapters_deleted(db, [data["chapter"]])
        await db.commit()
    async with app.state.sessionmaker() as db:
        chapter, analysis = await db.get(Chapter, data["chapter"]), await db.get(ChapterAnalysis, analysis_id)
        with pytest.raises(ConflictError):
            await memory_service.sync_from_analysis(db, chapter, analysis)
        await db.commit()
    async with app.state.sessionmaker() as db:
        memory = await db.get(StoryMemory, foreign_id)
        index = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == foreign_id,
        ))
        assert (memory.project_id, memory.chapter_no, memory.content) == (data["foreign"], 1, "他书记忆")
        assert index.project_id == data["foreign"] and index.chapter_no == 1


async def test_index_memories_refuses_foreign_source_without_existing_index(app, client):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        foreign = StoryMemory(project_id=data["foreign"], chapter_no=1, kind="manual", content="他书记忆")
        db.add(foreign)
        await db.flush()
        with pytest.raises(ConflictError):
            await memory_service.index_memories(db, data["project"], [foreign])
        await db.commit()
        assert await db.scalar(select(RetrievalChunk.id).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == foreign.id,
        )) is None


@pytest.mark.parametrize("foreign", [False, True])
async def test_new_memory_cannot_claim_orphan_index_even_if_error_is_caught(app, client, foreign):
    data = await _seed(app, client)
    next_id = data["memory"] + 1
    async with app.state.sessionmaker() as db:
        await retrieval_service.upsert_many(db, data["foreign"] if foreign else data["project"], "memory", [
            retrieval_service.ChunkInput(ref_id=next_id, text="先前孤儿"),
        ])
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await memory_service.create_memory(
                db, data["user"], data["project"], MemoryCreate(chapter_no=1, content="新记忆"),
            )
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await db.get(StoryMemory, next_id) is None
        orphan = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == next_id,
        ))
        assert orphan.text == "先前孤儿"


async def test_reanalysis_orphan_collision_rolls_back_earlier_deletions(app, client):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        old = StoryMemory(project_id=data["project"], chapter_id=data["chapter"], chapter_no=1,
                          kind="summary", origin="auto", content="先前摘要")
        analysis = ChapterAnalysis(project_id=data["project"], chapter_id=data["chapter"], chapter_no=1,
                                   content_hash="b" * 64, summary="全新摘要")
        db.add_all([old, analysis])
        await db.flush()
        await retrieval_service.upsert_many(db, data["project"], "memory", [memory_service._chunk(old)])
        old_id, next_id, analysis_id = old.id, old.id + 1, analysis.id
        await retrieval_service.upsert_many(db, data["foreign"], "memory", [
            retrieval_service.ChunkInput(ref_id=next_id, text="孤儿索引"),
        ])
        await db.commit()
    async with app.state.sessionmaker() as db:
        chapter, analysis = await db.get(Chapter, data["chapter"]), await db.get(ChapterAnalysis, analysis_id)
        with pytest.raises(ConflictError):
            await memory_service.sync_from_analysis(db, chapter, analysis)
        await db.commit()
    async with app.state.sessionmaker() as db:
        old = await db.get(StoryMemory, old_id)
        old_index = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == old_id,
        ))
        orphan = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == next_id,
        ))
        assert old.content == old_index.text == "先前摘要"
        assert orphan.text == "孤儿索引" and orphan.project_id == data["foreign"]
        assert await db.get(StoryMemory, next_id) is None


async def test_kb_batch_validates_all_sources_before_changing_first_rows(app, client):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        chunks = [KbChunk(document_id=data["document"], seq=i + 3, content="正文") for i in range(400)]
        db.add_all(chunks)
        await db.flush()
        await retrieval_service.upsert_many(db, data["project"], "kb", [
            retrieval_service.ChunkInput(ref_id=chunk.id, text=chunk.content) for chunk in chunks
        ])
        last_id = chunks[-1].id
        await db.commit()
    await _corrupt_index(app, "kb", last_id, data["foreign"])
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await kb_service.set_inject(db, data["user"], data["project"], data["document"], False)
        await db.commit()
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(RetrievalChunk).where(
            RetrievalChunk.source == "kb", RetrievalChunk.ref_id.in_(data["chunks"] + [last_id]),
        )))
        assert len(rows) == 3 and all(row.enabled for row in rows)
        assert (await db.get(KbDocument, data["document"])).inject is True


async def test_backup_import_rejects_existing_index_for_new_memory_and_rolls_back(app, client, monkeypatch):
    data = await _seed(app, client)
    backup = await client.get(f"/api/v1/projects/{data['project']}/backup")
    assert backup.status_code == 200, backup.text
    async with app.state.sessionmaker() as db:
        await retrieval_service.upsert_many(db, data["foreign"], "memory", [
            retrieval_service.ChunkInput(ref_id=999999, text="其他作品的孤儿索引"),
        ])
        before = await db.scalar(select(func.count(Project.id)))
        await db.commit()

    original = importer._Importer._retrieval

    async def inject_conflict(instance):
        # 模拟新 ID 已碰到同作品异常旧索引；不允许以导入业务行覆盖并认领旧内容。
        memory = instance.objects["story_memories"][0]
        await retrieval_service.upsert_many(instance.db, instance.project_id, "memory", [
            retrieval_service.ChunkInput(ref_id=memory.id, text="已存在的异常索引"),
        ])
        await original(instance)

    monkeypatch.setattr(importer._Importer, "_retrieval", inject_conflict)
    response = await client.post("/api/v1/projects/import", files={
        "file": ("backup.json", json.dumps(backup.json(), ensure_ascii=False).encode(), "application/json"),
    })
    assert response.status_code == 409 and response.json()["code"] == "retrieval_reference_invalid"
    assert "其他作品的孤儿索引" not in response.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id))) == before
        orphan = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == 999999,
        ))
        assert orphan.project_id == data["foreign"] and orphan.text == "其他作品的孤儿索引"


async def test_backup_import_rolls_back_on_foreign_orphan_collision(app, client):
    data = await _seed(app, client)
    backup = await client.get(f"/api/v1/projects/{data['project']}/backup")
    assert backup.status_code == 200, backup.text
    next_id = data["memory"] + 1
    async with app.state.sessionmaker() as db:
        await retrieval_service.upsert_many(db, data["foreign"], "memory", [
            retrieval_service.ChunkInput(ref_id=next_id, text="他书孤儿"),
        ])
        await db.commit()
        before = await db.scalar(select(func.count(Project.id)))
    response = await client.post("/api/v1/projects/import", files={
        "file": ("backup.json", json.dumps(backup.json(), ensure_ascii=False).encode(), "application/json"),
    })
    assert response.status_code == 409 and response.json()["code"] == "retrieval_reference_invalid"
    assert "他书孤儿" not in response.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id))) == before
        assert await db.get(StoryMemory, next_id) is None
        orphan = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == next_id,
        ))
        assert orphan.project_id == data["foreign"] and orphan.text == "他书孤儿"


@pytest.mark.parametrize("corruption", ["foreign_index", "foreign_source", "orphan", "foreign_chapter"])
async def test_chapter_number_shift_preflights_all_sources_before_any_update(app, client, corruption):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        if corruption == "foreign_index":
            await db.execute(update(RetrievalChunk).where(
                RetrievalChunk.source == "memory", RetrievalChunk.ref_id == data["memory"],
            ).values(project_id=data["foreign"]))
        elif corruption == "foreign_chapter":
            db.add(StoryMemory(project_id=data["foreign"], chapter_id=data["chapter"], chapter_no=1,
                               kind="manual", origin="manual", content="他书记忆"))
        else:
            if corruption == "foreign_source":
                foreign = StoryMemory(project_id=data["foreign"], chapter_no=1, kind="manual", content="他书记忆")
                db.add(foreign)
                await db.flush()
                ref_id = foreign.id
            else:
                ref_id = 999999
            await retrieval_service.upsert_many(db, data["project"], "memory", [
                retrieval_service.ChunkInput(ref_id=ref_id, text="异常索引", chapter_no=1),
            ])
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await chapter_numbering.shift_chapter_numbers(db, data["project"], from_no=1, delta=1)
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, data["chapter"])).chapter_no == 1
        assert (await db.get(StoryMemory, data["memory"])).chapter_no == 1
        index = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == data["memory"],
        ))
        assert index.chapter_no == 1


async def _seed_plan_shift(app, client, *, build):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        outline = Outline(project_id=data["project"], chapter_no=1, title="大纲")
        db.add(outline)
        await db.flush()
        first = await db.get(Chapter, data["chapter"])
        first.outline_id, first.content, first.word_count = outline.id, "", 0
        later = Chapter(project_id=data["project"], chapter_no=2, title="后章", content="")
        db.add(later)
        await db.flush()
        first_plan = ChapterPlan(project_id=data["project"], outline_id=outline.id, seq=1, title="第一条")
        second_plan = ChapterPlan(project_id=data["project"], outline_id=outline.id, seq=2, title="第二条")
        db.add_all([first_plan, second_plan])
        await db.flush()
        if not build:
            later.outline_id = outline.id
            first.plan_id, later.plan_id = first_plan.id, second_plan.id
            first_plan.chapter_id, second_plan.chapter_id = first.id, later.id
        memory = StoryMemory(project_id=data["project"], chapter_id=later.id, chapter_no=2,
                             kind="manual", origin="manual", content="后章记忆")
        db.add(memory)
        await db.flush()
        await retrieval_service.upsert_many(db, data["foreign"], "memory", [
            retrieval_service.ChunkInput(ref_id=memory.id, text="他书索引", chapter_no=2),
        ])
        await db.commit()
        return data | {"outline": outline.id, "later": later.id, "plans": [first_plan.id, second_plan.id]}


@pytest.mark.parametrize("agent_session", [False, True])
@pytest.mark.parametrize("build", [False, True])
@pytest.mark.parametrize("late", [False, True])
async def test_plan_shift_conflict_cannot_commit_partial_changes(app, client, monkeypatch, build, agent_session, late):
    data = await _seed_plan_shift(app, client, build=build)
    called = []
    if late:
        async with app.state.sessionmaker() as db:
            memory_id = await db.scalar(select(StoryMemory.id).where(StoryMemory.chapter_id == data["later"]))
            await db.execute(update(RetrievalChunk).where(
                RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory_id,
            ).values(project_id=data["project"]))
            await db.commit()

        async def later_conflict(*args, **kwargs):
            called.append(True)
            raise ConflictError("模拟后续关联冲突")

        monkeypatch.setattr(plan_service, "shift_chapter_numbers", later_conflict)
    async with app.state.sessionmaker() as db:
        outer = None
        if agent_session:
            from app.services.project_lifecycle import lock_active

            await lock_active(db, data["project"])
            outer = await db.begin_nested()
        service_db = TransactionSession(db) if agent_session else db
        with pytest.raises(ConflictError):
            if build:
                await plan_service.build_chapters(service_db, data["user"], data["project"], data["outline"])
            else:
                await plan_service.sync_plans(
                    service_db, data["user"], data["project"], data["outline"],
                    PlansSync.model_validate({"plans": [{"id": data["plans"][1], "title": "第二条"}]}),
                )
        if outer is not None:
            await outer.commit()
        await db.commit()
    if late:
        assert called == [True]
    async with app.state.sessionmaker() as db:
        first, later = await db.get(Chapter, data["chapter"]), await db.get(Chapter, data["later"])
        plans = [await db.get(ChapterPlan, pk) for pk in data["plans"]]
        assert first is not None and later is not None and [p.title for p in plans] == ["第一条", "第二条"]
        assert first.chapter_no == 1 and later.chapter_no == 2
        if build:
            assert first.plan_id is None and all(p.chapter_id is None for p in plans)
        else:
            assert first.plan_id == plans[0].id and later.plan_id == plans[1].id
            assert [p.chapter_id for p in plans] == [first.id, later.id]
        assert await db.scalar(select(func.count(OutlineSnapshot.id))) == 0


@pytest.mark.parametrize("build", [False, True])
async def test_plan_no_shift_does_not_reject_unrelated_bad_index(app, client, build):
    data = await _seed_plan_shift(app, client, build=build)
    async with app.state.sessionmaker() as db:
        second_plan = await db.get(ChapterPlan, data["plans"][1])
        if build:
            await db.delete(second_plan)
        else:
            later = await db.get(Chapter, data["later"])
            second_plan.chapter_id = None
            later.plan_id, later.outline_id = None, None
        await db.commit()
    async with app.state.sessionmaker() as db:
        if build:
            result = await plan_service.build_chapters(db, data["user"], data["project"], data["outline"])
            assert len(result.chapters) == 1 and result.chapters[0].id == data["chapter"]
        else:
            result = await plan_service.sync_plans(
                db, data["user"], data["project"], data["outline"],
                PlansSync.model_validate({"plans": [{"id": data["plans"][1], "title": "第二条"}]}),
            )
            assert [plan.id for plan in result] == [data["plans"][1]]
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, data["later"])).chapter_no == 2
        memory_id = await db.scalar(select(StoryMemory.id).where(StoryMemory.chapter_id == data["later"]))
        index = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory_id,
        ))
        assert index.project_id == data["foreign"] and index.chapter_no == 2


@pytest.mark.parametrize("build", [False, True])
async def test_plan_preflight_does_not_flush_staged_binding_changes(app, client, build):
    data = await _seed_plan_shift(app, client, build=build)
    async with app.state.sessionmaker() as db:
        plan_id = data["plans"][0] if build else data["plans"][1]
        plan = await db.get(ChapterPlan, plan_id)
        persisted = plan.chapter_id
        plan.chapter_id = data["later"] if build else None
        with pytest.raises(ConflictError):
            if build:
                await plan_service.build_chapters(db, data["user"], data["project"], data["outline"])
            else:
                await plan_service.sync_plans(
                    db, data["user"], data["project"], data["outline"],
                    PlansSync.model_validate({"plans": [{"id": plan_id, "title": "第二条"}]}),
                )
        with db.no_autoflush:
            assert await db.scalar(select(ChapterPlan.chapter_id).where(ChapterPlan.id == plan_id)) == persisted
        await db.rollback()
