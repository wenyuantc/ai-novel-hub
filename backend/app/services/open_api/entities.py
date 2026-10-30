"""分页读取当前创作设定，保留现有领域派生和视角语义。"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import ChapterPlan, Character, Organization, OrganizationMember, Relationship, RelationshipType
from app.schemas import open_api as out
from app.schemas.organization import MemberStatus
from app.schemas.relationship import RelationCategory, RelationStatus
from app.schemas.story import WorldCategory
from app.services import organization_service, plan_service, relationship_service, story_service
from app.services.api_key_service import ApiKeyPrincipal, get_authorized_project
from app.services.open_api.common import contains_literal, page_models


async def list_story[T: out.NovelEntity](
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    novel_id: int,
    kind: story_service.StoryKind,
    schema: type[T],
    *,
    page: int,
    page_size: int,
    q: str | None = None,
    category: WorldCategory | None = None,
    volume_no: int | None = None,
) -> out.Page[T]:
    await get_authorized_project(db, principal, novel_id)
    model = kind.model
    stmt = select(model).where(model.project_id == novel_id)
    if q:
        stmt = stmt.where(model.name.ilike(contains_literal(q), escape="\\"))
    if category is not None:
        stmt = stmt.where(model.category == category)
    if volume_no is not None:
        stmt = stmt.where(model.volume_no == volume_no)
    records, total = await page_models(db, stmt.order_by(*kind.order_by), page, page_size)
    return out.Page(items=[schema.model_validate(row) for row in records], total=total, page=page, page_size=page_size)


async def get_story[T: out.NovelEntity](
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    novel_id: int,
    item_id: int,
    kind: story_service.StoryKind,
    schema: type[T],
) -> T:
    await get_authorized_project(db, principal, novel_id)
    record = await story_service.get_owned(db, kind, principal.user_id, novel_id, item_id)
    return schema.model_validate(record)


async def list_plans(
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    novel_id: int,
    outline_id: int,
    *,
    page: int,
    page_size: int,
) -> out.Page[out.ChapterPlan]:
    await get_authorized_project(db, principal, novel_id)
    await plan_service.get_project_outline(db, principal.user_id, novel_id, outline_id)
    stmt = select(ChapterPlan).where(ChapterPlan.project_id == novel_id, ChapterPlan.outline_id == outline_id)
    records, total = await page_models(db, stmt.order_by(ChapterPlan.seq, ChapterPlan.id), page, page_size)
    plans = await plan_service.plan_outs(db, novel_id, records)
    return out.Page(
        items=[out.ChapterPlan.model_validate(p.model_dump()) for p in plans],
        total=total,
        page=page,
        page_size=page_size,
    )


async def _organization_outs(db: AsyncSession, records: list[Organization]) -> list[out.Organization]:
    serialized = await organization_service._outs(db, records)
    return [
        out.Organization.model_validate(
            {**item.model_dump(), "created_at": row.created_at, "updated_at": row.updated_at}
        )
        for row, item in zip(records, serialized, strict=True)
    ]


async def list_organizations(
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    novel_id: int,
    *,
    page: int,
    page_size: int,
    q: str | None = None,
) -> out.Page[out.Organization]:
    await get_authorized_project(db, principal, novel_id)
    stmt = select(Organization).where(Organization.project_id == novel_id)
    if q:
        stmt = stmt.where(Organization.name.ilike(contains_literal(q), escape="\\"))
    records, total = await page_models(db, stmt.order_by(Organization.sort, Organization.id), page, page_size)
    return out.Page(items=await _organization_outs(db, records), total=total, page=page, page_size=page_size)


async def get_organization(
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    novel_id: int,
    organization_id: int,
) -> out.Organization:
    await get_authorized_project(db, principal, novel_id)
    record = await organization_service.get_owned_org(db, principal.user_id, novel_id, organization_id)
    return (await _organization_outs(db, [record]))[0]


async def list_members(
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    novel_id: int,
    organization_id: int,
    *,
    page: int,
    page_size: int,
    status: MemberStatus | None = None,
) -> out.Page[out.OrganizationMember]:
    await get_authorized_project(db, principal, novel_id)
    await organization_service.get_owned_org(db, principal.user_id, novel_id, organization_id)
    stmt = select(OrganizationMember).where(OrganizationMember.organization_id == organization_id)
    if status is not None:
        stmt = stmt.where(OrganizationMember.status == status)
    records, total = await page_models(db, stmt.order_by(OrganizationMember.id), page, page_size)
    # 批量复用内部序列化（姓名 / 身份），只补开放契约的小说 ID 与成员行时间。
    serialized = (
        await organization_service._member_outs(
            db, select(OrganizationMember).where(OrganizationMember.id.in_([row.id for row in records]))
        )
        if records
        else []
    )
    by_id = {item.id: item for item in serialized}
    items = [
        out.OrganizationMember.model_validate(
            {
                **by_id[row.id].model_dump(),
                "novel_id": novel_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
        for row in records
    ]
    return out.Page(items=items, total=total, page=page, page_size=page_size)


async def list_relationships(
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    novel_id: int,
    *,
    page: int,
    page_size: int,
    character_id: int | None = None,
    status: RelationStatus | None = None,
    category: RelationCategory | None = None,
) -> out.Page[out.Relationship]:
    await get_authorized_project(db, principal, novel_id)
    stmt = select(Relationship).where(Relationship.project_id == novel_id)
    if character_id is not None:
        char = await db.scalar(
            select(Character.id).where(Character.id == character_id, Character.project_id == novel_id)
        )
        if char is None:
            raise NotFoundError("角色不存在")
        stmt = stmt.where(or_(Relationship.a_id == character_id, Relationship.b_id == character_id))
    if status is not None:
        stmt = stmt.where(Relationship.status == status)
    if category is not None:
        stmt = stmt.join(RelationshipType, Relationship.type_id == RelationshipType.id)
        stmt = stmt.where(RelationshipType.category == category)
    records, total = await page_models(db, stmt.order_by(Relationship.id), page, page_size)
    serialized = await relationship_service.to_outs(db, records, character_id)
    return out.Page(
        items=[out.Relationship.model_validate(item.model_dump()) for item in serialized],
        total=total,
        page=page,
        page_size=page_size,
    )


async def get_relationship(
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    novel_id: int,
    relationship_id: int,
) -> out.Relationship:
    await get_authorized_project(db, principal, novel_id)
    record = await relationship_service.get_owned_relationship(db, principal.user_id, novel_id, relationship_id)
    serialized = (await relationship_service.to_outs(db, [record]))[0]
    return out.Relationship.model_validate(serialized.model_dump())


async def list_relationship_types(
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    novel_id: int,
    *,
    page: int,
    page_size: int,
) -> out.Page[out.RelationshipType]:
    await get_authorized_project(db, principal, novel_id)
    stmt = select(RelationshipType).where(
        or_(RelationshipType.project_id.is_(None), RelationshipType.project_id == novel_id)
    )
    stmt = stmt.order_by(RelationshipType.project_id.is_not(None), RelationshipType.sort, RelationshipType.id)
    records, total = await page_models(db, stmt, page, page_size)
    items = [
        out.RelationshipType(
            id=row.id,
            novel_id=row.project_id,
            category=row.category,
            role_a=row.role_a,
            role_b=row.role_b,
            sort=row.sort,
            builtin=row.project_id is None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in records
    ]
    return out.Page(items=items, total=total, page=page, page_size=page_size)
