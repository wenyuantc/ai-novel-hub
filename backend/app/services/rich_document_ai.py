"""可进入模型的作者区块投影：先按来源过滤，再按预算裁剪。"""

import codecs
import hashlib
import json
from dataclasses import dataclass, replace
from itertools import batched
from uuid import UUID

from sqlalchemy import LargeBinary, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Chapter, Scene, SceneSection
from app.schemas.rich_document import is_text_boundary
from app.services.rich_document_core import add_analysis_section, new_analysis_hasher

DEFAULT_MAX_ROWS = 32
MAX_GLOBAL_ROWS = 64
MAX_SECTION_CHARS = 2_000


@dataclass(frozen=True)
class VisibleSection:
    chapter_id: int
    scene_id: int
    key: UUID
    anchor: int
    order: int
    title: str
    content: str
    truncated: bool
    omitted_global: bool = False


async def count_visible_scene_sections(db: AsyncSession, project_id: int, scene_ids: list[int]) -> int:
    count = 0
    with db.no_autoflush:
        for batch in batched(sorted(set(scene_ids)), 200):
            count += int(await db.scalar(select(func.count()).select_from(SceneSection)
                                         .join(Scene, Scene.id == SceneSection.scene_id)
                                         .join(Chapter, Chapter.id == Scene.chapter_id).where(
                                             Chapter.project_id == project_id, Scene.id.in_(batch),
                                             Scene.active.is_(True), SceneSection.active.is_(True),
                                             SceneSection.include_in_ai.is_(True),
                                         )) or 0)
    return count


async def count_visible_chapter_sections(db: AsyncSession, project_id: int, chapter_id: int) -> int:
    with db.no_autoflush:
        return int(await db.scalar(select(func.count()).select_from(SceneSection)
                                   .join(Scene, Scene.id == SceneSection.scene_id)
                                   .join(Chapter, Chapter.id == Scene.chapter_id).where(
                                       Chapter.project_id == project_id, Chapter.id == chapter_id,
                                       Scene.active.is_(True), SceneSection.active.is_(True),
                                       SceneSection.include_in_ai.is_(True),
                                   )) or 0)


async def analysis_fingerprints(db: AsyncSession, chapters: list[Chapter]) -> dict[int, str]:
    """分析来源指纹；无开启区块时严格沿用旧版纯正文 SHA-256。

    所有开启区块都计入指纹（即使当前预算未选入也保守失效）；格式、高亮及关闭区块不计入。
    逐行流式读取，避免为全书状态同时物化区块正文。
    """
    result = {}
    with db.no_autoflush:
        for batch in batched(chapters, 200):
            by_id = {chapter.id: chapter for chapter in batch}
            hashers = {}
            scenes = (select(Scene.id, Scene.chapter_id,
                             (func.row_number().over(partition_by=Scene.chapter_id,
                                                     order_by=(Scene.sort, Scene.id)) - 1).label("scene_order"))
                      .where(Scene.chapter_id.in_(by_id), Scene.active.is_(True)).subquery())
            statement = (select(scenes.c.chapter_id, scenes.c.scene_order, SceneSection.anchor,
                                SceneSection.sort.label("section_sort"), SceneSection.logical_key,
                                SceneSection.title, SceneSection.content)
                         .join(scenes, scenes.c.id == SceneSection.scene_id)
                         .where(SceneSection.active.is_(True), SceneSection.include_in_ai.is_(True))
                         .order_by(scenes.c.chapter_id, scenes.c.scene_order, SceneSection.anchor,
                                   SceneSection.sort, SceneSection.id).execution_options(yield_per=1))
            cursor = await db.stream(statement)
            try:
                async for row in cursor.mappings():
                    chapter = by_id[row["chapter_id"]]
                    hasher = hashers.get(chapter.id)
                    if hasher is None:
                        hasher = hashers[chapter.id] = new_analysis_hasher(chapter.content)
                    try:
                        add_analysis_section(hasher, row["scene_order"], row["anchor"], row["section_sort"],
                                             UUID(row["logical_key"]), row["title"], row["content"])
                    except (ValueError, TypeError, AttributeError, UnicodeEncodeError) as exc:
                        raise ConflictError("作者区块结构无效，不能计算分析指纹", extra={
                            "code": "rich_document_invalid",
                        }) from exc
            finally:
                await cursor.close()
            revisions = dict((await db.execute(select(Chapter.id, Chapter.content_revision)
                                               .where(Chapter.id.in_(by_id)))).all())
            if any(revisions.get(chapter.id) != chapter.content_revision for chapter in batch):
                raise ConflictError("分析来源读取期间章节已变化，请重试", extra={"code": "scene_context_changed"})
            result.update({chapter.id: (hashers[chapter.id].hexdigest() if chapter.id in hashers
                                        else hashlib.sha256(chapter.content.encode("utf-8")).hexdigest())
                           for chapter in batch})
    return result


