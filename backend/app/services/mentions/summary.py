"""按筛选范围内的已保存正文聚合；空章和歧义不伪装成确定出场。"""

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.mentions import (
    ChapterMentionCount,
    EntityCategory,
    EntityKind,
    MentionSummary,
    MentionSummaryEntry,
    SummaryChapter,
)
from app.services.mentions.context import query_context, select_entities, validate_chapter_range
from app.services.mentions.matcher import Dictionary, cache, match_entities
from app.services.mentions.sources import SourceDocument, iter_documents


def _chapter_counts(
    document: SourceDocument,
    project_id: int,
    dictionary: Dictionary,
    keys: set[tuple[str, int]],
) -> tuple[bool, dict[tuple[str, int], list[int]]]:
    counts: dict[tuple[str, int], list[int]] = {}
    has_content = bool(document.text.strip())
    if keys:
        for hit in cache.iter_matches((project_id, "chapter", document.ref.id), document.text, dictionary):
            owners = match_entities(hit, dictionary)
            bucket = 0 if len(owners) == 1 else 1
            for entity in owners:
                key = (entity.kind, entity.id)
                if key in keys:
                    counts.setdefault(key, [0, 0])[bucket] += 1
    return has_content, counts


async def summarize(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    *,
    entity_kind: EntityKind | None = None,
    entity_id: int | None = None,
    category: EntityCategory | None = None,
    q: str | None = None,
    chapter_from: int | None = None,
    chapter_to: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> MentionSummary:
    validate_chapter_range(chapter_from, chapter_to)
    context = await query_context(db, user_id, project_id)
    entities = select_entities(context.dictionary, entity_kind=entity_kind, entity_id=entity_id, category=category, q=q)
    selected = entities[(page - 1) * page_size : page * page_size]
    entries = {
        (e.kind, e.id): MentionSummaryEntry(
            entity=e,
            mention_count=0,
            ambiguous_count=0,
            chapter_count=0,
            last_chapter_no=None,
            gap_chapters=None,
            counts=[],
        )
        for e in selected
    }
    result = MentionSummary(
        chapters=[], entries=list(entries.values()), total=len(entities), page=page, page_size=page_size
    )
    written = 0
    last_rank: dict[tuple[str, int], int] = {}
    async for document in iter_documents(
        db, project_id, user_id, ["chapter"], chapter_from=chapter_from, chapter_to=chapter_to
    ):
        has_content, counts = await anyio.to_thread.run_sync(
            _chapter_counts,
            document,
            project_id,
            context.dictionary,
            set(entries),
        )
        result.chapters.append(
            SummaryChapter(
                id=document.ref.id,
                chapter_no=document.ref.chapter_no,
                title=document.ref.title,
                has_content=has_content,
            )
        )
        if has_content:
            written += 1
        for key, (definite, ambiguous) in counts.items():
            entry = entries[key]
            entry.mention_count += definite
            entry.ambiguous_count += ambiguous
            entry.counts.append(
                ChapterMentionCount(chapter_id=document.ref.id, count=definite, ambiguous_count=ambiguous)
            )
            if definite:
                entry.chapter_count += 1
                entry.last_chapter_no = document.ref.chapter_no
                last_rank[key] = written
    for key, rank in last_rank.items():
        entries[key].gap_chapters = written - rank
    return result
