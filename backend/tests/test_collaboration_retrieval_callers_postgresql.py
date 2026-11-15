"""使用两个真实 PostgreSQL 连接验证项目屏障与知识库来源预检。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text, update

from app.core.errors import ConflictError
from app.models import Chapter, ChapterPlan, KbChunk, KbDocument, Outline, RetrievalChunk, StoryMemory
from app.schemas.outline import PlansSync
from app.services import chapter_numbering, plan_service, project_lifecycle, retrieval_service
from tests.helpers import create_project, register
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_rowlock_postgresql import _assert_blocked_on_project_row

pytest_plugins = ("tests.test_collaboration_retrieval_postgresql",)


async def _seed(app, owner):
    await register(owner, "retrieval-callers-pg")
    project, other = await create_project(owner), await create_project(owner)
    async with app.state.sessionmaker() as db:
        doc = KbDocument(project_id=project["id"], filename="源.txt", sha256="f" * 64, text="本书正文")
        db.add(doc)
        await db.flush()
        chunks = [KbChunk(document_id=doc.id, seq=1, content="甲"), KbChunk(document_id=doc.id, seq=2, content="乙")]
        db.add_all(chunks)
        await db.flush()
        await retrieval_service.upsert_project_sources(db, project["id"], "kb", [
            retrieval_service.ChunkInput(ref_id=c.id, text=c.content) for c in chunks
        ])
        document_id, ref_ids = doc.id, [c.id for c in chunks]
        await db.commit()
    return project["id"], other["id"], document_id, ref_ids


@pytest.mark.parametrize("action", ["patch", "delete"])
async def test_pg_kb_write_waits_then_rejects_committed_foreign_index(postgres_retrieval_apps, action):
    app, other_app = postgres_retrieval_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://testserver") as contender,
    ):
        project_id, foreign_id, document_id, refs = await _seed(app, owner)
        contender.cookies.update(owner.cookies)
        pending = None
        try:
            async with app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project_id)
                await holder.execute(update(RetrievalChunk).where(
                    RetrievalChunk.source == "kb", RetrievalChunk.ref_id == refs[-1],
                ).values(project_id=foreign_id, text="外书秘密"))
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                url = f"/api/v1/projects/{project_id}/kb/documents/{document_id}"
                pending = asyncio.create_task(contender.patch(url, json={"inject": False}) if action == "patch"
                                              else contender.delete(url))
                await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, pid), 10)
                await _assert_blocked_on_project_row(other_app.state.sessionmaker, pid)
                assert not pending.done()
                await holder.commit()
            response = await asyncio.wait_for(pending, 10)
            assert response.status_code == 409 and response.json()["code"] == "retrieval_reference_invalid"
            assert "外书秘密" not in response.text and "project_id" not in response.text
            async with app.state.sessionmaker() as db:
                doc = await db.get(KbDocument, document_id)
                rows = list(await db.scalars(select(RetrievalChunk).where(
                    RetrievalChunk.source == "kb", RetrievalChunk.ref_id.in_(refs),
                )))
                assert doc.inject is True and len(rows) == 2 and all(row.enabled for row in rows)
                assert next(row for row in rows if row.ref_id == refs[-1]).project_id == foreign_id
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_pg_source_mutation_waits_for_inject_transaction(postgres_retrieval_apps, monkeypatch):
    app, other_app = postgres_retrieval_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://testserver") as contender,
    ):
        project_id, foreign_id, document_id, refs = await _seed(app, owner)
        contender.cookies.update(owner.cookies)
        entered, release = asyncio.Event(), asyncio.Event()
        original = retrieval_service.validate_project_source_refs
        holder_pid = []

        async def gated(db, pid, source, ids):
            if pid == project_id and source == "kb" and not holder_pid:
                holder_pid.append(await db.scalar(text("SELECT pg_backend_pid()")))
                entered.set()
                await release.wait()
            return await original(db, pid, source, ids)

        monkeypatch.setattr(retrieval_service, "validate_project_source_refs", gated)
        request = asyncio.create_task(contender.patch(
            f"/api/v1/projects/{project_id}/kb/documents/{document_id}", json={"inject": False},
        ))
        mutation = None
        try:
            await asyncio.wait_for(entered.wait(), 10)

            async def change_index():
                async with app.state.sessionmaker() as db:
                    await project_lifecycle.lock_active(db, project_id)
                    await db.execute(update(RetrievalChunk).where(
                        RetrievalChunk.source == "kb", RetrievalChunk.ref_id == refs[-1],
                    ).values(project_id=foreign_id))
                    await db.commit()

            mutation = asyncio.create_task(change_index())
            await asyncio.wait_for(_wait_for_blocker(app.state.sessionmaker, holder_pid[0]), 10)
            await _assert_blocked_on_project_row(app.state.sessionmaker, holder_pid[0])
            assert not mutation.done()
            release.set()
            response = await asyncio.wait_for(request, 10)
            assert response.status_code == 200, response.text
            await asyncio.wait_for(mutation, 10)
            async with app.state.sessionmaker() as db:
                doc = await db.get(KbDocument, document_id)
                rows = list(await db.scalars(select(RetrievalChunk).where(
                    RetrievalChunk.source == "kb", RetrievalChunk.ref_id.in_(refs),
                )))
                assert doc.inject is False and len(rows) == 2 and all(not row.enabled for row in rows)
        finally:
            release.set()
            for task in (request, mutation):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (request, mutation) if t is not None), return_exceptions=True)


async def test_pg_chapter_shift_rechecks_foreign_index_after_project_lock(postgres_retrieval_apps):
    app, other_app = postgres_retrieval_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner:
        project_id, foreign_id, _, _ = await _seed(app, owner)
        async with app.state.sessionmaker() as db:
            chapter = Chapter(project_id=project_id, chapter_no=1, title="章节")
            db.add(chapter)
            await db.flush()
            memory = StoryMemory(project_id=project_id, chapter_id=chapter.id, chapter_no=1,
                                 kind="manual", origin="manual", content="记忆")
            db.add(memory)
            await db.flush()
            await retrieval_service.upsert_project_sources(db, project_id, "memory", [
                retrieval_service.ChunkInput(ref_id=memory.id, text=memory.content, chapter_no=1),
            ])
            chapter_id, memory_id = chapter.id, memory.id
            await db.commit()
        pending = None
        try:
            async with app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project_id)
                await holder.execute(update(RetrievalChunk).where(
                    RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory_id,
                ).values(project_id=foreign_id))
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))

                async def shift():
                    async with other_app.state.sessionmaker() as db:
                        await project_lifecycle.lock_active(db, project_id)
                        try:
                            await chapter_numbering.shift_chapter_numbers(db, project_id, from_no=1, delta=1)
                        except ConflictError:
                            await db.commit()
                            return True
                        await db.commit()
                        return False

                pending = asyncio.create_task(shift())
                await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, pid), 10)
                await _assert_blocked_on_project_row(other_app.state.sessionmaker, pid)
                assert not pending.done()
                await holder.commit()
            assert await asyncio.wait_for(pending, 10)
            async with app.state.sessionmaker() as db:
                assert (await db.get(Chapter, chapter_id)).chapter_no == 1
                assert (await db.get(StoryMemory, memory_id)).chapter_no == 1
                row = await db.scalar(select(RetrievalChunk).where(
                    RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory_id,
                ))
                assert row.project_id == foreign_id and row.chapter_no == 1
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


@pytest.mark.parametrize("build", [False, True])
async def test_pg_plan_operation_rechecks_shift_before_mutation(postgres_retrieval_apps, build):
    app, other_app = postgres_retrieval_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner:
        project_id, foreign_id, _, _ = await _seed(app, owner)
        async with app.state.sessionmaker() as db:
            from app.models import Project

            user_id = await db.scalar(select(Project.user_id).where(Project.id == project_id))
            outline = Outline(project_id=project_id, chapter_no=1, title="大纲")
            db.add(outline)
            await db.flush()
            first = Chapter(project_id=project_id, outline_id=outline.id, chapter_no=1, title="第一章", content="")
            later = Chapter(project_id=project_id, chapter_no=2, title="第二章", content="")
            db.add_all([first, later])
            await db.flush()
            first_plan = ChapterPlan(project_id=project_id, outline_id=outline.id, seq=1, title="第一条")
            second_plan = ChapterPlan(project_id=project_id, outline_id=outline.id, seq=2, title="第二条")
            db.add_all([first_plan, second_plan])
            await db.flush()
            if not build:
                later.outline_id = outline.id
                first.plan_id, later.plan_id = first_plan.id, second_plan.id
                first_plan.chapter_id, second_plan.chapter_id = first.id, later.id
            memory = StoryMemory(project_id=project_id, chapter_id=later.id, chapter_no=2,
                                 kind="manual", origin="manual", content="后章记忆")
            db.add(memory)
            await db.flush()
            await retrieval_service.upsert_project_sources(db, project_id, "memory", [
                retrieval_service.ChunkInput(ref_id=memory.id, text=memory.content, chapter_no=2),
            ])
            outline_id, first_id, later_id, memory_id = outline.id, first.id, later.id, memory.id
            plan_ids = [first_plan.id, second_plan.id]
            await db.commit()
        pending = None
        try:
            async with app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project_id)
                await holder.execute(update(RetrievalChunk).where(
                    RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory_id,
                ).values(project_id=foreign_id))
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))

                async def apply_plan():
                    async with other_app.state.sessionmaker() as db:
                        try:
                            if build:
                                await plan_service.build_chapters(db, user_id, project_id, outline_id)
                            else:
                                await plan_service.sync_plans(db, user_id, project_id, outline_id,
                                    PlansSync.model_validate({"plans": [{"id": plan_ids[1], "title": "第二条"}]}))
                        except ConflictError:
                            await db.commit()
                            return True
                        await db.commit()
                        return False

                pending = asyncio.create_task(apply_plan())
                await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, pid), 10)
                await _assert_blocked_on_project_row(other_app.state.sessionmaker, pid)
                assert not pending.done()
                await holder.commit()
            assert await asyncio.wait_for(pending, 10)
            async with app.state.sessionmaker() as db:
                first, later = await db.get(Chapter, first_id), await db.get(Chapter, later_id)
                plans = [await db.get(ChapterPlan, plan_id) for plan_id in plan_ids]
                assert first is not None and later is not None and (first.chapter_no, later.chapter_no) == (1, 2)
                if build:
                    assert first.plan_id is None and all(plan.chapter_id is None for plan in plans)
                else:
                    assert (first.plan_id, later.plan_id) == tuple(plan_ids)
                    assert [plan.chapter_id for plan in plans] == [first_id, later_id]
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