async def visible_sections(
    db: AsyncSession, project_id: int, *, scene_ids: list[int] | None = None, chapter_ids: list[int] | None = None,
    max_rows: int | None = None, text_limit: int = MAX_SECTION_CHARS, recent_first: bool = False,
    anchor_ranges: dict[int, tuple[int, int, bool]] | None = None,
) -> list[VisibleSection]:
    """仅从当前作品的活动景查询已开启区块，根本不读取关闭区块的标题/正文。"""
    if (scene_ids is None) == (chapter_ids is None):
        raise ValueError("必须明确指定场景或章节范围")
    if anchor_ranges is not None and (scene_ids is None or set(anchor_ranges) != set(scene_ids)):
        raise ValueError("区块页范围必须与场景范围一致")
    ids = scene_ids if scene_ids is not None else chapter_ids
    assert ids is not None
    max_rows = DEFAULT_MAX_ROWS if max_rows is None else max_rows
    if not 1 <= text_limit <= MAX_SECTION_CHARS or not 1 <= max_rows <= MAX_GLOBAL_ROWS:
        raise ValueError("区块上下文预算无效")
    unique_ids = list(dict.fromkeys(ids))
    batches = list(batched(unique_ids, 32 if recent_first else 200))
    result = []
    with db.no_autoflush:
        for batch_index, batch in enumerate(batches):
            if anchor_ranges is not None:
                conditions = []
                for scene_id in batch:
                    lower, upper, include_upper = anchor_ranges[scene_id]
                    end_condition = (SceneSection.anchor <= upper if include_upper else SceneSection.anchor < upper)
                    conditions.append(and_(Scene.id == scene_id, SceneSection.anchor >= lower, end_condition))
                scope = or_(*conditions)
            else:
                scope = Scene.id.in_(batch) if scene_ids is not None else Scene.chapter_id.in_(batch)
            chapter_order = Chapter.chapter_no.desc() if recent_first else Chapter.chapter_no
            chapter_id_order = Chapter.id.desc() if recent_first else Chapter.id
            scene_order = Scene.sort.desc() if recent_first and scene_ids is not None else Scene.sort
            remaining = max_rows - len(result)
            # 先按来源和顺序只取有界身份，随后才读取对应标题/正文前缀；避免窗口子查询
            # 对整本书提前物化 32×2000 字的内容。
            statement = (select(Scene.chapter_id, Chapter.content_revision, Scene.id.label("scene_id"),
                                SceneSection.id.label("section_id"), SceneSection.anchor,
                                SceneSection.sort.label("section_sort"))
                         .join(Scene, Scene.id == SceneSection.scene_id).join(Chapter, Chapter.id == Scene.chapter_id)
                         .where(Chapter.project_id == project_id, Scene.active.is_(True),
                                SceneSection.active.is_(True), SceneSection.include_in_ai.is_(True), scope)
                         .order_by(chapter_order, chapter_id_order, scene_order, Scene.id,
                                   SceneSection.anchor, SceneSection.sort, SceneSection.id).limit(remaining + 1))
            rows = (await db.execute(statement)).mappings().all()
            overflow = len(rows) > remaining
            if overflow:
                rows = rows[:remaining]
            if not rows:
                continue
            # SQLite 的文本 substr 在 NUL 处提前结束；BLOB 前缀按 UTF-8 最长 4 字节/码点取足，
            # 不读取完整大区块，也不会丢掉嵌入的 NUL。PG TEXT 本身不允许 NUL。
            content_prefix = (func.substr(cast(SceneSection.content, LargeBinary), 1, 4 * (text_limit + 1))
                              if db.get_bind().dialect.name == "sqlite"
                              else func.substr(SceneSection.content, 1, text_limit + 1))
            content_rows = (await db.execute(select(
                SceneSection.id, SceneSection.scene_id, SceneSection.logical_key, SceneSection.anchor,
                SceneSection.sort, SceneSection.title, content_prefix.label("content"),
            ).join(Scene, Scene.id == SceneSection.scene_id).join(Chapter, Chapter.id == Scene.chapter_id).where(
                SceneSection.id.in_([row["section_id"] for row in rows]), Chapter.project_id == project_id,
                Scene.active.is_(True), SceneSection.active.is_(True), SceneSection.include_in_ai.is_(True),
            ).limit(len(rows)))).mappings().all()
            content_by_id = {row["id"]: row for row in content_rows}
            texts = dict((await db.execute(select(Scene.id, Scene.content).where(
                Scene.id.in_({row["scene_id"] for row in rows}),
            ))).all())
            for row in rows:
                data = content_by_id.get(row["section_id"])
                if data is None or (data["scene_id"], data["anchor"], data["sort"]) != (
                    row["scene_id"], row["anchor"], row["section_sort"],
                ):
                    raise ConflictError("作者区块已变化，请重新构造上下文", extra={"code": "scene_context_changed"})
                text = texts.get(row["scene_id"])
                if text is None or not is_text_boundary(text, row["anchor"]) or not data["title"].strip():
                    raise ConflictError("作者区块结构无效，不能构造模型上下文", extra={
                        "code": "rich_document_invalid",
                    })
                try:
                    key = UUID(data["logical_key"])
                except (ValueError, TypeError, AttributeError) as exc:
                    raise ConflictError("作者区块身份无效，不能构造模型上下文", extra={
                        "code": "rich_document_invalid",
                    }) from exc
                source = data["content"]
                if isinstance(source, bytes | bytearray | memoryview):
                    try:
                        source = codecs.getincrementaldecoder("utf-8")("strict").decode(bytes(source), final=False)
                    except UnicodeDecodeError as exc:
                        raise ConflictError("作者区块包含无效 Unicode", extra={
                            "code": "rich_document_invalid",
                        }) from exc
                truncated = len(source) > text_limit
                clipped = source
                if truncated:
                    clipped = source[:text_limit]
                    if clipped.endswith("\r") and source[text_limit] == "\n":
                        clipped = clipped[:-1]
                result.append(VisibleSection(
                    chapter_id=row["chapter_id"], scene_id=row["scene_id"], key=key, anchor=row["anchor"],
                    order=row["section_sort"], title=data["title"], content=clipped, truncated=truncated,
                ))
            expected = {row["chapter_id"]: row["content_revision"] for row in rows}
            actual = dict((await db.execute(select(Chapter.id, Chapter.content_revision).where(
                Chapter.id.in_(expected), Chapter.project_id == project_id,
            ))).all())
            if expected != actual:
                raise ConflictError("作者区块读取期间章节已变化", extra={"code": "scene_context_changed"})
            if len(result) >= max_rows:
                if result and (overflow or batch_index + 1 < len(batches)):
                    result[-1] = replace(result[-1], omitted_global=True)
                break
    return result


