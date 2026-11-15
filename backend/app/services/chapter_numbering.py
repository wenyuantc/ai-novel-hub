"""章节号平移（outline-pro R3）：在某一章之前插入 / 删除章节时，全项目引用章节号的字段整体平移。

章节号是多处数据判断先后的依据（「更晚章节优先」、伏笔超期 / 即将到期），插入章节后若只改 chapters.chapter_no，
这些判断就会错位。因此**任何新增的、存章节号的字段都必须登记到 CHAPTER_NO_REFS**；
`tests/test_chapter_numbering.py::test_all_chapter_no_columns_registered` 用反射扫描所有模型中名为 `chapter_no`
或以 `_chapter` 结尾的 Integer 列，未登记即失败。

文本里出现的章节号（如回写条目 reason「已有第 N 章的更新」、历史 note「第 N 章分析」）属于历史描述，不改写。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnalysisChange,
    CareerHistory,
    Chapter,
    ChapterAnalysis,
    Character,
    Foreshadow,
    ForeshadowHistory,
    Outline,
    Relationship,
    RelationshipHistory,
    RetrievalChunk,
    SettingProgression,
    SettingTimeline,
    StoryMemory,
)
from app.services import retrieval_service


@dataclass(frozen=True)
class ChapterNoRef:
    """一个存章节号的字段：model.column，scope(project_id) 给出限定到本项目的条件。"""

    model: Any
    column: str
    scope: Callable[[int], ColumnElement[bool]]

    @property
    def key(self) -> tuple[str, str]:
        return (self.model.__tablename__, self.column)


def _direct(model: Any) -> Callable[[int], ColumnElement[bool]]:
    return lambda pid: model.project_id == pid


def _via_relationship(pid: int) -> ColumnElement[bool]:
    return RelationshipHistory.relationship_id.in_(select(Relationship.id).where(Relationship.project_id == pid))


def _via_character(pid: int) -> ColumnElement[bool]:
    return CareerHistory.character_id.in_(select(Character.id).where(Character.project_id == pid))


def _via_foreshadow(pid: int) -> ColumnElement[bool]:
    return ForeshadowHistory.foreshadow_id.in_(select(Foreshadow.id).where(Foreshadow.project_id == pid))


def _via_setting_timeline(pid: int) -> ColumnElement[bool]:
    return (SettingProgression.anchor_kind == "planned") & SettingProgression.timeline_id.in_(
        select(SettingTimeline.id).where(SettingTimeline.project_id == pid)
    )


# 新增引用章节号的字段必须登记在这里（见模块说明）
CHAPTER_NO_REFS: tuple[ChapterNoRef, ...] = (
    ChapterNoRef(Chapter, "chapter_no", _direct(Chapter)),
    ChapterNoRef(Outline, "chapter_no", _direct(Outline)),
    ChapterNoRef(ChapterAnalysis, "chapter_no", _direct(ChapterAnalysis)),
    ChapterNoRef(AnalysisChange, "chapter_no", _direct(AnalysisChange)),
    ChapterNoRef(RelationshipHistory, "chapter_no", _via_relationship),
    ChapterNoRef(CareerHistory, "chapter_no", _via_character),
    ChapterNoRef(ForeshadowHistory, "chapter_no", _via_foreshadow),
    ChapterNoRef(Foreshadow, "planned_plant_chapter", _direct(Foreshadow)),
    ChapterNoRef(Foreshadow, "actual_plant_chapter", _direct(Foreshadow)),
    ChapterNoRef(Foreshadow, "planned_resolve_chapter", _direct(Foreshadow)),
    ChapterNoRef(Foreshadow, "actual_resolve_chapter", _direct(Foreshadow)),
    # 剧情记忆与其检索单元（knowledge-base）：召回「本章之前」依赖章节号
    ChapterNoRef(StoryMemory, "chapter_no", _direct(StoryMemory)),
    ChapterNoRef(RetrievalChunk, "chapter_no", _direct(RetrievalChunk)),
    # 已建章锚点跟随 chapter_id；只有计划章号参与整体平移，删章后的孤儿不得按数字重绑。
    ChapterNoRef(SettingProgression, "chapter_no", _via_setting_timeline),
)


async def preflight_chapter_shift(db: AsyncSession, project_id: int, from_no: int) -> None:
    """在任何平移前检查所有受影响的章节、记忆与多态检索来源。"""
    shifted_chapters = select(Chapter.id).where(Chapter.project_id == project_id, Chapter.chapter_no >= from_no)
    with db.no_autoflush:
        chapter_ids = list(await db.scalars(shifted_chapters))
        for chapter_id in chapter_ids:
            retrieval_service._require_clean_source_identity(
                db, Chapter, chapter_id, ("id", "project_id", "chapter_no"),
            )
        foreign_memory = await db.scalar(select(StoryMemory.id).where(
            StoryMemory.chapter_id.in_(shifted_chapters), StoryMemory.project_id.is_distinct_from(project_id),
        ).limit(1))
        if foreign_memory is not None:
            raise retrieval_service._invalid_retrieval_reference()
        memory_ids = list(await db.scalars(select(StoryMemory.id).where(
            StoryMemory.project_id == project_id,
            or_(StoryMemory.chapter_no >= from_no, StoryMemory.chapter_id.in_(shifted_chapters)),
        )))
        for memory_id in memory_ids:
            retrieval_service._require_clean_source_identity(
                db, StoryMemory, memory_id, ("id", "project_id", "chapter_id", "chapter_no", "origin"),
            )
        indexed = (await db.execute(select(RetrievalChunk.id, RetrievalChunk.source, RetrievalChunk.ref_id).where(
            RetrievalChunk.project_id == project_id, RetrievalChunk.chapter_no.is_not(None),
            RetrievalChunk.chapter_no >= from_no,
        ))).all()
        refs: dict[str, list[int]] = {"memory": memory_ids, "kb": []}
        for row in indexed:
            if row.source not in refs:
                raise retrieval_service._invalid_retrieval_reference()
            retrieval_service._require_clean_source_identity(
                db, RetrievalChunk, row.id, ("id", "project_id", "source", "ref_id", "chapter_no", "version"),
            )
            refs[row.source].append(row.ref_id)
        await retrieval_service.validate_project_source_refs(db, project_id, "memory", refs["memory"])
        await retrieval_service.validate_project_source_refs(db, project_id, "kb", refs["kb"])


async def shift_chapter_numbers(db: AsyncSession, project_id: int, *, from_no: int, delta: int) -> None:
    """把项目内所有 >= from_no 的章节号加上 delta（可为负）。不 commit。

    chapters.chapter_no 没有唯一约束，各表一条 UPDATE，顺序不影响结果。已加载到会话中的对象同步更新（fetch）。
    调用方持项目写锁并在整批插删和平移完成、flush 后调用
    setting_progression_service.on_story_positions_changed；这里不在批处理的中间态触发重算。
    """
    if delta == 0:
        return
    await preflight_chapter_shift(db, project_id, from_no)
    for ref in CHAPTER_NO_REFS:
        col = getattr(ref.model, ref.column)
        await db.execute(
            update(ref.model)
            .where(ref.scope(project_id), col.is_not(None), col >= from_no)
            .values({ref.column: col + delta})
            .execution_options(synchronize_session="fetch")
        )
