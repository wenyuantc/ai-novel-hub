"""修改影响追踪的读取与确认：第一级（派生内容过期，零模型调用）、评估结果的读取派生字段、确认状态的 CAS。

第一级完全在读取时按指纹判定，不依赖任何「正文已变更」事件：
- 章摘要：该章最新分析的 content_hash 与当前分析指纹不同（与 document_export/story.py 同一判定）；
- 剧情记忆：复用 memory_service.memory_source_stale（记忆页 source_stale 的同一处实现）；
- 设定变化：由该章分析写入、仍生效且未撤销的 SettingProgression，其 source_content_hash 与当前指纹不同；
  空哈希的旧记录不判为过期。作者可把某条设定变化标为「已确认无需更新」（acknowledged），不再计入。

权限统一走 project_access.require_project：读取 content.read，确认 content.edit（查看者 403、非协作者 404）。
本模块只写 consistency_impacts；不改正文、摘要、记忆、设定与 consistency_issues。
"""

from dataclasses import dataclass, field
from itertools import batched
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import (
    AnalysisChange,
    Career,
    Chapter,
    ChapterAnalysis,
    Character,
    ConsistencyImpact,
    ConsistencyLedger,
    Organization,
    SettingProgression,
    SettingTimeline,
    StoryMemory,
    Task,
    WorldSetting,
)
from app.models.entities import ACTIVE_TASK_STATUSES
from app.schemas.consistency import (
    AssessmentOut,
    ImpactChange,
    ImpactChapterRef,
    ImpactChapterSummary,
    ImpactDetail,
    ImpactItemOut,
    ImpactOverview,
    ImpactRunning,
    ImpactUpdate,
    StaleMemory,
    StaleProgression,
    StaleSummary,
)
from app.services import memory_service
from app.services.analysis_service import latest_analyses
from app.services.consistency import check, ledger
from app.services.mentions.matcher import text_hash
from app.services.project_access import require_project
from app.services.rich_document_ai import analysis_fingerprints

CHAPTER_NOT_FOUND = "章节不存在"
NO_BASELINE = "本章还没有改动前的事实台账，请先运行一次一致性检查"
UNCHANGED = "本章自上次抽取事实后没有改动，暂无可评估的变化"
EMPTY_CHAPTER = "章节正文为空，无法评估影响"
ITEM_NOT_FOUND = "受影响章节不在本次评估结果中，请刷新后重试"
BAD_ACKNOWLEDGED = "要确认的设定变化不属于本章的分析结果"
_MEMORY_BATCH = 500


def _stale() -> ConflictError:
    return ConflictError(
        "修改影响记录已被更新，请刷新后重试；当前的选择可继续保留", extra={"code": "stale_revision"},
    )


# ---------------- 第一级：派生内容过期 ----------------


@dataclass
class Derived:
    """一章已过期的派生内容。"""

    analyzed: bool = False
    # 最新分析（仅在其来源指纹与当前不同时给出）
    summary: ChapterAnalysis | None = None
    memories: list[StoryMemory] = field(default_factory=list)
    progressions: list[tuple[SettingProgression, SettingTimeline]] = field(default_factory=list)


