"""章节删除及改号不得经损坏的跨书引用影响其他作品。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Column, ForeignKey, Integer, Table, select

from app.core.errors import ConflictError
from app.models import (
    AiUsageCall,
    AnalysisChange,
    Base,
    Chapter,
    ChapterAnalysis,
    ChapterCharacter,
    ChapterPlan,
    Character,
    ConsistencyLedger,
    GenerationLog,
    Outline,
    PlanningUnit,
    RetrievalChunk,
    RevisionBatch,
    RevisionBatchScene,
    Scene,
    SceneMarker,
    SceneReviewAnalysisReceipt,
    SceneReviewProposal,
    SceneSection,
    SerializationEdition,
    SerializationEntry,
    SerializationPublication,
    SettingProgression,
    SettingTimeline,
    StoryMemory,
    Task,
)
from app.services.chapter_delete_guard import ensure_chapter_delete_isolation
from tests.helpers import create_project, register


@pytest.fixture
async def books(app, client):
    user = await register(client, "chapter-guard-owner")
    first, second = await create_project(client, title="一书"), await create_project(client, title="二书")
    chapters = []
    for book in (first, second):
        response = await client.post(f"/api/v1/projects/{book['id']}/chapters", json={
            "title": "正文", "content": "故事内容",
        })
        assert response.status_code == 201, response.text
        chapters.append(response.json())
    async with app.state.sessionmaker() as db:
        scene_ids = [await db.scalar(select(Scene.id).where(Scene.chapter_id == chapter["id"])) for chapter in chapters]
    return user, first, second, chapters, scene_ids


@pytest.mark.parametrize("reference", [
    "plan", "planning_chapter", "planning_scene", "log", "task", "analysis", "memory", "retrieval",
    "character", "progression", "analysis_change", "proposal", "receipt", "marker_origin", "section_origin",
    "ledger", "revision_batch", "revision_scene", "usage", "serialization_entry", "serialization_publication",
])
async def test_chapter_deletion_rejects_cross_project_cascade(app, client, books, reference):
    user, first, second, chapters, scenes = books
    first_id, second_id = first["id"], second["id"]
    chapter_id, other_chapter_id = chapters[0]["id"], chapters[1]["id"]
    async with app.state.sessionmaker() as db:
        if reference == "plan":
            outline = Outline(project_id=second_id, chapter_no=1, title="他书大纲")
            db.add(outline)
            await db.flush()
            foreign = ChapterPlan(project_id=second_id, outline_id=outline.id, seq=1, title="他书细纲",
                                  chapter_id=chapter_id)
        elif reference == "planning_chapter":
            foreign = PlanningUnit(project_id=second_id, chapter_id=chapter_id)
        elif reference == "planning_scene":
            foreign = PlanningUnit(project_id=second_id, scene_id=scenes[0], source_level="scene")
        elif reference == "log":
            foreign = GenerationLog(project_id=second_id, actor_user_id=user["id"], chapter_id=chapter_id,
                                    kind="generate", provider="test", model="test", status="success")
        elif reference == "task":
            foreign = Task(user_id=user["id"], project_id=second_id, chapter_id=chapter_id,
                           kind="chapter.analyze", status="succeeded")
        elif reference == "usage":
            foreign = AiUsageCall(id=str(uuid4()), started_at=datetime.now(UTC), project_id=second_id,
                                  had_project=True, chapter_id=chapter_id, actor_id=user["id"],
                                  payer_id=user["id"], kind="chapter.generate", provider="test", model="test",
                                  model_type="text")
        elif reference == "serialization_entry":
            foreign = SerializationEntry(project_id=second_id, chapter_id=chapter_id)
        elif reference == "serialization_publication":
            entry = SerializationEntry(project_id=first_id, chapter_id=chapter_id)
            db.add(entry)
            await db.flush()
            edition = SerializationEdition(entry_id=entry.id, seq=1, chapter_no=1, title="标题",
                                           content="定稿", fingerprint="a" * 64)
            db.add(edition)
            await db.flush()
            foreign = SerializationPublication(project_id=second_id, edition_id=edition.id,
                                               channel="签约平台", published_at=datetime.now(UTC))
        elif reference == "analysis":
            foreign = ChapterAnalysis(project_id=second_id, chapter_id=chapter_id, chapter_no=1, content_hash="a")
        elif reference in {"memory", "retrieval"}:
            memory = StoryMemory(project_id=second_id if reference == "memory" else first_id, chapter_id=chapter_id,
                                 chapter_no=1, kind="summary", origin="auto", content="不应丢失")
            db.add(memory)
            await db.flush()
            foreign = memory if reference == "memory" else RetrievalChunk(
                project_id=second_id, source="memory", ref_id=memory.id, text="他书索引",
            )
        elif reference == "character":
            character = Character(project_id=second_id, name="他书角色")
            db.add(character)
            await db.flush()
            foreign = ChapterCharacter(chapter_id=chapter_id, character_id=character.id)
        elif reference == "progression":
            character = Character(project_id=second_id, name="他书角色")
            db.add(character)
            await db.flush()
            timeline = SettingTimeline(project_id=second_id, subject_kind="character", character_id=character.id)
            db.add(timeline)
            await db.flush()
            foreign = SettingProgression(timeline_id=timeline.id, field_key="current_state", operation="replace",
                                         value="他书进展", anchor_kind="chapter", chapter_id=chapter_id)
        elif reference == "analysis_change":
            analysis = ChapterAnalysis(project_id=first_id, chapter_id=chapter_id, chapter_no=1, content_hash="a")
            db.add(analysis)
            await db.flush()
            foreign = AnalysisChange(project_id=second_id, analysis_id=analysis.id, chapter_no=1,
                                     kind="character", status="pending")
        elif reference in {"proposal", "receipt"}:
            task = Task(user_id=user["id"], project_id=second_id, chapter_id=other_chapter_id,
                        kind="chapter.generate", status="succeeded")
            db.add(task)
            await db.flush()
            if reference == "proposal":
                foreign = SceneReviewProposal(task_id=task.id, chapter_id=chapter_id, task_key=task.statistics_key,
                                              run_key=str(uuid4()), token=str(uuid4()), content_revision=0, payload={})
            else:
                foreign = SceneReviewAnalysisReceipt(run_key=str(uuid4()), task_key=task.statistics_key,
                                                     chapter_id=chapter_id, content_hash="a", status="running")
        elif reference == "ledger":
            foreign = ConsistencyLedger(project_id=second_id, chapter_id=chapter_id, source_hash="a" * 64,
                                        text_hash="a" * 64, facts=[])
        elif reference in {"revision_batch", "revision_scene"}:
            batch = RevisionBatch(project_id=second_id if reference == "revision_batch" else first_id,
                                  query="原词", replacement="新词", status="applied", chapter_count=1, hit_count=1)
            db.add(batch)
            await db.flush()
            foreign = RevisionBatchScene(batch_id=batch.id, chapter_id=chapter_id,
                                         scene_id=scenes[0] if reference == "revision_batch" else scenes[1],
                                         chapter_slot=1, chapter_no_at_apply=1, chapter_title="正文",
                                         applied_revision=0, before_document={}, start_offsets=[0], hit_count=1)
        elif reference == "marker_origin":
            source = SceneMarker(scene_id=scenes[0], logical_key=str(uuid4()), status="deleted", color="yellow")
            db.add(source)
            await db.flush()
            foreign = SceneMarker(scene_id=scenes[1], logical_key=str(uuid4()), status="deleted", color="yellow",
                                  origin_marker_id=source.id)
        else:
            source = SceneSection(scene_id=scenes[0], logical_key=str(uuid4()), anchor=0, sort=0, title="原区块")
            db.add(source)
            await db.flush()
            foreign = SceneSection(scene_id=scenes[1], logical_key=str(uuid4()), anchor=0, sort=0, title="他书区块",
                                   origin_section_id=source.id)
        db.add(foreign)
        await db.commit()
    response = await client.delete(f"/api/v1/projects/{first_id}/chapters/{chapter_id}")
    assert response.status_code == 409 and response.json()["code"] == "chapter_reference_invalid", response.text
    assert response.json() == {
        "code": "chapter_reference_invalid", "detail": "章节存在无法安全处理的关联，请联系作者检查",
    }
    async with app.state.sessionmaker() as db:
        assert await db.get(Chapter, chapter_id) is not None


async def test_unregistered_metadata_foreign_key_blocks_chapter_deletion(app, books):
    _, first, _, chapters, _ = books
    new_table = Table("unreviewed_chapter_cascade", Base.metadata,
                      Column("id", Integer, primary_key=True),
                      Column("chapter_id", Integer, ForeignKey("chapters.id", ondelete="CASCADE")))
    try:
        async with app.state.sessionmaker() as db:
            with pytest.raises(ConflictError) as error:
                await ensure_chapter_delete_isolation(db, first["id"], [chapters[0]["id"]])
            assert error.value.extra["code"] == "chapter_reference_invalid"
    finally:
        Base.metadata.remove(new_table)


@pytest.mark.parametrize("reference", ["memory", "retrieval"])
async def test_renumber_does_not_modify_cross_project_memory_or_index(app, client, books, reference):
    _, first, second, chapters, _ = books
    chapter_id = chapters[0]["id"]
    async with app.state.sessionmaker() as db:
        memory = StoryMemory(project_id=second["id"] if reference == "memory" else first["id"],
                             chapter_id=chapter_id, chapter_no=1, kind="summary", origin="auto")
        db.add(memory)
        await db.flush()
        if reference == "retrieval":
            db.add(RetrievalChunk(project_id=second["id"], source="memory", ref_id=memory.id,
                                  text="他书索引", chapter_no=1))
        await db.commit()
        memory_id = memory.id
    response = await client.patch(f"/api/v1/projects/{first['id']}/chapters/{chapter_id}", json={"chapter_no": 9})
    assert response.status_code == 409 and response.json()["code"] == "chapter_reference_invalid", response.text
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter_id)).chapter_no == 1
        assert (await db.get(StoryMemory, memory_id)).chapter_no == 1
        if reference == "retrieval":
            assert (await db.scalar(select(RetrievalChunk).where(RetrievalChunk.ref_id == memory_id))).chapter_no == 1


async def test_same_project_cascade_references_keep_existing_deletion_behavior(app, client, books):
    user, first, _, chapters, scenes = books
    project_id, chapter_id = first["id"], chapters[0]["id"]
    usage_id = str(uuid4())
    other = await client.post(f"/api/v1/projects/{project_id}/chapters", json={"title": "同书另一章"})
    assert other.status_code == 201, other.text
    async with app.state.sessionmaker() as db:
        other_scene = await db.scalar(select(Scene.id).where(Scene.chapter_id == other.json()["id"]))
        outline = Outline(project_id=project_id, chapter_no=1, title="本书大纲")
        character = Character(project_id=project_id, name="本书角色")
        task = Task(user_id=user["id"], project_id=project_id, chapter_id=chapter_id,
                    kind="chapter.analyze", status="succeeded")
        analysis = ChapterAnalysis(project_id=project_id, chapter_id=chapter_id, chapter_no=1, content_hash="a")
        db.add_all([outline, character, task, analysis])
        await db.flush()
        plan = ChapterPlan(project_id=project_id, outline_id=outline.id, seq=1, title="本书细纲", chapter_id=chapter_id)
        timeline = SettingTimeline(project_id=project_id, subject_kind="character", character_id=character.id)
        memory = StoryMemory(project_id=project_id, chapter_id=chapter_id, chapter_no=1, kind="summary", origin="auto")
        source_marker = SceneMarker(scene_id=scenes[0], logical_key=str(uuid4()), status="deleted", color="yellow")
        source_section = SceneSection(scene_id=scenes[0], logical_key=str(uuid4()), anchor=0, sort=0, title="原区块")
        db.add_all([plan, timeline, memory, source_marker, source_section])
        await db.flush()
        chapter = await db.get(Chapter, chapter_id)
        chapter.outline_id, chapter.plan_id = outline.id, plan.id
        db.add_all([
            PlanningUnit(project_id=project_id, chapter_id=chapter_id, plan_id=plan.id),
            PlanningUnit(project_id=project_id, scene_id=scenes[0], source_level="scene"),
            ChapterCharacter(chapter_id=chapter_id, character_id=character.id),
            SettingProgression(timeline_id=timeline.id, field_key="current_state", operation="replace",
                               value="本书进展", anchor_kind="chapter", chapter_id=chapter_id),
            AnalysisChange(project_id=project_id, analysis_id=analysis.id, chapter_no=1,
                           kind="character", status="pending"),
            GenerationLog(project_id=project_id, actor_user_id=user["id"], chapter_id=chapter_id,
                          kind="generate", provider="test", model="test", status="success"),
            AiUsageCall(id=usage_id, started_at=datetime.now(UTC), project_id=project_id, had_project=True,
                        chapter_id=chapter_id, actor_id=user["id"], payer_id=user["id"], author_paid=True,
                        kind="chapter.generate", provider="test", model="test", model_type="text"),
            RetrievalChunk(project_id=project_id, source="memory", ref_id=memory.id, text="本书索引"),
            SceneReviewProposal(task_id=task.id, chapter_id=chapter_id, task_key=task.statistics_key,
                                run_key=str(uuid4()), token=str(uuid4()), content_revision=0, payload={}),
            SceneReviewAnalysisReceipt(run_key=str(uuid4()), task_key=task.statistics_key, chapter_id=chapter_id,
                                       analysis_id=analysis.id, content_hash="a", status="running"),
            SceneMarker(scene_id=other_scene, logical_key=str(uuid4()), status="deleted", color="yellow",
                        origin_marker_id=source_marker.id),
            SceneSection(scene_id=other_scene, logical_key=str(uuid4()), anchor=0, sort=0, title="同书区块",
                         origin_section_id=source_section.id),
        ])
        await db.commit()
        plan_id = plan.id
    response = await client.delete(f"/api/v1/projects/{project_id}/chapters/{chapter_id}")
    assert response.status_code == 204, response.text
    async with app.state.sessionmaker() as db:
        assert await db.get(Chapter, chapter_id) is None
        assert (await db.get(ChapterPlan, plan_id)).chapter_id is None
        assert await db.get(Chapter, other.json()["id"]) is not None
        usage = await db.get(AiUsageCall, usage_id)
        assert usage is not None and usage.chapter_id is None and usage.project_id == project_id
