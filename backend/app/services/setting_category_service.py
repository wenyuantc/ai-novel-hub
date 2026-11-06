"""分类与整理标签写边界；读取复用独立一致快照，组合原语不提交。"""

from contextlib import asynccontextmanager

from pydantic import ValidationError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Project, SettingCategory, SettingCategoryRule
from app.models.setting_fields import SettingFieldDefinition, SettingFieldOption
from app.schemas import setting_categories as s
from app.schemas.audit import AuditField
from app.schemas.setting_fields import EntryKind, EntryType
from app.services import setting_catalog_service as catalog
from app.services import setting_category_query_service as query
from app.services import setting_field_service as fields
from app.services.audit_writer import AuditOperation
from app.services.authorization_service import AuthenticatedActor
from app.services.project_service import get_manageable_project
from app.services.setting_category_validation import assert_canonical_setting_tags as _assert_canonical_tags
from app.services.setting_category_validation import validate_category_graph_snapshot


async def categories_revision(db: AsyncSession, project_id: int) -> int:
    with db.no_autoflush:
        revision = await db.scalar(select(Project.categories_revision).where(Project.id == project_id))
    if revision is None:
        raise NotFoundError("项目不存在")
    return revision


async def check_categories_revision(db: AsyncSession, project_id: int, expected: int) -> int:
    revision = await categories_revision(db, project_id)
    if revision != expected:
        raise fields.stale_revision(revision, "categories")
    return revision


async def claim_categories_revision(db: AsyncSession, project_id: int, expected: int) -> int:
    """调用方持项目锁；整笔标签或分类操作只领取一次代次，不提交。"""
    result = await db.execute(
        update(Project)
        .where(Project.id == project_id, Project.categories_revision == expected)
        .values(categories_revision=Project.categories_revision + 1)
        .execution_options(synchronize_session="fetch")
    )
    if result.rowcount != 1:
        raise fields.stale_revision(await categories_revision(db, project_id), "categories")
    return expected + 1


@asynccontextmanager
async def _editing(db: AsyncSession, actor_id: int, project_id: int, field: AuditField):
    async with AuditOperation(db, actor_id, "project.update", "project", project_id, project_id=project_id) as audit:
        project = await get_manageable_project(db, actor_id, project_id, lock=True, permission="projects.edit")
        audit.target(project)
        audit.summary.changed_fields = [field]
        try:
            yield audit
        except IntegrityError as exc:
            await db.rollback()
            raise ConflictError("分类名称或规则与现有资料冲突", extra={"code": "category_conflict"}) from exc
        except BaseException:
            await db.rollback()
            raise