async def derived_stale(
    db: AsyncSession, project_id: int, chapters: list[Chapter], fingerprints: dict[int, str],
) -> dict[int, Derived]:
    """批量判定各章派生内容是否过期（不逐章查库）。fingerprints = 各章当前分析指纹。"""
    result = {chapter.id: Derived() for chapter in chapters}
    states = {chapter.id: (chapter.project_id, chapter.chapter_no, fingerprints[chapter.id]) for chapter in chapters}
    for chunk in batched(sorted(result), 200):
        for chapter_id, analysis in (await latest_analyses(db, list(chunk))).items():
            result[chapter_id].analyzed = True
            if analysis.content_hash != fingerprints[chapter_id]:
                result[chapter_id].summary = analysis
        memories = list(await db.scalars(
            select(StoryMemory)
            .where(StoryMemory.project_id == project_id, StoryMemory.origin == "auto",
                   StoryMemory.chapter_id.in_(chunk), StoryMemory.analysis_id.is_not(None))
            .order_by(StoryMemory.id)
        ))
        for rows in batched(memories, _MEMORY_BATCH):
            # 与记忆页的 source_stale 同一处判定；章节指纹已算好，不再重复读取章节
            stale = await memory_service.memory_source_stale(db, list(rows), chapter_states=states)
            for row in rows:
                if stale[row.id]:
                    result[row.chapter_id].memories.append(row)
        rows = await db.execute(
            select(SettingProgression, SettingTimeline, ChapterAnalysis.chapter_id)
            .join(SettingTimeline, SettingTimeline.id == SettingProgression.timeline_id)
            .join(AnalysisChange, AnalysisChange.id == SettingProgression.source_change_id)
            .join(ChapterAnalysis, ChapterAnalysis.id == AnalysisChange.analysis_id)
            .where(
                SettingTimeline.project_id == project_id, AnalysisChange.project_id == project_id,
                ChapterAnalysis.project_id == project_id, ChapterAnalysis.chapter_id.in_(chunk),
                SettingProgression.source == "analysis", SettingProgression.active.is_(True),
                SettingProgression.reverted_at.is_(None),
                # 空哈希的旧记录无从比较，不判为过期
                SettingProgression.source_content_hash.is_not(None),
            )
            .order_by(SettingProgression.id)
        )
        for progression, timeline, chapter_id in rows:
            if progression.source_content_hash != fingerprints[chapter_id]:
                result[chapter_id].progressions.append((progression, timeline))
    return result


async def _fingerprints(db: AsyncSession, chapters: list[Chapter]) -> dict[int, str]:
    return await analysis_fingerprints(db, chapters) if chapters else {}


def _ledger_state(row: ConsistencyLedger | None, fingerprint: str) -> str:
    return "missing" if row is None else "fresh" if ledger.is_fresh(row, fingerprint) else "stale"


def assess_block(chapter: Chapter, row: ConsistencyLedger | None, fingerprint: str) -> str | None:
    """不能评估的原因；None = 可以评估。

    改动前台账的取法：台账过期 → 现有 facts 即改动前；台账新鲜且有上一版 → prev_facts 即改动前；两者都没有则无从比较。
    """
    if not chapter.content.strip():
        return "empty"
    if row is None:
        return "no_baseline"
    if ledger.is_fresh(row, fingerprint) and row.prev_facts is None:
        return "unchanged"
    return None


def block_message(block: str) -> str:
    return {"empty": EMPTY_CHAPTER, "no_baseline": NO_BASELINE, "unchanged": UNCHANGED}[block]


async def open_impact(db: AsyncSession, project_id: int) -> tuple[int, str | None, int, int | None] | None:
    """本作品进行中的影响评估任务：(id, task_key, user_id, chapter_id)。"""
    row = (await db.execute(
        select(Task.id, Task.statistics_key, Task.user_id, Task.input)
        .where(Task.project_id == project_id, Task.kind == check.IMPACT_KIND, Task.status.in_(ACTIVE_TASK_STATUSES))
        .order_by(Task.id)
        .limit(1)
    )).one_or_none()
    if row is None:
        return None
    chapter_id = row.input.get("chapter_id") if isinstance(row.input, dict) else None
    return row.id, row.statistics_key, row.user_id, chapter_id if type(chapter_id) is int else None


def _running(running: tuple[int, str | None, int, int | None] | None, user_id: int) -> dict[str, Any]:
    if running is None:
        return {}
    return {
        "running_task_id": running[0],
        # 订阅进度需要 task_key；只交给任务的发起者本人
        "running_task_key": running[1] if running[2] == user_id else None,
        "running_chapter_id": running[3],
    }


def _items(row: ConsistencyImpact) -> list[dict[str, Any]]:
    return [item for item in row.items or [] if isinstance(item, dict)]


def _acknowledged(row: ConsistencyImpact | None) -> set[int]:
    return {value for value in (row.acknowledged or []) if type(value) is int} if row is not None else set()


def _item_changed(item: dict[str, Any], hashes: dict[int, str]) -> bool:
    """被列章节已删除，或其当前正文指纹与任一证据记录时不同。"""
    current = hashes.get(item.get("chapter_id"))
    evidence = [value for value in item.get("evidence") or [] if isinstance(value, dict)]
    return current is None or any(value.get("text_hash") != current for value in evidence)


