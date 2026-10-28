"""撤销使用真实 SQLite 外键 / 事务，覆盖级联、并发修改与检索恢复。"""

import json
from datetime import datetime

import pytest
from sqlalchemy import delete, select, update

from app.core.errors import ConflictError
from app.models import (
    AnalysisChange,
    Career,
    CareerHistory,
    CareerStage,
    Chapter,
    ChapterAnalysis,
    ChapterCharacter,
    ChapterPlan,
    ChapterVersion,
    Character,
    CharacterCareer,
    Foreshadow,
    ForeshadowCharacter,
    GenerationLog,
    Organization,
    OrganizationMember,
    Outline,
    Project,
    Relationship,
    RelationshipHistory,
    RelationshipType,
    RetrievalChunk,
    StoryMemory,
    Task,
    User,
)
from app.services import memory_service, retrieval_service, version_service
from app.services.agent import effects
from app.services.chapter_numbering import CHAPTER_NO_REFS, shift_chapter_numbers
from tests.test_chapter_numbering import _seed, _values


@pytest.fixture
async def project_db(app):
    async with app.state.sessionmaker() as db:
        user = User(username="undo", password_hash="unused")
        db.add(user)
        await db.flush()
        project = Project(user_id=user.id, title="撤销测试")
        db.add(project)
        await db.commit()
        yield db, project


async def _character_graph(db, project_id):
    a = Character(project_id=project_id, name="甲")
    b = Character(project_id=project_id, name="乙")
    org = Organization(project_id=project_id, name="宗门")
    career = Career(project_id=project_id, name="剑修")
    chapter = Chapter(project_id=project_id, title="启程", content="正文", word_count=2)
    foreshadow = Foreshadow(project_id=project_id, title="玉佩")
    db.add_all([a, b, org, career, chapter, foreshadow])
    await db.flush()
    stage = CareerStage(career_id=career.id, position=1, name="初阶")
    type_id = await db.scalar(select(RelationshipType.id).where(RelationshipType.project_id.is_(None)).limit(1))
    relationship = Relationship(
        project_id=project_id, a_id=a.id, b_id=b.id, pair_low=a.id, pair_high=b.id, type_id=type_id
    )
    db.add_all([stage, relationship])
    await db.flush()
    db.add_all(
        [
            OrganizationMember(organization_id=org.id, character_id=a.id, position="长老"),
            CharacterCareer(character_id=a.id, career_id=career.id, stage_id=stage.id, role="main"),
            CareerHistory(character_id=a.id, career_id=career.id, source="manual", changes={"stage": [None, "初阶"]}),
            RelationshipHistory(relationship_id=relationship.id, source="manual", changes={"intimacy": [0, 30]}),
            ChapterCharacter(chapter_id=chapter.id, character_id=a.id),
            ForeshadowCharacter(foreshadow_id=foreshadow.id, character_id=a.id),
        ]
    )
    await db.commit()
    return a, org, career


async def test_delete_character_restores_all_associations_and_original_ids(project_db):
    db, project = project_db
    character, _, _ = await _character_graph(db, project.id)
    before = await effects.snapshot(db, project.id)
    await db.delete(character)
    after = await effects.snapshot(db, project.id)
    delta = effects.diff(before, after)
    assert {e["table"] for e in delta} == {
        "characters",
        "organization_members",
        "character_careers",
        "career_history",
        "relationships",
        "relationship_history",
        "chapter_characters",
        "foreshadow_characters",
    }
    await db.commit()
    await effects.undo(db, project.id, json.loads(json.dumps(delta)))
    await db.commit()
    assert await effects.snapshot(db, project.id) == before


async def test_undo_delete_keeps_original_created_at(project_db):
    db, project = project_db
    character, org, _ = await _character_graph(db, project.id)
    old = datetime(2020, 1, 2, 3, 4, 5)
    for model, row_id in ((Character, character.id), (Organization, org.id)):
        await db.execute(update(model).where(model.id == row_id).values(created_at=old))
    await db.commit()
    before = await effects.snapshot(db, project.id)
    await db.execute(delete(Organization).where(Organization.id == org.id))
    await db.execute(delete(Character).where(Character.id == character.id))
    after = await effects.snapshot(db, project.id)
    await db.commit()
    await effects.undo(db, project.id, json.loads(json.dumps(effects.diff(before, after))))
    await db.commit()
    for model, row_id in ((Character, character.id), (Organization, org.id)):
        restored = await db.scalar(select(model.created_at).where(model.id == row_id))
        assert restored.replace(tzinfo=None) == old


