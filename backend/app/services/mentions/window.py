"""原文窗口与过期位置核验；资料分块重建不影响 document ID + 原文位置。"""

from functools import partial

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.schemas.mentions import SourceType, SourceWindow
from app.services.mentions.context import InvalidMentionQuery, query_context, selected_sources
from app.services.mentions.matcher import Dictionary, iter_matches, span_out, text_hash
from app.services.mentions.sources import SourceDocument, load_document

WINDOW_CHARS = 8000


def _window(
    document: SourceDocument,
    dictionary: Dictionary,
    *,
    start: int,
    end: int | None,
    content_hash: str | None,
) -> SourceWindow:
    current_hash = text_hash(document.text)
    stale = content_hash is not None and content_hash != current_hash
    target_start = target_end = None
    if stale:
        left, right = 0, min(len(document.text), WINDOW_CHARS)
    else:
        if start > len(document.text) or (end is not None and (end <= start or end > len(document.text))):
            raise InvalidMentionQuery("原文定位范围无效，请刷新提及列表")
        if end is not None and end - start > WINDOW_CHARS:
            raise InvalidMentionQuery("定位范围不能超过 8000 字")
        if end is not None:
            target_start, target_end = start, end
        right = min(len(document.text), max(start, end or start) + WINDOW_CHARS // 2)
        left = max(0, right - WINDOW_CHARS)
        # 较长合法目标必须完整放入窗口，同时保持窗口上限。
        if target_start is not None and left > target_start:
            left, right = target_start, min(len(document.text), target_start + WINDOW_CHARS)
    spans = []
    # 从原文开头迭代能严格保持“最早非重叠”的语义，窗口末端后立即停止。
    for hit in iter_matches(document.text, dictionary):
        if hit.start >= right:
            break
        if hit.start >= left and hit.end <= right:
            spans.append(span_out(hit, dictionary))
    return SourceWindow(
        source=document.ref,
        content_hash=current_hash,
        stale=stale,
        window_start=left,
        text=document.text[left:right],
        spans=spans,
        target_start=target_start,
        target_end=target_end,
    )


async def source_window(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    source_type: SourceType,
    source_id: int,
    *,
    start: int = 0,
    end: int | None = None,
    content_hash: str | None = None,
) -> SourceWindow:
    context = await query_context(db, user_id, project_id)
    selected_sources(context, source_type)
    document = await load_document(db, project_id, user_id, source_type, source_id)
    if document is None:
        raise NotFoundError("提及来源不存在")
    return await anyio.to_thread.run_sync(
        partial(
            _window,
            document,
            context.dictionary,
            start=start,
            end=end,
            content_hash=content_hash,
        )
    )
