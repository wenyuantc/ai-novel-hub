"""检索来源损坏时不能以空上下文继续发起项目 AI 请求。"""

import pytest

from app.models import Chapter, KbChunk, KbDocument, Project, StoryMemory
from app.services import kb_service, memory_service, retrieval_service
from app.services.ai_dispatch import VisibilityPolicyError
from tests.helpers import create_project, register


async def test_knowledge_context_fails_closed_on_foreign_source(app, client):
    await app.state.task_engine.stop()
    await register(client, "context-owner")
    project = await create_project(client)
    foreign = await create_project(client, title="其他作品")
    async with app.state.sessionmaker() as db:
        doc = KbDocument(project_id=foreign["id"], filename="他书资料.txt", sha256="a" * 64,
                         text="他书隐藏正文", status="ready", chunk_count=1)
        db.add(doc)
        await db.flush()
        chunk = KbChunk(document_id=doc.id, seq=1, content=doc.text)
        db.add(chunk)
        await db.flush()
        await retrieval_service.upsert_many(db, project["id"], "kb", [
            retrieval_service.ChunkInput(ref_id=chunk.id, text=chunk.content),
        ])
        await db.commit()
        current = await db.get(Project, project["id"])
        with pytest.raises(VisibilityPolicyError) as error:
            await kb_service.knowledge_text(db, current, "他书隐藏正文", embedder=None)
        assert "他书隐藏正文" not in str(error.value)


async def test_pinned_memory_context_fails_closed_on_foreign_chapter(app, client):
    await app.state.task_engine.stop()
    await register(client, "pinned-owner")
    project = await create_project(client)
    foreign = await create_project(client, title="其他作品")
    async with app.state.sessionmaker() as db:
        chapter = Chapter(project_id=foreign["id"], chapter_no=1, title="他书章节")
        db.add(chapter)
        await db.flush()
        memory = StoryMemory(project_id=project["id"], chapter_id=chapter.id, chapter_no=1, kind="manual",
                             origin="manual", content="不应召回的记忆", pinned=True)
        db.add(memory)
        await db.commit()
        current = await db.get(Project, project["id"])
        with pytest.raises(VisibilityPolicyError) as error:
            await memory_service.recall(db, current, 2, "", embedder=None)
        assert "不应召回的记忆" not in str(error.value)
