"""分类元数据校验与有界只读查询；每次跨批查询使用独立一致快照。"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import BadRequestError, ConflictError, NotFoundError, UnauthorizedError
from app.models import Project, SettingCategory, SettingCategoryRule
from app.schemas import setting_categories as s
from app.schemas.progression import StoryPosition
from app.schemas.setting_fields import DefinitionOut, OptionOut
from app.services import authorization_service as authz
from app.services import setting_catalog_service as catalog
from app.services import setting_field_service as fields
from app.services.project_service import get_manageable_project
from app.services.setting_category_matcher import match_category
from app.services.setting_category_validation import validate_category_spec_metadata
from app.services.setting_field_values import is_positive_db_int
from app.services.setting_progression_service import _chapter_for_anchor


@dataclass(frozen=True)
class CategoryMetadata:
    definitions: dict[int, DefinitionOut]
    options: dict[int, OptionOut]


async def validate_category_spec(
    db: AsyncSession,
    project_id: int,
    spec: s.CategorySpec,
    *,
    retained_rules: Sequence[s.CategoryRule] = (),
) -> CategoryMetadata:
    """已授权事务内读写共用的规则校验，retained必须来自服务器原分类。

    新建/新预览传空；保存分类读取传自己的已存规则；编辑只传服务器原规则。
    因此停用身份可以原样保留，不能用一个全局开关放行新停用条件。
    """
    selected = {rule.definition_id for rule in spec.rules if rule.kind == "dropdown"}
    with db.no_autoflush:
        definitions, options_by_definition = await fields._metadata(db, project_id, definition_ids=selected)
        by_id = {definition.id: definition for definition in definitions}
        options = {option.id: option for values in options_by_definition.values() for option in values}
        validate_category_spec_metadata(
            project_id,
            spec,
            [definition.model_dump() for definition in definitions],
            [option.model_dump() for option in options.values()],
            retained_rules=retained_rules,
        )
        return CategoryMetadata(by_id, options)


async def load_category(db: AsyncSession, project_id: int, category_id: int) -> s.CategoryOut:
    """已授权事务内的当前分类快照，不复用可能过期的ORM对象。"""
    with db.no_autoflush:
        row = (
            await db.execute(
                select(
                    SettingCategory.id,
                    SettingCategory.project_id,
                    SettingCategory.name,
                    SettingCategory.applicable_types,
                    SettingCategory.match_mode,
                    SettingCategory.sort,
                ).where(SettingCategory.project_id == project_id, SettingCategory.id == category_id)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("设定分类不存在")
        rules = []
        for rule in await db.execute(
            select(SettingCategoryRule.__table__)
            .where(
                SettingCategoryRule.category_id == category_id,
            )
            .order_by(SettingCategoryRule.sort, SettingCategoryRule.id)
            .limit(21)
        ):
            payload = {"id": rule.id, "category_id": category_id, "sort": rule.sort, "kind": rule.kind}
            if rule.kind == "tag":
                payload["tag"] = rule.tag
            else:
                payload.update(definition_id=rule.definition_id, option_id=rule.option_id)
            rules.append(payload)
        try:
            return s.CategoryOut.model_validate({**dict(row._mapping), "rules": rules})
        except ValidationError as exc:
            raise ConflictError("分类规则资料不完整，请修正分类", extra={"code": "invalid_category_graph"}) from exc


@asynccontextmanager
async def category_read_snapshot(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: authz.AuthenticatedActor,
    project_id: int,
) -> AsyncIterator[AsyncSession]:
    """SQL前配置独立连接，认证/授权/生命周期和全部候选批次共享快照。"""
    if not isinstance(actor, authz.AuthenticatedActor):
        raise TypeError("分类快照必须携带已认证的真实actor身份")
    async with sessionmaker() as db:
        try:
            if db.bind.dialect.name == "postgresql":
                connection = await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            elif db.bind.dialect.name == "sqlite":
                connection = await db.connection()
                # legacy sqlite驱动的Session.begin仅是逻辑事务，SELECT前显式建立真实BEGIN。
                await connection.exec_driver_sql("BEGIN")
            else:
                raise ValueError("分类一致快照只支持PostgreSQL与SQLite")
            with db.no_autoflush:
                try:
                    current = await authz.snapshot(db, actor.id)
                except NotFoundError:
                    raise UnauthorizedError("未登录或登录已过期") from None
                if not current.is_active or current.token_version != actor.token_version:
                    raise UnauthorizedError("未登录或登录已过期")
                db.info["authenticated_actor"] = actor
                await get_manageable_project(db, actor.id, project_id)
                yield db
        finally:
            await db.rollback()


async def _revisions(db: AsyncSession, project_id: int) -> tuple[int, int]:
    row = (
        await db.execute(
            select(Project.categories_revision, Project.fields_revision).where(
                Project.id == project_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("项目不存在")
    return row.categories_revision, row.fields_revision


def _reasons(category: s.CategoryOut | s.CategorySpec, indexes: tuple[int, ...], metadata: CategoryMetadata):
    result = []
    for index in indexes:
        rule = category.rules[index]
        common = {"rule_index": index, "rule_id": getattr(rule, "id", None), "kind": rule.kind}
        if rule.kind == "tag":
            result.append(s.CategoryMatchReason(**common, tag=rule.tag, description=f"整理标签：{rule.tag}"))
        else:
            definition = metadata.definitions[rule.definition_id]
            option = metadata.options[rule.option_id]
            result.append(
                s.CategoryMatchReason(
                    **common,
                    definition_id=definition.id,
                    definition_label=definition.label,
                    option_id=option.id,
                    option_label=option.label,
                    description=f"{definition.label}：{option.label}",
                )
            )
    return result


async def query_category_entries(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: authz.AuthenticatedActor,
    project_id: int,
    *,
    category_id: int | None = None,
    draft: s.CategorySpec | None = None,
    editing_category_id: int | None = None,
    filters: s.CategoryEntryQuery | None = None,
) -> s.CategoryEntryPage:
    """保存分类与未保存预览共用查询；总数在完整规则匹配后累计。"""
    if (category_id is None) == (draft is None) or (editing_category_id is not None and draft is None):
        raise BadRequestError("请选择一个已保存分类或预览草稿")
    if any(value is not None and not is_positive_db_int(value) for value in (category_id, editing_category_id)):
        raise BadRequestError("分类编号不合法")
    filters = filters.model_copy(deep=True) if filters is not None else s.CategoryEntryQuery()
    draft = draft.model_copy(deep=True) if draft is not None else None
    async with category_read_snapshot(sessionmaker, actor, project_id) as db:
        before = await _revisions(db, project_id)
        if category_id is not None:
            category = await load_category(db, project_id, category_id)
            spec = category.to_spec()
            retained = category.rules
        else:
            category = draft
            spec = draft
            retained = (await load_category(db, project_id, editing_category_id)).rules if editing_category_id else ()
        metadata = await validate_category_spec(db, project_id, spec, retained_rules=retained)
        chapter_no = filters.chapter_no
        if filters.chapter_id is not None:
            chapter_no = (await _chapter_for_anchor(db, project_id, filters.chapter_id)).chapter_no
        position = (
            StoryPosition(chapter_id=filters.chapter_id, chapter_no=chapter_no, boundary=filters.boundary)
            if chapter_no
            else None
        )
        types = [
            entry_type for entry_type in spec.applicable_types if filters.types is None or entry_type in filters.types
        ]
        offset = (filters.page - 1) * filters.page_size
        total, items, after = 0, [], None
        while types:
            candidates = await catalog.candidate_batch(db, project_id, types=types, q=filters.q, after=after, limit=200)
            if not candidates:
                break
            projections = (
                await fields.resolve_entry_fields(
                    db,
                    project_id,
                    [candidate.entry.reference for candidate in candidates],
                    position,
                    definition_ids=set(metadata.definitions),
                )
                if metadata.definitions
                else {}
            )
            for candidate in candidates:
                ref = candidate.entry.reference
                projection = projections.get((ref.kind, ref.id))
                values = {}
                if projection is not None:
                    for slot in projection.fields:
                        if candidate.entry.type in metadata.definitions[slot.definition_id].applicable_types:
                            values[slot.definition_id] = slot.value["option_id"] if slot.value is not None else None
                match = match_category(spec, candidate.entry.type, candidate.setting_tags, values)
                if match.matched:
                    if offset <= total < offset + filters.page_size:
                        items.append(
                            s.CategoryEntryOut(
                                **candidate.entry.model_dump(),
                                setting_tags=list(candidate.setting_tags),
                                reasons=_reasons(category, match.matched_rule_indexes, metadata),
                            )
                        )
                    total += 1
            after = candidates[-1].cursor
            if len(candidates) < 200:
                break
        after_revisions = await _revisions(db, project_id)
        if before != after_revisions:
            scope = "categories" if before[0] != after_revisions[0] else "fields"
            raise fields.stale_revision(after_revisions[0 if scope == "categories" else 1], scope)
        return s.CategoryEntryPage(
            category=category,
            categories_revision=before[0],
            fields_revision=before[1],
            position=position,
            items=items,
            total=total,
            page=filters.page,
            page_size=filters.page_size,
        )