async def test_snapshot_ignores_stale_identity_map_and_other_projects(project_db):
    db, project = project_db
    character, _, _ = await _character_graph(db, project.id)
    other = Project(user_id=project.user_id, title="其他项目")
    db.add(other)
    await db.flush()
    db.add(Character(project_id=other.id, name="不应出现"))
    await db.commit()
    before = await effects.snapshot(db, project.id)
    await db.execute(
        update(Character)
        .where(Character.id == character.id)
        .values(name="数据库新名")
        .execution_options(synchronize_session=False)
    )
    assert character.name == "甲"
    after = await effects.snapshot(db, project.id)
    assert after["characters"][str(character.id)]["name"] == "数据库新名"
    assert all(row["project_id"] == project.id for row in after["characters"].values())
    assert "updated_at" not in after["characters"][str(character.id)]
    assert all("content" not in row for row in after["chapters"].values())
    delta = effects.diff(before, after)
    await effects.undo(db, project.id, delta)
    assert character.name == "甲"


@pytest.mark.parametrize("kind", ["organization", "career"])
async def test_deleted_parent_restores_children_with_self_and_restrict_fks(project_db, kind):
    db, project = project_db
    _, org, career = await _character_graph(db, project.id)
    child = Organization(project_id=project.id, name="分舵", parent_id=org.id)
    db.add(child)
    await db.commit()
    before = await effects.snapshot(db, project.id)
    await db.delete(org if kind == "organization" else career)
    after = await effects.snapshot(db, project.id)
    await db.commit()
    await effects.undo(db, project.id, effects.diff(before, after))
    assert await effects.snapshot(db, project.id) == before
    if kind == "organization":
        assert child.parent_id == org.id