async def list_categories(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: AuthenticatedActor,
    project_id: int,
    *,
    types: list[EntryType] | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> s.CategoryPage:
    async with query.category_read_snapshot(sessionmaker, actor, project_id) as db:
        versions = await query._revisions(db, project_id)
        predicates = [SettingCategory.project_id == project_id]
        if q and q.strip():
            escaped = q.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_")
            predicates.append(SettingCategory.name.ilike(f"%{escaped}%", escape="!"))
        # 配置目录按请求页读取；类型数组最多九项，过滤在 SQL 内完成。
        if types is not None:
            expand = func.json_array_elements_text if db.bind.dialect.name == "postgresql" else func.json_each
            type_rows = expand(SettingCategory.applicable_types).table_valued("value")
            predicates.append(select(1).select_from(type_rows).where(type_rows.c.value.in_(types)).exists())
        total = await db.scalar(select(func.count()).select_from(SettingCategory).where(*predicates))
        ids = list(
            await db.scalars(
                select(SettingCategory.id)
                .where(*predicates)
                .order_by(SettingCategory.sort, SettingCategory.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        items = [await query.load_category(db, project_id, category_id) for category_id in ids]
        return s.CategoryPage(
            categories_revision=versions[0],
            fields_revision=versions[1],
            items=items,
            total=total or 0,
            page=page,
            page_size=page_size,
        )


async def read_category(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: AuthenticatedActor,
    project_id: int,
    category_id: int,
) -> s.CategoryMutation:
    async with query.category_read_snapshot(sessionmaker, actor, project_id) as db:
        versions = await query._revisions(db, project_id)
        item = await query.load_category(db, project_id, category_id)
        return s.CategoryMutation(item=item, categories_revision=versions[0], fields_revision=versions[1])


async def apply_category(
    db: AsyncSession,
    project_id: int,
    data: s.CategoryCreate | s.CategoryUpdate,
    category_id: int | None = None,
) -> s.CategoryOut:
    """持项目锁的组合事务原语；校验服务器旧规则，只 flush，不领取版本或提交。"""
    current = await query.load_category(db, project_id, category_id) if category_id is not None else None
    changes = data.model_dump(exclude_unset=True, exclude={"expected_categories_revision", "expected_fields_revision"})
    if not changes:
        raise BadRequestError("请选择有效的分类修改")
    values = current.to_spec().model_dump() if current is not None else {}
    values.update(changes)
    try:
        spec = s.CategorySpec.model_validate(values)
    except ValidationError as exc:
        raise BadRequestError("分类名称、适用类型或规则不合法", extra={"code": "invalid_category_rule"}) from exc
    await query.validate_category_spec(
        db, project_id, spec, retained_rules=current.rules if current is not None else ()
    )
    duplicate = select(SettingCategory.id).where(
        SettingCategory.project_id == project_id, SettingCategory.name == spec.name
    )
    if category_id is not None:
        duplicate = duplicate.where(SettingCategory.id != category_id)
    if await db.scalar(duplicate) is not None:
        raise ConflictError("同名分类已存在", extra={"code": "category_conflict"})
    values = spec.model_dump(exclude={"rules"})
    if current is None:
        category = SettingCategory(project_id=project_id, **values)
        db.add(category)
        await db.flush()
        category_id = category.id
    else:
        await db.execute(update(SettingCategory).where(SettingCategory.id == category_id).values(**values))
    if current is None or "rules" in changes:
        if current is not None:
            await db.execute(delete(SettingCategoryRule).where(SettingCategoryRule.category_id == category_id))
        db.add_all(SettingCategoryRule(category_id=category_id, **rule.model_dump()) for rule in spec.rules)
    await db.flush()
    return await query.load_category(db, project_id, category_id)


async def save_category(
    db: AsyncSession,
    actor_id: int,
    project_id: int,
    data: s.CategoryCreate | s.CategoryUpdate,
    category_id: int | None = None,
) -> s.CategoryMutation:
    async with _editing(db, actor_id, project_id, "setting_categories") as audit:
        await check_categories_revision(db, project_id, data.expected_categories_revision)
        field_revision = await fields.check_fields_revision(db, project_id, data.expected_fields_revision)
        item = await apply_category(db, project_id, data, category_id)
        revision = await claim_categories_revision(db, project_id, data.expected_categories_revision)
        await audit.commit()
    return s.CategoryMutation(item=item, categories_revision=revision, fields_revision=field_revision)


async def delete_category(
    db: AsyncSession,
    actor_id: int,
    project_id: int,
    category_id: int,
    expected: int,
) -> s.CategoryVersions:
    async with _editing(db, actor_id, project_id, "setting_categories") as audit:
        await check_categories_revision(db, project_id, expected)
        await query.load_category(db, project_id, category_id)
        await db.execute(
            delete(SettingCategory).where(SettingCategory.project_id == project_id, SettingCategory.id == category_id)
        )
        revision = await claim_categories_revision(db, project_id, expected)
        field_revision = await fields.fields_revision(db, project_id)
        await audit.commit()
    return s.CategoryVersions(categories_revision=revision, fields_revision=field_revision)


async def _entry_tags(db: AsyncSession, project_id: int, kind: EntryKind, entry_id: int):
    entry = await catalog.get_entry(db, project_id, kind, entry_id)
    model = catalog.ENTRY_MODELS[kind]
    value = await db.scalar(select(model.setting_tags).where(model.project_id == project_id, model.id == entry_id))
    _assert_canonical_tags(value)
    return entry, value


async def read_tags(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: AuthenticatedActor,
    project_id: int,
    kind: EntryKind,
    entry_id: int,
) -> s.SettingTagsOut:
    async with query.category_read_snapshot(sessionmaker, actor, project_id) as db:
        revision = await categories_revision(db, project_id)
        entry, tags = await _entry_tags(db, project_id, kind, entry_id)
        return s.SettingTagsOut(entry=entry, setting_tags=tags, categories_revision=revision)


async def apply_entry_tags(
    db: AsyncSession,
    project_id: int,
    kind: EntryKind,
    entry_id: int,
    setting_tags: list[str],
) -> list[str]:
    """调用方已授权、持项目写锁并负责一次 categories CAS；规范化后只 flush，不提交。

    导入/恢复应先验证原始载荷，再使用本原语或插入后的整图校验；不能用规范化掩盖坏快照。
    """
    await catalog.get_entry(db, project_id, kind, entry_id)
    try:
        tags = s.normalize_setting_tags(setting_tags)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    model = catalog.ENTRY_MODELS[kind]
    await db.execute(
        update(model).where(model.project_id == project_id, model.id == entry_id).values(setting_tags=tags)
    )
    await db.flush()
    return tags


async def save_tags(
    db: AsyncSession,
    actor_id: int,
    project_id: int,
    kind: EntryKind,
    entry_id: int,
    data: s.SettingTagsUpdate,
) -> s.SettingTagsOut:
    async with _editing(db, actor_id, project_id, "setting_tags") as audit:
        await check_categories_revision(db, project_id, data.expected_categories_revision)
        tags = await apply_entry_tags(db, project_id, kind, entry_id, data.setting_tags)
        entry = await catalog.get_entry(db, project_id, kind, entry_id)
        revision = await claim_categories_revision(db, project_id, data.expected_categories_revision)
        await audit.commit()
    return s.SettingTagsOut(entry=entry, setting_tags=tags, categories_revision=revision)


async def suggest_tags(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: AuthenticatedActor,
    project_id: int,
    *,
    q: str | None = None,
    limit: int = 30,
) -> s.SettingTagSuggestions:
    """数据库展开、去重和限量；Python最多持有 limit+1 个标签，不加载全部条目。"""
    async with query.category_read_snapshot(sessionmaker, actor, project_id) as db:
        revision = await categories_revision(db, project_id)
        postgres = db.bind.dialect.name == "postgresql"
        selects = []
        for model in catalog.ENTRY_MODELS.values():
            # 表名来自封闭模型注册表；用户内容只通过绑定参数进入SQL。
            expansion = (
                "LATERAL jsonb_array_elements_text(entity.setting_tags::jsonb) AS tags(value)"
                if postgres
                else "json_each(entity.setting_tags) AS tags"
            )
            selects.append(
                f"SELECT tags.value AS tag FROM {model.__tablename__} AS entity "
                f"CROSS JOIN {expansion} WHERE entity.project_id = :pid"
            )
        pattern = (q or "").strip().lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
        statement = text(
            "SELECT DISTINCT tag FROM (" + " UNION ALL ".join(selects) + ") AS entry_tags "
            "WHERE lower(tag) LIKE :pattern ESCAPE '!' ORDER BY tag LIMIT :limit"
        )
        tags = list(await db.scalars(statement, {"pid": project_id, "pattern": f"%{pattern}%", "limit": limit + 1}))
        return s.SettingTagSuggestions(items=tags[:limit], has_more=len(tags) > limit, categories_revision=revision)


async def validate_project_category_graph(db: AsyncSession, project_id: int) -> None:
    """验证调用方已flush的资料图；不授权、不写入、不领取版本、不提交。

    导入及effects在自己的项目屏障/事务内显式调用，另保留字段整图校验。
    历史停用规则仅以服务器实际已存规则作为retained来源。
    """
    with db.no_autoflush:
        for model in catalog.ENTRY_MODELS.values():
            result = await db.stream(select(model.setting_tags).where(model.project_id == project_id))
            async for tags in result.scalars():
                _assert_canonical_tags(tags)
        categories = (
            (
                await db.execute(
                    select(SettingCategory.__table__).where(
                        SettingCategory.project_id == project_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        rules = (
            (
                await db.execute(
                    select(SettingCategoryRule.__table__)
                    .join(SettingCategory, SettingCategory.id == SettingCategoryRule.category_id)
                    .where(SettingCategory.project_id == project_id)
                )
            )
            .mappings()
            .all()
        )
        definitions = (
            (
                await db.execute(
                    select(SettingFieldDefinition.__table__).where(
                        SettingFieldDefinition.project_id == project_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        options = (
            (
                await db.execute(
                    select(SettingFieldOption.__table__)
                    .join(SettingFieldDefinition, SettingFieldDefinition.id == SettingFieldOption.definition_id)
                    .where(SettingFieldDefinition.project_id == project_id)
                )
            )
            .mappings()
            .all()
        )
        validate_category_graph_snapshot(
            project_id, categories=categories, rules=rules, definitions=definitions, options=options
        )
