"""分页提及列表与本地草稿预览；原文分批加载，匹配在线程中执行。"""

from functools import partial

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.mentions import (
    SOURCE_TYPES,
    EntityKind,
    MentionCatalog,
    MentionPage,
    MentionPreview,
    Occurrence,
    SourceType,
)
from app.services.mentions.context import query_context, select_entities, selected_sources, validate_chapter_range
from app.services.mentions.matcher import Dictionary, cache, iter_matches, span_out, text_hash
from app.services.mentions.sources import SourceDocument, iter_documents


async def catalog(db: AsyncSession, user_id: int, project_id: int) -> MentionCatalog:
    context = await query_context(db, user_id, project_id)
    return MentionCatalog(
        entities=list(context.dictionary.entities),
        dictionary_hash=context.dictionary.fingerprint,
        available_source_types=context.source_types,
    )


def _preview(text: str, dictionary: Dictionary) -> MentionPreview:
    return MentionPreview(
        spans=[span_out(hit, dictionary) for hit in iter_matches(text, dictionary)],
        content_hash=text_hash(text),
        dictionary_hash=dictionary.fingerprint,
    )


async def preview(db: AsyncSession, user_id: int, project_id: int, text: str) -> MentionPreview:
    context = await query_context(db, user_id, project_id)
    return await anyio.to_thread.run_sync(_preview, text, context.dictionary)


def _source_page(
    document: SourceDocument,
    project_id: int,
    dictionary: Dictionary,
    keys: set[tuple[str, int]],
    *,
    skip: int,
    take: int,
) -> tuple[int, list[Occurrence]]:
    content_hash = text_hash(document.text)
    items: list[Occurrence] = []
    total = 0
    scope = (project_id, document.ref.type, document.ref.id)
    for hit in cache.iter_matches(scope, document.text, dictionary, content_hash=content_hash):
        owners = dictionary.owners[hit.term]
        if not any((entity.kind, entity.id) in keys for entity in owners):
            continue
        if total >= skip and len(items) < take:
            items.append(
                Occurrence(
                    source=document.ref,
                    start=hit.start,
                    end=hit.end,
                    text=hit.term,
                    entities=list(owners),
                    content_hash=content_hash,
                    context_before=document.text[max(0, hit.start - 40) : hit.start],
                    context_after=document.text[hit.end : hit.end + 60],
                )
            )
        total += 1
    return total, items


async def list_mentions(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    *,
    entity_kind: EntityKind | None = None,
    entity_id: int | None = None,
    source_type: SourceType | None = None,
    chapter_from: int | None = None,
    chapter_to: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> MentionPage:
    validate_chapter_range(chapter_from, chapter_to)
    context = await query_context(db, user_id, project_id)
    entries = select_entities(context.dictionary, entity_kind=entity_kind, entity_id=entity_id)
    types = selected_sources(context, source_type)
    keys = {(e.kind, e.id) for e in entries}
    result = MentionPage(
        items=[],
        total=0,
        page=page,
        page_size=page_size,
        source_counts={kind: 0 for kind in SOURCE_TYPES},
        dictionary_hash=context.dictionary.fingerprint,
    )
    if not keys:
        return result
    offset = (page - 1) * page_size
    async for document in iter_documents(
        db, project_id, user_id, types, chapter_from=chapter_from, chapter_to=chapter_to
    ):
        count, items = await anyio.to_thread.run_sync(
            partial(
                _source_page,
                document,
                project_id,
                context.dictionary,
                keys,
                skip=max(0, offset - result.total),
                take=page_size - len(result.items),
            )
        )
        result.items.extend(items)
        result.total += count
        result.source_counts[document.ref.type] += count
    return result