def render_visible_sections(sections: list[VisibleSection], budget: int) -> str:
    """以来源可辨的非正文 JSON 块构造，预算不足时明确告知；绝不改变正式正文坐标。"""
    if not sections or budget <= 0:
        return ""
    header = "【作者参考区块（非正式正文）】\n"
    notice = "[部分已开启区块可能因预算或数量限制未纳入]\n"
    if len(header) + len(notice) > budget:
        return ""
    parts = [header]
    used = len(header)
    usable = budget - len(notice)
    omitted_budget = False
    for index, item in enumerate(sections):
        remaining = usable - used
        def encode(content: str, truncated: bool, *, title: str = item.title) -> str:
            return json.dumps({"名称": title, "内容": content, "已截断": truncated},
                              ensure_ascii=False, separators=(",", ":")) + "\n"
        chunk = encode(item.content, item.truncated)
        if len(chunk) > remaining:
            low, high = 0, len(item.content)
            while low < high:
                middle = (low + high + 1) // 2
                if len(encode(item.content[:middle], True)) <= remaining:
                    low = middle
                else:
                    high = middle - 1
            if 0 < low < len(item.content) and item.content[low - 1:low + 1] == "\r\n":
                low -= 1
            chunk = encode(item.content[:low], True)
            if len(chunk) > remaining:
                omitted_budget = True
                break
            parts.append(chunk)
            omitted_budget = index + 1 < len(sections)
            break
        parts.append(chunk)
        used += len(chunk)
    if omitted_budget or any(item.omitted_global for item in sections):
        parts.append(notice)
    return "".join(parts) if len(parts) > 1 else ""


async def scene_reference_context(
    db: AsyncSession, project_id: int, scenes: list[tuple[int, int]], budget: int,
) -> str:
    """当前景和前景共用预算，最近场景优先取材后按故事顺序展示。"""
    if budget < 100 or not scenes:
        return ""
    ranks = dict(scenes)
    prioritized = [scene_id for scene_id, _sort in sorted(scenes, key=lambda item: item[1], reverse=True)]
    count = await count_visible_scene_sections(db, project_id, prioritized)
    if not count:
        return ""
    row_budget = min(32, count, max(1, budget // 256))
    visible = await visible_sections(db, project_id, scene_ids=prioritized, max_rows=row_budget,
                                     text_limit=min(2_000, max(1, budget // row_budget)), recent_first=True)
    visible.sort(key=lambda item: (ranks[item.scene_id], item.anchor, item.order, str(item.key)))
    return render_visible_sections(visible, budget)