async def overview(db: AsyncSession, user_id: int, project_id: int) -> ImpactOverview:
    """作品级：有过期派生内容或有评估记录的章节，各类计数、台账状态与评估概要。"""
    await require_project(db, user_id, project_id, "content.read")
    chapters = list(await db.scalars(
        select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_no, Chapter.id)
    ))
    fingerprints = await _fingerprints(db, chapters)
    derived = await derived_stale(db, project_id, chapters, fingerprints)
    impacts = {row.chapter_id: row for row in await db.scalars(
        select(ConsistencyImpact).where(ConsistencyImpact.project_id == project_id)
    )}
    ledgers = await ledger.ledgers_by_chapter(db, project_id)
    hashes = {chapter.id: text_hash(chapter.content) for chapter in chapters}
    out = []
    for chapter in chapters:
        info, row = derived[chapter.id], impacts.get(chapter.id)
        acknowledged = _acknowledged(row)
        progressions = sum(1 for progression, _ in info.progressions if progression.id not in acknowledged)
        stale_count = int(info.summary is not None) + len(info.memories) + progressions
        assessed = row is not None and row.assessed_at is not None
        if not stale_count and not assessed:
            continue
        items = _items(row) if assessed else []
        out.append(ImpactChapterSummary(
            chapter_id=chapter.id,
            chapter_no=chapter.chapter_no,
            chapter_title=chapter.title,
            stale_summary=info.summary is not None,
            stale_memories=len(info.memories),
            stale_progressions=progressions,
            stale_count=stale_count,
            ledger_state=_ledger_state(ledgers.get(chapter.id), fingerprints[chapter.id]),
            assessed=assessed,
            assessed_at=row.assessed_at if assessed else None,
            task_id=row.task_id if assessed else None,
            outdated=assessed and row.source_hash != fingerprints[chapter.id],
            no_change=assessed and not row.changes,
            changes=len(row.changes or []) if assessed else 0,
            items=len(items),
            pending_items=sum(1 for item in items if item.get("status") == "pending"),
            changed_items=sum(1 for item in items if _item_changed(item, hashes)),
            uncovered=len(row.uncovered or []) if assessed else 0,
        ))
    return ImpactOverview(chapters=out, **_running(await open_impact(db, project_id), user_id))


async def running(db: AsyncSession, user_id: int, project_id: int) -> ImpactRunning:
    """只报告进行中的评估任务（一次按作品的任务查询）。

    评估进行中前端按秒级轮询它判断任务是否结束，结束后再读一次概览；概览要读全书正文算指纹，不适合轮询。
    """
    await require_project(db, user_id, project_id, "content.read")
    return ImpactRunning(**_running(await open_impact(db, project_id), user_id))


