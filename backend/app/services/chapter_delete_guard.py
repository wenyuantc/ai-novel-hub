"""删除章节前验证外键归属及存续章节的结构历史。"""

from collections import defaultdict
from itertools import batched

from sqlalchemy import ColumnElement, and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ConflictError
from app.models import (
    AiUsageCall,
    AnalysisChange,
    Base,
    Chapter,
    ChapterAnalysis,
    ChapterCharacter,
    ChapterPlan,
    ChapterVersion,
    Character,
    ConsistencyImpact,
    ConsistencyLedger,
    GenerationLog,
    Outline,
    PlanningUnit,
    RetrievalChunk,
    RevisionBatch,
    RevisionBatchScene,
    Scene,
    SceneMarker,
    SceneMove,
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
from app.services.scene_move_history import move_touches_chapter

# (被删除表, 引用表, 引用列, 被引用列, 删除动作)。新关系必须分析后才能加入。
_REVIEWED_FKS = frozenset({
    ("analysis_changes", "setting_progressions", "source_change_id", "id", "SET NULL"),
    ("chapter_analyses", "analysis_changes", "analysis_id", "id", "CASCADE"),
    ("chapter_analyses", "scene_review_analysis_receipts", "analysis_id", "id", "SET NULL"),
    ("chapter_analyses", "story_memories", "analysis_id", "id", "SET NULL"),
    # 清除章节引用，保留账户与作品的计量记录。
    ("chapters", "ai_usage_calls", "chapter_id", "id", "SET NULL"),
    ("chapters", "chapter_analyses", "chapter_id", "id", "CASCADE"),
    ("chapters", "chapter_characters", "chapter_id", "id", "CASCADE"),
    ("chapters", "chapter_plans", "chapter_id", "id", "SET NULL"),
    ("chapters", "chapter_versions", "chapter_id", "id", "CASCADE"),
    # 事实台账随章节级联删除（没有下游外键）；问题证据只在 JSON 中记章节 id，不受删除影响。
    # 修改影响记录同样随章节级联删除（没有下游外键）；影响项只在 JSON 中记后续章节 id。
    ("chapters", "consistency_impacts", "chapter_id", "id", "CASCADE"),
    ("chapters", "consistency_ledgers", "chapter_id", "id", "CASCADE"),
    ("chapters", "generation_logs", "chapter_id", "id", "SET NULL"),
    ("chapters", "planning_units", "chapter_id", "id", "SET NULL"),
    ("chapters", "revision_batch_scenes", "chapter_id", "id", "SET NULL"),
    ("chapters", "scene_review_analysis_receipts", "chapter_id", "id", "CASCADE"),
    ("chapters", "scene_review_proposals", "chapter_id", "id", "CASCADE"),
    ("chapters", "scene_moves", "from_chapter_id", "id", "SET NULL"),
    ("chapters", "scene_moves", "to_chapter_id", "id", "SET NULL"),
    ("chapters", "scenes", "chapter_id", "id", "CASCADE"),
    ("chapters", "serialization_entries", "chapter_id", "id", "SET NULL"),
    ("chapters", "setting_progressions", "chapter_id", "id", "SET NULL"),
    ("chapters", "story_memories", "chapter_id", "id", "SET NULL"),
    ("chapters", "tasks", "chapter_id", "id", "SET NULL"),
    ("scene_markers", "scene_markers", "origin_marker_id", "id", "SET NULL"),
    ("scene_review_proposals", "scene_review_payload_chunks", "proposal_id", "id", "CASCADE"),
    ("scene_sections", "scene_markers", "scene_id", "scene_id", "CASCADE"),
    ("scene_sections", "scene_markers", "section_id", "id", "CASCADE"),
    ("scene_sections", "scene_sections", "origin_section_id", "id", "SET NULL"),
    ("scenes", "planning_units", "scene_id", "id", "SET NULL"),
    ("scenes", "revision_batch_scenes", "scene_id", "id", "SET NULL"),
    ("scenes", "scene_documents", "scene_id", "id", "CASCADE"),
    ("scenes", "scene_markers", "scene_id", "id", "CASCADE"),
    ("scenes", "scene_moves", "scene_id", "id", "CASCADE"),
    ("scenes", "scene_sections", "scene_id", "id", "CASCADE"),
    ("scenes", "setting_progressions", "scene_id", "id", "SET NULL"),
})


def _current_fk_graph() -> frozenset[tuple[str, str, str, str, str]]:
    """沿 CASCADE 反查全部 ORM 外键，防止新增子表绕过归属检查。"""
    pending, visited, edges = {"chapters"}, set(), set()
    while pending:
        parent = pending.pop()
        if parent in visited:
            continue
        visited.add(parent)
        for table in Base.metadata.tables.values():
            for fk in table.foreign_keys:
                if fk.column.table.name == parent:
                    action = (fk.ondelete or "").upper()
                    edges.add((parent, table.name, fk.parent.name, fk.column.name, action))
                    if action == "CASCADE":
                        pending.add(table.name)
    return frozenset(edges)


def _invalid() -> ConflictError:
    return ConflictError("章节存在无法安全处理的关联，请联系作者检查", extra={"code": "chapter_reference_invalid"})


def _foreign_moves(project_id: int, chapter_ids: tuple[int, ...], scenes):
    source, target, holder = aliased(Chapter), aliased(Chapter), aliased(Chapter)
    return (
        select(SceneMove.id)
        .outerjoin(Scene, Scene.id == SceneMove.scene_id)
        .outerjoin(holder, holder.id == Scene.chapter_id)
        .outerjoin(source, source.id == SceneMove.from_chapter_id)
        .outerjoin(target, target.id == SceneMove.to_chapter_id)
        .outerjoin(PlanningUnit, PlanningUnit.id == SceneMove.planning_unit_id)
        .where(
            or_(SceneMove.scene_id.in_(scenes), SceneMove.from_chapter_id.in_(chapter_ids),
                SceneMove.to_chapter_id.in_(chapter_ids)),
            or_(holder.project_id.is_distinct_from(project_id),
                and_(SceneMove.from_chapter_id.is_not(None), source.project_id.is_distinct_from(project_id)),
                and_(SceneMove.to_chapter_id.is_not(None), target.project_id.is_distinct_from(project_id)),
                and_(SceneMove.planning_unit_id.is_not(None),
                     or_(PlanningUnit.project_id.is_distinct_from(project_id),
                         PlanningUnit.source_level.is_distinct_from("scene")))),
        )
    )


async def _check_surviving_history(db: AsyncSession, project_id: int, removed: set[int]) -> None:
    lost_scenes: dict[int, set[int]] = defaultdict(set)
    lost_units: dict[int, set[int]] = defaultdict(set)
    for batch in batched(sorted(removed), 500):
        rows = await db.execute(
            select(SceneMove.scene_id, SceneMove.from_chapter_id, SceneMove.to_chapter_id, SceneMove.planning_unit_id)
            .join(Scene, Scene.id == SceneMove.scene_id).where(Scene.chapter_id.in_(batch))
        )
        for sid, source, target, unit_id in rows:
            for cid in (source, target):
                if cid is None or cid in removed:
                    continue
                lost_scenes[cid].add(sid)
                if unit_id is not None:
                    lost_units[cid].add(unit_id)

    # 同章重挂或另一条存续移动证据可继续证明规划归属，只有实际丢失的证据需要保护。
    unit_ids = set().union(*lost_units.values())
    for batch in batched(sorted(unit_ids), 500):
        current = await db.execute(
            select(PlanningUnit.id, Scene.chapter_id).join(Scene, Scene.id == PlanningUnit.scene_id)
            .where(PlanningUnit.id.in_(batch), PlanningUnit.project_id == project_id)
        )
        for unit_id, cid in current:
            if cid in lost_units:
                lost_units[cid].discard(unit_id)
        retained = await db.execute(
            select(SceneMove.planning_unit_id, SceneMove.from_chapter_id, SceneMove.to_chapter_id, Scene.chapter_id)
            .join(Scene, Scene.id == SceneMove.scene_id).join(Chapter, Chapter.id == Scene.chapter_id)
            .where(SceneMove.planning_unit_id.in_(batch), Chapter.project_id == project_id)
        )
        for unit_id, source, target, holder in retained:
            if holder not in removed:
                for cid in (source, target):
                    if cid in lost_units:
                        lost_units[cid].discard(unit_id)

    for batch in batched(sorted(lost_scenes), 500):
        rows = await db.stream(
            select(ChapterVersion.chapter_id, ChapterVersion.scene_snapshot)
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
            .where(Chapter.project_id == project_id, ChapterVersion.chapter_id.in_(batch),
                   ChapterVersion.scene_snapshot.is_not(None)).execution_options(yield_per=20)
        )
        try:
            async for cid, snapshot in rows:
                items = snapshot.get("scenes") if isinstance(snapshot, dict) else None
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    scene_id = item.get("id")
                    plan = item.get("planning")
                    unit_id = plan.get("unit_id") if isinstance(plan, dict) else None
                    if ((type(scene_id) is int and scene_id in lost_scenes[cid])
                            or (type(unit_id) is int and unit_id in lost_units.get(cid, ()))):
                        raise ConflictError("其他章节的历史仍引用待删除场景，请先移出关联场景", extra={
                            "code": "scene_move_history_in_use",
                        })
        finally:
            await rows.close()


async def ensure_chapter_delete_isolation(db: AsyncSession, project_id: int, chapter_ids: list[int]) -> None:
    """调用方须先持有项目锁；越作品、无法确认归属或未审核的新外键都失败关闭。"""
    if not chapter_ids:
        return
    if _current_fk_graph() != _REVIEWED_FKS:
        raise _invalid()
    removed = set(chapter_ids)
    with db.no_autoflush:
        for batch in batched(sorted(removed), 500):
            owned = set(await db.scalars(select(Chapter.id).where(
                Chapter.id.in_(batch), Chapter.project_id == project_id,
            )))
            if owned != set(batch):
                raise _invalid()
            scenes = select(Scene.id).where(Scene.chapter_id.in_(batch))
            analyses = select(ChapterAnalysis.id).where(ChapterAnalysis.chapter_id.in_(batch))
            changes = select(AnalysisChange.id).where(AnalysisChange.analysis_id.in_(analyses))
            memories = select(StoryMemory.id).where(StoryMemory.chapter_id.in_(batch), StoryMemory.origin == "auto")
            source_marker = aliased(SceneMarker)
            source_section = aliased(SceneSection)
            markers = select(source_marker.id).where(source_marker.scene_id.in_(scenes))
            sections = select(source_section.id).where(source_section.scene_id.in_(scenes))
            revision_history = select(SceneMove.id).where(
                SceneMove.scene_id == Scene.id, move_touches_chapter(RevisionBatchScene.chapter_id),
            ).exists()
            checks = [
                _foreign_moves(project_id, batch, scenes),
                select(ChapterPlan.id).outerjoin(Outline, Outline.id == ChapterPlan.outline_id)
                .where(ChapterPlan.chapter_id.in_(batch), or_(
                    ChapterPlan.project_id.is_distinct_from(project_id),
                    Outline.project_id.is_distinct_from(project_id),
                )),
                select(GenerationLog.id).where(GenerationLog.chapter_id.in_(batch),
                                               GenerationLog.project_id.is_distinct_from(project_id)),
                select(AiUsageCall.id).where(AiUsageCall.chapter_id.in_(batch),
                                             AiUsageCall.project_id.is_distinct_from(project_id)),
                select(SerializationEntry.id).where(SerializationEntry.chapter_id.in_(batch),
                                                    SerializationEntry.project_id.is_distinct_from(project_id)),
                select(SerializationPublication.id).join(SerializationEdition).join(SerializationEntry)
                .where(SerializationEntry.chapter_id.in_(batch),
                       SerializationPublication.project_id.is_distinct_from(project_id)),
                select(Task.id).where(Task.chapter_id.in_(batch), Task.project_id.is_distinct_from(project_id)),
                select(ConsistencyLedger.id).where(ConsistencyLedger.chapter_id.in_(batch),
                                                   ConsistencyLedger.project_id.is_distinct_from(project_id)),
                select(ConsistencyImpact.id).where(ConsistencyImpact.chapter_id.in_(batch),
                                                   ConsistencyImpact.project_id.is_distinct_from(project_id)),
                # 删章令本批次的章、景引用失效；不能借错配的外键改变另一作品的历史。
                select(RevisionBatchScene.id)
                .outerjoin(RevisionBatch, RevisionBatch.id == RevisionBatchScene.batch_id)
                .outerjoin(Scene, Scene.id == RevisionBatchScene.scene_id)
                .outerjoin(Chapter, Chapter.id == Scene.chapter_id)
                .where(or_(RevisionBatchScene.chapter_id.in_(batch), RevisionBatchScene.scene_id.in_(scenes)),
                       or_(RevisionBatch.project_id.is_distinct_from(project_id), and_(
                           RevisionBatchScene.scene_id.is_not(None),
                           or_(Chapter.project_id.is_distinct_from(project_id), and_(
                               Scene.chapter_id.is_distinct_from(RevisionBatchScene.chapter_id), ~revision_history,
                           )),
                       ))),
                select(ChapterAnalysis.id).where(ChapterAnalysis.chapter_id.in_(batch),
                                                 ChapterAnalysis.project_id.is_distinct_from(project_id)),
                select(AnalysisChange.id).where(AnalysisChange.analysis_id.in_(analyses),
                                                AnalysisChange.project_id.is_distinct_from(project_id)),
                select(StoryMemory.id).where(
                    or_(StoryMemory.chapter_id.in_(batch), StoryMemory.analysis_id.in_(analyses)),
                    StoryMemory.project_id.is_distinct_from(project_id),
                ),
                # 自动记忆删除通过多态 ref_id 手动清索引，没有数据库 FK 也须限定归属。
                select(RetrievalChunk.id).where(RetrievalChunk.source == "memory", RetrievalChunk.ref_id.in_(memories),
                                                RetrievalChunk.project_id.is_distinct_from(project_id)),
                select(PlanningUnit.id).outerjoin(ChapterPlan, ChapterPlan.id == PlanningUnit.plan_id)
                .outerjoin(Outline, Outline.id == ChapterPlan.outline_id).where(
                    or_(PlanningUnit.chapter_id.in_(batch), PlanningUnit.scene_id.in_(scenes)),
                    or_(PlanningUnit.project_id.is_distinct_from(project_id), and_(
                        PlanningUnit.plan_id.is_not(None), or_(
                            ChapterPlan.project_id.is_distinct_from(project_id),
                            Outline.project_id.is_distinct_from(project_id),
                        ),
                    )),
                ),
                select(ChapterCharacter.chapter_id).outerjoin(Character, Character.id == ChapterCharacter.character_id)
                .where(ChapterCharacter.chapter_id.in_(batch), Character.project_id.is_distinct_from(project_id)),
                select(SettingProgression.id).outerjoin(
                    SettingTimeline, SettingTimeline.id == SettingProgression.timeline_id,
                )
                .where(or_(SettingProgression.chapter_id.in_(batch), SettingProgression.scene_id.in_(scenes),
                           SettingProgression.source_change_id.in_(changes)),
                       SettingTimeline.project_id.is_distinct_from(project_id)),
                select(SceneReviewProposal.id).outerjoin(Task, Task.id == SceneReviewProposal.task_id)
                .where(SceneReviewProposal.chapter_id.in_(batch), Task.project_id.is_distinct_from(project_id)),
                select(SceneMarker.id).outerjoin(Scene, Scene.id == SceneMarker.scene_id)
                .outerjoin(Chapter, Chapter.id == Scene.chapter_id)
                .where(SceneMarker.origin_marker_id.in_(markers), Chapter.project_id.is_distinct_from(project_id)),
                select(SceneSection.id).outerjoin(Scene, Scene.id == SceneSection.scene_id)
                .outerjoin(Chapter, Chapter.id == Scene.chapter_id)
                .where(SceneSection.origin_section_id.in_(sections), Chapter.project_id.is_distinct_from(project_id)),
            ]
            for statement in checks:
                if await db.scalar(statement.limit(1)) is not None:
                    raise _invalid()
            # 此收据的 task_key 不是外键；任务、章节或关联分析归属不明时也不清理。
            receipts = await db.execute(
                select(SceneReviewAnalysisReceipt.analysis_id, Task.project_id.label("task_project"),
                       Chapter.project_id.label("chapter_project"),
                       ChapterAnalysis.project_id.label("analysis_project"))
                .outerjoin(Task, Task.statistics_key == SceneReviewAnalysisReceipt.task_key)
                .outerjoin(Chapter, Chapter.id == SceneReviewAnalysisReceipt.chapter_id)
                .outerjoin(ChapterAnalysis, ChapterAnalysis.id == SceneReviewAnalysisReceipt.analysis_id)
                .where(or_(SceneReviewAnalysisReceipt.chapter_id.in_(batch),
                           SceneReviewAnalysisReceipt.analysis_id.in_(analyses)))
            )
            for row in receipts:
                if (row.chapter_project != project_id or row.task_project != project_id
                        or (row.analysis_id is not None and row.analysis_project != project_id)):
                    raise _invalid()
        await _check_surviving_history(db, project_id, removed)


async def detach_scene_move_chapters(db: AsyncSession, project_id: int, chapter_ids: list[int]) -> None:
    """调用方已验证删章。断开批次历史定位，保留存续章节的最后原生身份。"""
    removed = set(chapter_ids)
    origins = set()
    for batch in batched(sorted(removed), 500):
        origins.update(await db.scalars(select(SceneMove.from_chapter_id).join(Scene, Scene.id == SceneMove.scene_id)
            .where(SceneMove.first_move, SceneMove.from_chapter_id.is_not(None), Scene.chapter_id.in_(batch))))
        await db.execute(update(RevisionBatchScene).where(
            RevisionBatchScene.chapter_id.in_(batch), RevisionBatchScene.scene_id.is_not(None),
        ).values(scene_id=None).execution_options(synchronize_session=False))

    for batch in batched(sorted(origins - removed), 500):
        known = set(await db.scalars(select(Scene.chapter_id).join(Chapter, Chapter.id == Scene.chapter_id).where(
            Scene.chapter_id.in_(batch), Chapter.project_id == project_id,
            ~select(SceneMove.id).where(SceneMove.scene_id == Scene.id).exists(),
        ).distinct()))
        moved = await db.execute(select(SceneMove.from_chapter_id, Scene.chapter_id)
            .join(Scene, Scene.id == SceneMove.scene_id).join(Chapter, Chapter.id == Scene.chapter_id)
            .where(SceneMove.first_move, SceneMove.from_chapter_id.in_(batch), Chapter.project_id == project_id))
        known.update(origin for origin, holder in moved if holder not in removed)
        # 一章仍有活动场景但原生身份全随待删章清除时，补身份锚点，保持投影不变。
        db.add_all([Scene(chapter_id=cid, active=False) for cid in batch if cid not in known])
    await db.flush()


def has_serialization_work() -> ColumnElement[bool]:
    """智能体无法撤销的人工排程、校对状态或定稿。默认空条目没有作者内容。"""
    editions = select(SerializationEdition.id).where(SerializationEdition.entry_id == SerializationEntry.id)
    return or_(SerializationEntry.stage != "revision", SerializationEntry.planned_date.is_not(None), editions.exists())


async def check_agent_serialization_removal(db: AsyncSession, project_id: int, chapter_ids: list[int]) -> None:
    for batch in batched(chapter_ids, 500):
        work = select(SerializationEntry.id).where(
            SerializationEntry.project_id == project_id, SerializationEntry.chapter_id.in_(batch),
            has_serialization_work(),
        )
        if await db.scalar(work.limit(1)) is not None:
            raise ConflictError("待移除的章节已有连载排期、编辑状态或定稿，请在工作台核对后手动处理")


async def detach_serialization_chapters(db: AsyncSession, project_id: int, chapter_ids: list[int]) -> None:
    """删章事务中清理未发布定稿和排期。已发布稿继续通过永久条目关联其原始发布记录。"""
    for batch in batched(chapter_ids, 500):
        entries = select(SerializationEntry.id).where(
            SerializationEntry.project_id == project_id, SerializationEntry.chapter_id.in_(batch),
        )
        has_publication = select(SerializationPublication.id).where(
            SerializationPublication.edition_id == SerializationEdition.id,
        ).exists()
        await db.execute(delete(SerializationEdition).where(
            SerializationEdition.entry_id.in_(entries), ~has_publication,
        ).execution_options(synchronize_session=False))
        has_edition = select(SerializationEdition.id).where(
            SerializationEdition.entry_id == SerializationEntry.id,
        ).exists()
        await db.execute(delete(SerializationEntry).where(
            SerializationEntry.project_id == project_id, SerializationEntry.chapter_id.in_(batch), ~has_edition,
        ).execution_options(synchronize_session=False))
        await db.execute(
            update(SerializationEntry)
            .where(SerializationEntry.project_id == project_id, SerializationEntry.chapter_id.in_(batch))
            .values(planned_date=None, chapter_id=None, revision=SerializationEntry.revision + 1)
            .execution_options(synchronize_session=False)
        )
