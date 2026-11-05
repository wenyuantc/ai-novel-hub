"""每次查询重新验证作品权限与当前词典；无任何业务写入。"""

from dataclasses import dataclass

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFoundError
from app.models import Character, WorldSetting
from app.schemas.mentions import SOURCE_TYPES, EntityCategory, EntityKind, EntityRef, SourceType
from app.services.mentions.matcher import Dictionary, compile_dictionary
from app.services.project_service import get_manageable_project


class InvalidMentionQuery(AppError):
    status_code = 422


@dataclass(frozen=True)
class QueryContext:
    dictionary: Dictionary
    source_types: list[SourceType]


async def query_context(db: AsyncSession, user_id: int, project_id: int) -> QueryContext:
    with db.no_autoflush:
        project = await get_manageable_project(db, user_id, project_id)
        available = list(SOURCE_TYPES) if project.user_id == user_id else [t for t in SOURCE_TYPES if t != "chat"]
        characters = await db.execute(
            select(Character.id, Character.name, Character.aliases).where(
                Character.project_id == project_id,
            )
        )
        worlds = await db.execute(
            select(WorldSetting.id, WorldSetting.title, WorldSetting.aliases, WorldSetting.category).where(
                WorldSetting.project_id == project_id,
                WorldSetting.category.in_(("place", "item")),
            )
        )
    entities = [
        EntityRef(kind="character", id=c.id, name=c.name, category="character", aliases=c.aliases or [])
        for c in characters
    ]
    entities.extend(
        EntityRef(kind="world", id=w.id, name=w.title, category=w.category, aliases=w.aliases or []) for w in worlds
    )
    dictionary = await anyio.to_thread.run_sync(compile_dictionary, entities)
    return QueryContext(dictionary, available)


def select_entities(
    dictionary: Dictionary,
    *,
    entity_kind: EntityKind | None = None,
    entity_id: int | None = None,
    category: EntityCategory | None = None,
    q: str | None = None,
) -> list[EntityRef]:
    if entity_id is not None and entity_kind is None:
        raise InvalidMentionQuery("指定条目 ID 时必须同时指定条目类型")
    entries = [e for e in dictionary.entities if entity_kind is None or e.kind == entity_kind]
    if entity_id is not None:
        entries = [e for e in entries if e.id == entity_id]
        if not entries:
            raise NotFoundError("设定条目不存在")
    if category is not None:
        entries = [e for e in entries if e.category == category]
    if q and (query := q.strip().casefold()):
        entries = [e for e in entries if any(query in value.casefold() for value in (e.name, *e.aliases))]
    return entries


def validate_chapter_range(chapter_from: int | None, chapter_to: int | None) -> None:
    if chapter_from is not None and chapter_to is not None and chapter_from > chapter_to:
        raise InvalidMentionQuery("起始章节不能大于结束章节")


def selected_sources(context: QueryContext, source_type: SourceType | None) -> list[SourceType]:
    if source_type is None:
        return context.source_types
    if source_type not in context.source_types:
        raise NotFoundError("提及来源不存在")
    return [source_type]
