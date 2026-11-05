"""四类权威原文适配；调用者先校验作品可读，聊天始终再次按真实会话归属过滤。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentConversation, AgentMessage, Chapter, KbDocument, ProjectNote
from app.schemas.mentions import SourceRef, SourceType


@dataclass(frozen=True)
class SourceDocument:
    ref: SourceRef
    text: str


def _statement(
    source_type: SourceType,
    project_id: int,
    user_id: int,
    *,
    chapter_from: int | None = None,
    chapter_to: int | None = None,
    source_id: int | None = None,
) -> Select:
    if source_type == "chapter":
        statement = select(Chapter.id, Chapter.title, Chapter.chapter_no, Chapter.content, Chapter.created_at).where(
            Chapter.project_id == project_id,
        )
        if chapter_from is not None:
            statement = statement.where(Chapter.chapter_no >= chapter_from)
        if chapter_to is not None:
            statement = statement.where(Chapter.chapter_no <= chapter_to)
        identity = Chapter.id
        statement = statement.order_by(Chapter.chapter_no, Chapter.id)
    elif source_type == "chat":
        statement = (
            select(
                AgentMessage.id,
                AgentConversation.title,
                AgentMessage.conversation_id,
                AgentMessage.content,
                AgentMessage.created_at,
            )
            .join(AgentConversation, AgentConversation.id == AgentMessage.conversation_id)
            .where(
                AgentConversation.project_id == project_id,
                AgentConversation.user_id == user_id,
                AgentMessage.role.in_(("user", "assistant")),
            )
            .order_by(AgentMessage.id)
        )
        identity = AgentMessage.id
    elif source_type == "note":
        statement = (
            select(ProjectNote.id, ProjectNote.title, ProjectNote.content, ProjectNote.created_at)
            .where(
                ProjectNote.project_id == project_id,
            )
            .order_by(ProjectNote.id)
        )
        identity = ProjectNote.id
    else:
        statement = (
            select(
                KbDocument.id,
                KbDocument.filename.label("title"),
                KbDocument.text.label("content"),
                KbDocument.created_at,
            )
            .where(
                KbDocument.project_id == project_id,
            )
            .order_by(KbDocument.id)
        )
        identity = KbDocument.id
    return statement.where(identity == source_id) if source_id is not None else statement


def _document(source_type: SourceType, row: Any) -> SourceDocument:
    return SourceDocument(
        SourceRef(
            type=source_type,
            id=row.id,
            title=row.title or "（无标题）",
            created_at=row.created_at,
            chapter_no=row.chapter_no if source_type == "chapter" else None,
            conversation_id=row.conversation_id if source_type == "chat" else None,
        ),
        row.content,
    )


async def iter_documents(
    db: AsyncSession,
    project_id: int,
    user_id: int,
    source_types: list[SourceType],
    *,
    chapter_from: int | None = None,
    chapter_to: int | None = None,
) -> AsyncIterator[SourceDocument]:
    for source_type in source_types:
        # 单份资料可能有300万字；一次只持有一份原文，其余来源使用小批次游标。
        batch_size = 1 if source_type == "knowledge" else 16
        statement = _statement(source_type, project_id, user_id, chapter_from=chapter_from, chapter_to=chapter_to)
        with db.no_autoflush:
            stream = await db.stream(statement.execution_options(yield_per=batch_size))
            try:
                async for rows in stream.partitions(batch_size):
                    for row in rows:
                        yield _document(source_type, row)
            finally:
                await stream.close()


async def load_document(
    db: AsyncSession,
    project_id: int,
    user_id: int,
    source_type: SourceType,
    source_id: int,
) -> SourceDocument | None:
    with db.no_autoflush:
        row = (await db.execute(_statement(source_type, project_id, user_id, source_id=source_id))).first()
    return _document(source_type, row) if row is not None else None