async def _chapter(db: AsyncSession, project_id: int, chapter_id: int) -> Chapter:
    chapter = await db.scalar(
        select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    if chapter is None:
        raise NotFoundError(CHAPTER_NOT_FOUND)
    return chapter


async def _names(db: AsyncSession, model: Any, column: Any, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    return dict((await db.execute(select(model.id, column).where(model.id.in_(ids)))).all())


async def _progression_outs(
    db: AsyncSession, rows: list[tuple[SettingProgression, SettingTimeline]], acknowledged: set[int],
) -> list[StaleProgression]:
    timelines = [timeline for _, timeline in rows]
    characters = await _names(db, Character, Character.name, {
        value for timeline in timelines for value in (timeline.character_id, timeline.related_character_id) if value
    })
    worlds = await _names(db, WorldSetting, WorldSetting.title,
                          {t.world_setting_id for t in timelines if t.world_setting_id})
    careers = await _names(db, Career, Career.name, {t.career_id for t in timelines if t.career_id})
    organizations = await _names(db, Organization, Organization.name,
                                 {t.organization_id for t in timelines if t.organization_id})
    out = []
    for progression, timeline in rows:
        parts = [
            characters.get(timeline.character_id), characters.get(timeline.related_character_id),
            worlds.get(timeline.world_setting_id), careers.get(timeline.career_id),
            organizations.get(timeline.organization_id),
        ]
        out.append(StaleProgression(
            id=progression.id,
            timeline_id=timeline.id,
            subject_kind=timeline.subject_kind,
            character_id=timeline.character_id,
            related_character_id=timeline.related_character_id,
            world_setting_id=timeline.world_setting_id,
            career_id=timeline.career_id,
            organization_id=timeline.organization_id,
            subject_name=" · ".join(part for part in parts if part),
            field_key=progression.field_key,
            custom_field_id=progression.custom_field_id,
            anchor_label=progression.anchor_label,
            acknowledged=progression.id in acknowledged,
        ))
    return out


async def _assessment(
    db: AsyncSession, project_id: int, row: ConsistencyImpact, fingerprint: str,
) -> AssessmentOut:
    assessed = row.assessed_at is not None
    items = _items(row) if assessed else []
    uncovered = [value for value in (row.uncovered or []) if type(value) is int] if assessed else []
    info: dict[int, tuple[int, str, str]] = {}
    for chunk in batched(sorted({item.get("chapter_id") for item in items} | set(uncovered)), 200):
        rows = await db.execute(select(Chapter.id, Chapter.chapter_no, Chapter.title, Chapter.content).where(
            Chapter.project_id == project_id, Chapter.id.in_(chunk),
        ))
        for value in rows:
            info[value.id] = (value.chapter_no, value.title, text_hash(value.content))
    hashes = {chapter_id: value[2] for chapter_id, value in info.items()}
    item_outs = [
        ImpactItemOut.model_validate({
            **item,
            "chapter_no": info.get(item.get("chapter_id"), (None, None))[0],
            "chapter_title": info.get(item.get("chapter_id"), (None, None))[1],
            "changed": _item_changed(item, hashes),
        })
        for item in items
    ]
    # 当前章号在前；已删除的章节排在最后
    item_outs.sort(key=lambda item: (item.chapter_no is None, item.chapter_no or 0, item.chapter_id))
    refs = [
        ImpactChapterRef(chapter_id=chapter_id, chapter_no=info.get(chapter_id, (None, None))[0],
                         chapter_title=info.get(chapter_id, (None, None))[1])
        for chapter_id in uncovered
    ]
    refs.sort(key=lambda ref: (ref.chapter_no is None, ref.chapter_no or 0, ref.chapter_id))
    return AssessmentOut(
        assessed=assessed,
        assessed_at=row.assessed_at,
        outdated=assessed and row.source_hash != fingerprint,
        no_change=assessed and not row.changes,
        changes=[ImpactChange.model_validate(change) for change in (row.changes or [])] if assessed else [],
        items=item_outs,
        uncovered=refs,
        model=row.model,
    )


async def _detail(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> ImpactDetail:
    chapter = await _chapter(db, project_id, chapter_id)
    fingerprint = (await _fingerprints(db, [chapter]))[chapter.id]
    info = (await derived_stale(db, project_id, [chapter], {chapter.id: fingerprint}))[chapter.id]
    row = await db.scalar(
        select(ConsistencyImpact)
        .where(ConsistencyImpact.chapter_id == chapter.id, ConsistencyImpact.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    current = (await ledger.ledgers_by_chapter(db, project_id, [chapter.id])).get(chapter.id)
    acknowledged = _acknowledged(row)
    progressions = await _progression_outs(db, info.progressions, acknowledged)
    block = assess_block(chapter, current, fingerprint)
    return ImpactDetail(
        chapter_id=chapter.id,
        chapter_no=chapter.chapter_no,
        chapter_title=chapter.title,
        analyzed=info.analyzed,
        stale_summary=(
            StaleSummary(analysis_id=info.summary.id, analyzed_at=info.summary.created_at)
            if info.summary is not None else None
        ),
        stale_memories=[StaleMemory(id=m.id, kind=m.kind, content=m.content) for m in info.memories],
        stale_progressions=progressions,
        stale_count=(int(info.summary is not None) + len(info.memories)
                     + sum(1 for item in progressions if not item.acknowledged)),
        ledger_state=_ledger_state(current, fingerprint),
        can_assess=block is None,
        assess_block=block,
        assessment=await _assessment(db, project_id, row, fingerprint) if row is not None else None,
        revision=row.revision if row is not None else 0,
        **_running(await open_impact(db, project_id), user_id),
    )


async def get_impact(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> ImpactDetail:
    await require_project(db, user_id, project_id, "content.read")
    return await _detail(db, user_id, project_id, chapter_id)


# ---------------- 确认状态 ----------------


async def _chapter_progression_ids(db: AsyncSession, project_id: int, chapter_id: int, ids: set[int]) -> set[int]:
    """ids 中确由该章分析写入的设定变化（不论是否过期）。"""
    found: set[int] = set()
    for chunk in batched(sorted(ids), 500):
        found |= set(await db.scalars(
            select(SettingProgression.id)
            .join(SettingTimeline, SettingTimeline.id == SettingProgression.timeline_id)
            .join(AnalysisChange, AnalysisChange.id == SettingProgression.source_change_id)
            .join(ChapterAnalysis, ChapterAnalysis.id == AnalysisChange.analysis_id)
            .where(SettingProgression.id.in_(chunk), SettingTimeline.project_id == project_id,
                   ChapterAnalysis.project_id == project_id, ChapterAnalysis.chapter_id == chapter_id)
        ))
    return found


async def update_impact(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, body: ImpactUpdate,
) -> ImpactDetail:
    """影响项确认状态与「已确认无需更新」的 CAS：持作品行锁（与评估任务的落库事务串行）。

    修订不符 → 409 code=stale_revision。尚无记录时 revision 传 0，只能保存 acknowledged（创建一行未评估的记录）。
    acknowledged 是合并语义：只替换本章当前仍过期的设定变化的确认，其余已确认的 id 保留。
    """
    await require_project(db, user_id, project_id, "content.edit", for_update=True)
    chapter = await _chapter(db, project_id, chapter_id)
    row = await db.scalar(
        select(ConsistencyImpact)
        .where(ConsistencyImpact.chapter_id == chapter.id, ConsistencyImpact.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    if (row.revision if row is not None else 0) != body.revision:
        await db.rollback()
        raise _stale()
    values: dict[str, Any] = {}
    if body.items is not None:
        items = _items(row) if row is not None and row.assessed_at is not None else []
        wanted = {item.chapter_id: item.status for item in body.items}
        if not set(wanted) <= {item.get("chapter_id") for item in items}:
            await db.rollback()
            raise BadRequestError(ITEM_NOT_FOUND)
        if wanted:
            values["items"] = [
                {**item, "status": wanted.get(item.get("chapter_id"), item.get("status"))} for item in items
            ]
    if body.acknowledged is not None:
        wanted_ids = set(body.acknowledged)
        if await _chapter_progression_ids(db, project_id, chapter.id, wanted_ids) != wanted_ids:
            await db.rollback()
            raise BadRequestError(BAD_ACKNOWLEDGED)
        # 合并而非整体替换：请求只决定「本章当前仍过期的设定变化」里哪些已确认；
        # 此前确认过、现在不再过期的 id 原样保留（正文改回再改时不必重新确认），已不存在的 id 顺带清理。
        fingerprint = (await _fingerprints(db, [chapter]))[chapter.id]
        info = (await derived_stale(db, project_id, [chapter], {chapter.id: fingerprint}))[chapter.id]
        stale_now = {progression.id for progression, _ in info.progressions}
        kept = _acknowledged(row) - stale_now
        kept = await _chapter_progression_ids(db, project_id, chapter.id, kept)
        values["acknowledged"] = sorted(kept | wanted_ids)
    if row is None:
        db.add(ConsistencyImpact(
            project_id=project_id, chapter_id=chapter.id, changes=[], items=[], uncovered=[],
            acknowledged=values.get("acknowledged", []), revision=1, actor_id=user_id,
        ))
        try:
            await db.commit()
        except IntegrityError:
            # 并发创建：另一请求（或评估任务）已先建了本章的记录
            await db.rollback()
            raise _stale() from None
    else:
        result = await db.execute(
            update(ConsistencyImpact)
            .where(ConsistencyImpact.id == row.id, ConsistencyImpact.project_id == project_id,
                   ConsistencyImpact.revision == body.revision)
            .values(**values, revision=ConsistencyImpact.revision + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise _stale()
        await db.commit()
    return await _detail(db, user_id, project_id, chapter_id)
