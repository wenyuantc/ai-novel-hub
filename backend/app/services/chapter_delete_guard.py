"""删除章节前验证所有 ORM 外键级联的作品归属。"""

from itertools import batched

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ConflictError
from app.models import (
    AnalysisChange,
    Base,
    Chapter,
    ChapterAnalysis,
    ChapterCharacter,
    ChapterPlan,
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
    SceneReviewAnalysisReceipt,
    SceneReviewProposal,
    SceneSection,
    SettingProgression,
    SettingTimeline,
    StoryMemory,
    Task,
)

# (被删除表, 引用表, 引用列, 被引用列, 删除动作)。新关系必须分析后才能加入。
_REVIEWED_FKS = frozenset({
    ("analysis_changes", "setting_progressions", "source_change_id", "id", "SET NULL"),
    ("chapter_analyses", "analysis_changes", "analysis_id", "id", "CASCADE"),
    ("chapter_analyses", "scene_review_analysis_receipts", "analysis_id", "id", "SET NULL"),
    ("chapter_analyses", "story_memories", "analysis_id", "id", "SET NULL"),
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
    ("chapters", "scenes", "chapter_id", "id", "CASCADE"),
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


async def ensure_chapter_delete_isolation(db: AsyncSession, project_id: int, chapter_ids: list[int]) -> None:
    """调用方须先持有项目锁；越作品、无法确认归属或未审核的新外键都失败关闭。"""
    if not chapter_ids:
        return
    if _current_fk_graph() != _REVIEWED_FKS:
        raise _invalid()
    with db.no_autoflush:
        for batch in batched(sorted(set(chapter_ids)), 500):
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
            checks = [
                select(ChapterPlan.id).outerjoin(Outline, Outline.id == ChapterPlan.outline_id)
                .where(ChapterPlan.chapter_id.in_(batch), or_(
                    ChapterPlan.project_id.is_distinct_from(project_id),
                    Outline.project_id.is_distinct_from(project_id),
                )),
                select(GenerationLog.id).where(GenerationLog.chapter_id.in_(batch),
                                               GenerationLog.project_id.is_distinct_from(project_id)),
                select(Task.id).where(Task.chapter_id.in_(batch), Task.project_id.is_distinct_from(project_id)),
                select(ConsistencyLedger.id).where(ConsistencyLedger.chapter_id.in_(batch),
                                                   ConsistencyLedger.project_id.is_distinct_from(project_id)),
                select(ConsistencyImpact.id).where(ConsistencyImpact.chapter_id.in_(batch),
                                                   ConsistencyImpact.project_id.is_distinct_from(project_id)),
                # 删章令本批次的章、景引用失效；不能借错配的外键改变另一作品的历史。
                select(RevisionBatchScene.id)
                .outerjoin(RevisionBatch, RevisionBatch.id == RevisionBatchScene.batch_id)
                .outerjoin(Scene, Scene.id == RevisionBatchScene.scene_id)
                .where(or_(RevisionBatchScene.chapter_id.in_(batch), RevisionBatchScene.scene_id.in_(scenes)),
                       or_(RevisionBatch.project_id.is_distinct_from(project_id), and_(
                           RevisionBatchScene.scene_id.is_not(None),
                           Scene.chapter_id.is_distinct_from(RevisionBatchScene.chapter_id),
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