async def test_undo_new_parent_rejects_later_added_dependent(project_db):
    db, project = project_db
    chapter = Chapter(project_id=project.id, title="一")
    db.add(chapter)
    await db.commit()
    before = await effects.snapshot(db, project.id)
    character = Character(project_id=project.id, name="新角色")
    db.add(character)
    after = await effects.snapshot(db, project.id)
    delta = effects.diff(before, after)
    db.add(ChapterCharacter(chapter_id=chapter.id, character_id=character.id))
    await db.commit()
    current = await effects.snapshot(db, project.id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(db, project.id, delta)
    assert exc.value.extra["code"] == "undo_conflict"
    assert "关联" in exc.value.extra["conflicts"][0]["label"]
    assert await effects.snapshot(db, project.id) == current


async def test_undo_new_character_rejects_later_json_plan_reference(project_db):
    db, project = project_db
    outline = Outline(project_id=project.id, title="大纲")
    db.add(outline)
    await db.flush()
    before = await effects.snapshot(db, project.id)
    character = Character(project_id=project.id, name="新角色")
    db.add(character)
    delta = effects.diff(before, await effects.snapshot(db, project.id))
    db.add(
        ChapterPlan(
            project_id=project.id, outline_id=outline.id, seq=1, title="后来创建的细纲", character_ids=[character.id]
        )
    )
    await db.commit()
    with pytest.raises(ConflictError) as exc:
        await effects.undo(db, project.id, delta)
    assert "角色引用" in exc.value.extra["conflicts"][0]["label"]


async def test_restoring_deleted_id_occupied_by_other_project_returns_conflict(project_db):
    db, project = project_db
    character = Character(project_id=project.id, name="旧角色")
    db.add(character)
    await db.flush()
    character_id = character.id
    before = await effects.snapshot(db, project.id)
    await db.delete(character)
    delta = effects.diff(before, await effects.snapshot(db, project.id))
    other = Project(user_id=project.user_id, title="别的项目")
    db.add(other)
    await db.flush()
    db.add(Character(id=character_id, project_id=other.id, name="占用主键"))
    await db.commit()
    with pytest.raises(ConflictError) as exc:
        await effects.undo(db, project.id, delta)
    assert "主键" in exc.value.extra["conflicts"][0]["label"]
    assert await db.scalar(select(Character.name).where(Character.id == character_id)) == "占用主键"


async def test_undo_new_outline_detaches_existing_chapter_before_deleting(project_db):
    db, project = project_db
    chapter = Chapter(project_id=project.id, title="已有章节", content="保留内容")
    db.add(chapter)
    await db.commit()
    before = await effects.snapshot(db, project.id)
    outline = Outline(project_id=project.id, title="新大纲")
    db.add(outline)
    await db.flush()
    chapter.outline_id = outline.id
    delta = effects.diff(before, await effects.snapshot(db, project.id))
    await effects.undo(db, project.id, delta)
    assert await effects.snapshot(db, project.id) == before
    assert chapter.content == "保留内容" and chapter.outline_id is None


async def test_modified_row_conflict_is_atomic_even_when_other_rows_can_restore(project_db):
    db, project = project_db
    a = Character(project_id=project.id, name="甲")
    b = Character(project_id=project.id, name="乙")
    db.add_all([a, b])
    await db.commit()
    before = await effects.snapshot(db, project.id)
    a.name, b.name = "甲新", "乙新"
    delta = effects.diff(before, await effects.snapshot(db, project.id))
    await db.commit()
    b.name = "后续手动编辑"
    await db.commit()
    current = await effects.snapshot(db, project.id)
    with pytest.raises(ConflictError):
        await effects.undo(db, project.id, delta)
    assert await effects.snapshot(db, project.id) == current


@pytest.mark.parametrize("outer_savepoint", [False, True])
async def test_undo_turn_can_roll_back_all_items_on_earlier_conflict(project_db, outer_savepoint):
    db, project = project_db
    pid = project.id
    a = Character(project_id=pid, name="甲")
    b = Character(project_id=pid, name="乙")
    db.add_all([a, b])
    await db.commit()
    before = await effects.snapshot(db, pid)
    a.name = "甲新"
    middle = await effects.snapshot(db, pid)
    first = effects.diff(before, middle)
    b.name = "乙新"
    second = effects.diff(middle, await effects.snapshot(db, pid))
    await db.commit()
    a.name = "后续编辑"
    await db.commit()
    current = await effects.snapshot(db, pid)
    if outer_savepoint:
        with pytest.raises(ConflictError):
            async with db.begin_nested():
                await effects.undo(db, pid, second)
                await effects.undo(db, pid, first)
    else:
        with pytest.raises(ConflictError):
            await effects.undo(db, pid, second)
            await effects.undo(db, pid, first)
        await db.rollback()
    assert await effects.snapshot(db, pid) == current


async def test_chapter_number_undo_restores_every_registered_column_and_search(project_db):
    db, project = project_db
    seeded = await _seed(db, project.user_id, "完整章节号", (1, 3, 5))
    pid = seeded["project_id"]
    before_numbers = await _values(db, seeded)
    before = await effects.snapshot(db, pid)
    db.add(Outline(project_id=pid, chapter_no=2, title="插入章节"))
    await shift_chapter_numbers(db, pid, from_no=3, delta=2)
    delta = effects.diff(before, await effects.snapshot(db, pid))
    assert all(
        ref.model.__tablename__ in effects.EFFECT_TABLES or ref.model is RetrievalChunk for ref in CHAPTER_NO_REFS
    )
    await db.commit()
    assert await effects.undo(db, pid, delta) is True
    await db.commit()
    assert await _values(db, seeded) == before_numbers
    assert await effects.snapshot(db, pid) == before


async def test_delete_empty_plan_chapter_restores_versions_analysis_memory_and_links(project_db):
    db, project = project_db
    pid = project.id
    outline = Outline(project_id=pid, title="大纲")
    db.add(outline)
    await db.flush()
    chapter = Chapter(project_id=pid, title="已清空的章节", outline_id=outline.id, content=" \n")
    db.add(chapter)
    await db.flush()
    plan = ChapterPlan(project_id=pid, outline_id=outline.id, seq=1, title="细纲", chapter_id=chapter.id)
    analysis = ChapterAnalysis(project_id=pid, chapter_id=chapter.id, chapter_no=1, content_hash="old")
    log = GenerationLog(project_id=pid, chapter_id=chapter.id, kind="generate", status="success")
    task = Task(
        user_id=project.user_id, project_id=pid, chapter_id=chapter.id, kind="chapter.generate", status="succeeded"
    )
    db.add_all([plan, analysis, log, task])
    await db.flush()
    chapter.plan_id = plan.id
    version = ChapterVersion(
        chapter_id=chapter.id, seq=1, source="manual", content="历史正文", word_count=4, generation_log_id=log.id
    )
    change = AnalysisChange(
        project_id=pid,
        analysis_id=analysis.id,
        chapter_no=1,
        kind="character_state",
        status="applied",
        applied_at=datetime(2026, 1, 1),
    )
    memory = StoryMemory(
        project_id=pid,
        chapter_id=chapter.id,
        chapter_no=1,
        analysis_id=analysis.id,
        kind="summary",
        content="玉佩隐藏线索",
        pinned=True,
    )
    db.add_all([version, change, memory])
    await db.flush()
    await memory_service.index_memories(db, pid, [memory])
    await db.commit()
    before = await effects.snapshot(db, pid)
    # 与 sync_plans 删除空章的实际顺序一致。
    await db.delete(plan)
    await db.flush()
    await memory_service.on_chapters_deleted(db, [chapter.id])
    await db.delete(chapter)
    delta = json.loads(json.dumps(effects.diff(before, await effects.snapshot(db, pid))))
    assert {"chapter_versions", "chapter_analyses", "analysis_changes", "generation_logs", "tasks"} <= {
        e["table"] for e in delta
    }
    await db.commit()
    assert await effects.undo(db, pid, delta) is True
    await db.commit()
    assert await effects.snapshot(db, pid) == before
    indexed = (
        await db.execute(
            select(RetrievalChunk.text, RetrievalChunk.pinned, RetrievalChunk.chapter_no).where(
                RetrievalChunk.project_id == pid
            )
        )
    ).one()
    assert indexed == ("玉佩隐藏线索", True, 1)


async def test_outline_delete_restores_deferred_links_without_touching_chapter_content(project_db):
    db, project = project_db
    outline = Outline(project_id=project.id, title="纲")
    db.add(outline)
    await db.flush()
    chapter = Chapter(project_id=project.id, title="章", content="必须保留正文", outline_id=outline.id)
    db.add(chapter)
    await db.flush()
    plan = ChapterPlan(project_id=project.id, outline_id=outline.id, chapter_id=chapter.id, seq=1, title="细")
    db.add(plan)
    await db.flush()
    chapter.plan_id = plan.id
    await db.commit()
    before = await effects.snapshot(db, project.id)
    await db.delete(outline)
    delta = effects.diff(before, await effects.snapshot(db, project.id))
    await db.commit()
    await effects.undo(db, project.id, delta)
    assert await effects.snapshot(db, project.id) == before
    assert chapter.content == "必须保留正文"
    assert chapter.plan_id == plan.id


async def test_undo_inserted_chapter_does_not_delete_later_written_content(project_db):
    db, project = project_db
    before = await effects.snapshot(db, project.id)
    chapter = Chapter(project_id=project.id, title="新章")
    db.add(chapter)
    delta = effects.diff(before, await effects.snapshot(db, project.id))
    await db.commit()
    chapter.content = "后来写的正文"
    await db.commit()
    with pytest.raises(ConflictError):
        await effects.undo(db, project.id, delta)
    assert await db.scalar(select(Chapter.content).where(Chapter.id == chapter.id)) == "后来写的正文"


async def test_restore_unique_conflict_rolls_back_partial_reinsertion(project_db):
    db, project = project_db
    character, org, _ = await _character_graph(db, project.id)
    pid = project.id
    before = await effects.snapshot(db, pid)
    await db.delete(character)
    await db.delete(org)
    delta = effects.diff(before, await effects.snapshot(db, pid))
    await db.commit()
    db.add(Organization(id=500, project_id=pid, name="宗门"))
    await db.commit()
    current = await effects.snapshot(db, pid)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(db, pid, delta)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await effects.snapshot(db, pid) == current


@pytest.mark.parametrize("changed", [False, True])
async def test_text_restores_by_version_and_rejects_later_content_change(project_db, changed):
    db, project = project_db
    pid = project.id
    chapter = Chapter(project_id=pid, title="文本", content="甲走进山谷。", word_count=6)
    db.add(chapter)
    await db.flush()
    original = await version_service.record(db, chapter, "baseline")
    before = await effects.snapshot(db, pid)
    chapter.content = "甲慢慢走进山谷。"
    latest = await version_service.record(db, chapter, "agent")
    delta = effects.diff(before, await effects.snapshot(db, pid))
    delta.append(
        effects.chapter_text_effect(
            chapter.id,
            original.id,
            latest.id,
            effects.content_hash(original.content),
            effects.content_hash(latest.content),
        )
    )
    await db.commit()
    if changed:
        chapter.content = "后续手动编辑正文"
        await db.commit()
        with pytest.raises(ConflictError):
            await effects.undo(db, pid, delta)
        assert chapter.content == "后续手动编辑正文"
    else:
        await effects.undo(db, pid, delta)
        await db.commit()
        assert chapter.content == "甲走进山谷。"
        assert chapter.word_count == 6
        restored = await version_service.get_latest(db, chapter.id)
        assert restored.source == "restore" and restored.content == original.content
        assert await db.get(ChapterVersion, latest.id) is not None


@pytest.mark.parametrize("changed", ["content", "before_version", "after_version", "generating", "project"])
async def test_text_effect_rechecks_hash_versions_and_project_without_row_diff(project_db, changed):
    db, project = project_db
    chapter = Chapter(project_id=project.id, title="原文校验", content="修改前正文")
    db.add(chapter)
    await db.flush()
    original = await version_service.record(db, chapter, "baseline")
    chapter.content = "修改后正文"
    latest = await version_service.record(db, chapter, "agent")
    delta = effects.chapter_text_effect(
        chapter.id,
        original.id,
        latest.id,
        effects.content_hash(original.content),
        effects.content_hash(latest.content),
    )
    pid = project.id
    if changed == "content":
        chapter.content = "之后手动写入的正文"
    elif changed == "before_version":
        original.content = "历史版本内容已变化"
    elif changed == "after_version":
        await db.delete(latest)
    elif changed == "generating":
        chapter.status = "generating"
    else:
        other = Project(user_id=project.user_id, title="别的项目")
        db.add(other)
        await db.flush()
        pid = other.id
    await db.commit()
    current = chapter.content
    version_ids = list(await db.scalars(select(ChapterVersion.id).where(ChapterVersion.chapter_id == chapter.id)))
    with pytest.raises(ConflictError) as exc:
        await effects.undo(db, pid, [delta])
    assert exc.value.extra["code"] == "undo_conflict"
    assert await db.scalar(select(Chapter.content).where(Chapter.id == chapter.id)) == current
    assert (
        list(await db.scalars(select(ChapterVersion.id).where(ChapterVersion.chapter_id == chapter.id))) == version_ids
    )


async def test_new_private_relationship_type_undo_preserves_global_presets(project_db):
    db, project = project_db
    global_ids = list(await db.scalars(select(RelationshipType.id).where(RelationshipType.project_id.is_(None))))
    before = await effects.snapshot(db, project.id)
    kind = RelationshipType(project_id=project.id, category="other", role_a="剑主", role_b="剑灵")
    db.add(kind)
    delta = effects.diff(before, await effects.snapshot(db, project.id))
    assert [e["table"] for e in delta] == ["relationship_types"]
    await effects.undo(db, project.id, delta)
    assert (
        list(await db.scalars(select(RelationshipType.id).where(RelationshipType.project_id.is_(None)))) == global_ids
    )


async def _pair(db, project_id):
    a = Character(project_id=project_id, name="甲")
    b = Character(project_id=project_id, name="乙")
    db.add_all([a, b])
    await db.commit()
    return a, b


async def test_undo_new_relationship_with_new_type_deletes_child_before_restrict_parent(project_db):
    # relationships.type_id 没有 ondelete（NO ACTION）：必须先删关系、再删新建的自定义类型。
    db, project = project_db
    a, b = await _pair(db, project.id)
    before = await effects.snapshot(db, project.id)
    kind = RelationshipType(project_id=project.id, category="other", role_a="剑主", role_b="剑灵")
    db.add(kind)
    await db.flush()
    rel = Relationship(project_id=project.id, a_id=a.id, b_id=b.id, pair_low=a.id, pair_high=b.id, type_id=kind.id)
    db.add(rel)
    delta = effects.diff(before, await effects.snapshot(db, project.id))
    assert {e["table"] for e in delta} == {"relationship_types", "relationships"}
    await effects.undo(db, project.id, delta)
    await db.commit()
    assert await db.scalar(select(Relationship.id).where(Relationship.project_id == project.id)) is None
    assert await db.scalar(select(RelationshipType.id).where(RelationshipType.project_id == project.id)) is None


async def test_undo_repointed_relationship_restores_reference_before_deleting_new_type(project_db):
    # 既有关系改指向新建类型：撤销时先把引用改回原类型，否则删除新类型会被 NO ACTION 外键拒绝。
    db, project = project_db
    a, b = await _pair(db, project.id)
    preset = await db.scalar(select(RelationshipType.id).where(RelationshipType.project_id.is_(None)).limit(1))
    rel = Relationship(project_id=project.id, a_id=a.id, b_id=b.id, pair_low=a.id, pair_high=b.id, type_id=preset)
    db.add(rel)
    await db.commit()
    rel_id = rel.id
    before = await effects.snapshot(db, project.id)
    kind = RelationshipType(project_id=project.id, category="other", role_a="剑主", role_b="剑灵")
    db.add(kind)
    await db.flush()
    await db.execute(update(Relationship).where(Relationship.id == rel_id).values(type_id=kind.id))
    delta = effects.diff(before, await effects.snapshot(db, project.id))
    await effects.undo(db, project.id, delta)
    await db.commit()
    assert await db.scalar(select(Relationship.type_id).where(Relationship.id == rel_id)) == preset
    assert await db.scalar(select(RelationshipType.id).where(RelationshipType.project_id == project.id)) is None


async def test_restored_muted_memory_remains_excluded_and_insert_undo_removes_index(project_db):
    db, project = project_db
    pid = project.id
    before = await effects.snapshot(db, pid)
    memory = StoryMemory(project_id=pid, chapter_no=1, kind="manual", origin="manual", content="玉佩隐藏线索")
    db.add(memory)
    await db.flush()
    await memory_service.index_memories(db, pid, [memory])
    after = await effects.snapshot(db, pid)
    await db.commit()
    await effects.undo(db, pid, effects.diff(before, after))
    assert await db.scalar(select(RetrievalChunk.id).where(RetrievalChunk.project_id == pid)) is None
    db.add(StoryMemory(project_id=pid, chapter_no=1, kind="manual", content="禁用线索", muted=True))
    await db.flush()
    before = await effects.snapshot(db, pid)
    await db.execute(delete(StoryMemory).where(StoryMemory.project_id == pid))
    delta = effects.diff(before, await effects.snapshot(db, pid))
    await effects.undo(db, pid, delta)
    assert await db.scalar(select(RetrievalChunk.enabled).where(RetrievalChunk.project_id == pid)) is False
    result = await retrieval_service.search(db, pid, "memory", "禁用线索", embedder=None, top_k=5, min_score=0)
    assert result.hits == []


async def test_memory_index_undo_refreshes_stale_derived_identity(project_db):
    db, project = project_db
    pid = project.id
    memory = StoryMemory(project_id=pid, chapter_no=1, kind="manual", origin="manual", content="玉佩的秘密")
    db.add(memory)
    await db.flush()
    await memory_service.index_memories(db, pid, [memory])
    indexed = await db.scalar(select(RetrievalChunk).where(RetrievalChunk.project_id == pid))
    before = await effects.snapshot(db, pid)
    memory.chapter_no = 2
    await retrieval_service.set_flags(db, "memory", [memory.id], chapter_no=2)
    delta = effects.diff(before, await effects.snapshot(db, pid))
    await db.commit()
    assert indexed.chapter_no == 1  # set_flags 的 Core UPDATE 没有同步此对象
    await effects.undo(db, pid, delta)
    assert await db.scalar(select(RetrievalChunk.chapter_no).where(RetrievalChunk.id == indexed.id)) == 1
