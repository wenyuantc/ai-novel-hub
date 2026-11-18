"""连载定稿的单书备份边界与预检。所有发布稿始终属于核心作品资料。"""

from collections.abc import Callable
from typing import NamedTuple

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Chapter
from app.models.serialization import (
    SerializationEdition,
    SerializationEntry,
    SerializationGoal,
    SerializationPublication,
)
from app.schemas.reader import ReaderBlock
from app.schemas.rich_document import require_range
from app.services.serialization_snapshot import edition_fingerprint

SERIALIZATION_TABLES = (
    "serialization_goals", "serialization_entries", "serialization_editions", "serialization_publications",
)


class SerializationState(NamedTuple):
    goal_revision: int | None
    entry_count: int
    entry_revisions: int
    publications: int


async def serialization_state(db: AsyncSession, project_id: int) -> SerializationState:
    """定稿推进条目版本、发布只追加。一个查询核实这些不由章节版本反映的变化。"""
    goal = select(SerializationGoal.revision).where(SerializationGoal.project_id == project_id)
    entries = select(func.count()).select_from(SerializationEntry).where(SerializationEntry.project_id == project_id)
    revisions = select(func.coalesce(func.sum(SerializationEntry.revision), 0)).where(
        SerializationEntry.project_id == project_id
    )
    publications = select(func.count()).select_from(SerializationPublication).where(
        SerializationPublication.project_id == project_id
    )
    foreign_chapter = select(SerializationEntry.id).join(Chapter, Chapter.id == SerializationEntry.chapter_id).where(
        SerializationEntry.project_id != Chapter.project_id,
        or_(SerializationEntry.project_id == project_id, Chapter.project_id == project_id),
    ).exists()
    foreign_edition = select(SerializationPublication.id).join(
        SerializationEdition, SerializationEdition.id == SerializationPublication.edition_id,
    ).join(SerializationEntry, SerializationEntry.id == SerializationEdition.entry_id).where(
        SerializationPublication.project_id != SerializationEntry.project_id,
        or_(SerializationPublication.project_id == project_id, SerializationEntry.project_id == project_id),
    ).exists()
    row = (await db.execute(select(goal.scalar_subquery(), entries.scalar_subquery(), revisions.scalar_subquery(),
                                   publications.scalar_subquery(), foreign_chapter, foreign_edition))).one()
    if row[4] or row[5]:
        raise ConflictError("连载记录的作品归属不一致，无法导出")
    return SerializationState(*row[:4])


def _validate_blocks(content: str, blocks: list[dict] | None) -> None:
    if blocks is None:
        return
    cursor = 0
    for raw in blocks:
        block = ReaderBlock.model_validate(raw, strict=True, extra="forbid")
        require_range(content, block.start, block.end, allow_empty=True)
        if block.start < cursor or content[cursor:block.start].strip("\r\n"):
            raise ValueError("排版块必须覆盖全部正文并依序排列")
        if "\n" in content[block.start:block.end] or "\r" in content[block.start:block.end]:
            raise ValueError("排版块不能跨越行分隔符")
        if block.kind == "heading":
            if block.level is None or not 1 <= block.level <= 6:
                raise ValueError("标题层级必须为 1 至 6")
        elif block.level is not None:
            raise ValueError("只有标题块可指定层级")
        offset = block.start
        for run in block.runs:
            if run.start != offset or not offset < run.end <= block.end:
                raise ValueError("行内排版范围不连续或超出段落")
            offset = run.end
        if offset != block.end:
            raise ValueError("行内排版未覆盖段落")
        cursor = block.end
    if content[cursor:].strip("\r\n"):
        raise ValueError("排版块未覆盖正文结尾")


def validate_serialization_tables(tables: dict[str, list[dict]], project_id: int, error: Callable[[str], None]) -> None:
    goals, entries, editions, publications = (tables.get(table, []) for table in SERIALIZATION_TABLES)
    if len(goals) > 1 or any(row.get("daily_goal") is not None and row["daily_goal"] <= 0 for row in goals):
        error("每部作品只允许一份正整章数的日更目标")
    # 作品外键由导入器填入目标作品。显式携带的外书归属须在预览阶段被识别。
    for rows in (goals, entries, publications):
        if any("project_id" in row and (type(row["project_id"]) is not int or row["project_id"] != project_id)
               for row in rows):
            error("连载记录的来源作品与备份不一致")

    published_editions = {row["edition_id"] for row in publications}
    for row in publications:
        channel = row["channel"]
        if not row.get("created_at") or not channel.strip() or channel != channel.strip() or "\x00" in channel:
            error(f"发布记录 #{row['id']} 的登记时间或平台无效")

    live_chapters: set[int] = set()
    detached = {row["id"] for row in entries if row.get("chapter_id") is None}
    seen_seqs: set[tuple[int, int]] = set()
    finalized: set[int] = set()
    for row in editions:
        eid, seq = row["entry_id"], row["seq"]
        key = (eid, seq)
        if seq < 1 or key in seen_seqs:
            error(f"定稿 #{row['id']} 在本章的稿次重复或无效")
        seen_seqs.add(key)
        finalized.add(eid)
        if eid in detached and row["id"] not in published_editions:
            error(f"源章节已删除的定稿 #{row['id']} 缺少发布记录")
        number, title = row["chapter_no_at_confirmation"], row["title"]
        content, blocks = row["content"], row.get("blocks")
        try:
            if number < 1 or not content.strip() or not row.get("created_at"):
                raise ValueError("冻结的章号、正文或确认时间无效")
            _validate_blocks(content, blocks)
            if row["fingerprint"] != edition_fingerprint(number, title, content, blocks):
                raise ValueError("标题、正文或排版与定稿指纹不一致")
        except ValidationError as exc:
            error(f"定稿 #{row['id']} 的读者排版结构无效：{exc.errors()[0]['msg']}")
        except ValueError as exc:
            error(f"定稿 #{row['id']} 无效：{exc}")
    for row in entries:
        cid, stage = row.get("chapter_id"), row.get("stage")
        if stage not in ("revision", "proofreading", "final") or stage == "final" and row["id"] not in finalized:
            error(f"连载条目 #{row['id']} 的状态与定稿不一致")
        if cid is None:
            if row["id"] not in finalized or row.get("planned_date") is not None:
                error("源章节已删除的条目必须有发布留档，并清除待发排期")
        elif cid in live_chapters:
            error("同一章节不可有多个连载条目")
        else:
            live_chapters.add(cid)
