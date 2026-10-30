"""组织与成员。组织可嵌套（parent_id），设置上级时沿上级链检测循环；成员按 (组织, 角色) 唯一。

apply_membership 是章节分析回写成员关系的入口：返回写入前的快照（{position, status} 或 None），
调用方（writeback_service）据此记录前后值并支持撤销；不 commit。
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Character, Organization, OrganizationMember
from app.models.entities import MEMBER_STATUSES
from app.schemas.organization import (
    MemberCreate,
    MemberOut,
    MemberUpdate,
    OrganizationCreate,
    OrganizationOut,
    OrganizationUpdate,
    OrganizationWithMembers,
)
from app.services import story_service
from app.services.project_service import get_manageable_project

MAX_DEPTH = 50
NAME_CONFLICT = "同名组织已存在"
MEMBER_CONFLICT = "该角色已是该组织成员"


async def get_owned_org(db: AsyncSession, user_id: int, project_id: int, org_id: int) -> Organization:
    await get_manageable_project(db, user_id, project_id)
    org = await db.scalar(select(Organization).where(Organization.id == org_id, Organization.project_id == project_id))
    if org is None:
        raise NotFoundError("组织不存在")
    return org


async def _member_counts(db: AsyncSession, org_ids: list[int]) -> dict[int, int]:
    if not org_ids:
        return {}
    rows = await db.execute(
        select(OrganizationMember.organization_id, func.count(OrganizationMember.id))
        .where(OrganizationMember.organization_id.in_(org_ids))
        .group_by(OrganizationMember.organization_id)
    )
    return {oid: int(n) for oid, n in rows.all()}


async def _outs(db: AsyncSession, orgs: list[Organization]) -> list[OrganizationOut]:
    counts = await _member_counts(db, [o.id for o in orgs])
    return [OrganizationOut.model_validate(o).model_copy(update={"member_count": counts.get(o.id, 0)}) for o in orgs]


async def check_parent(db: AsyncSession, project_id: int, org_id: int | None, parent_id: int | None) -> None:
    """上级须属于同一项目；沿上级链向上查（最多 50 层），遇到自身即判为循环。"""
    if parent_id is None:
        return
    if org_id is not None and parent_id == org_id:
        raise BadRequestError("不能把组织自身或其下级组织设为上级")
    parents = dict(
        (
            await db.execute(
                select(Organization.id, Organization.parent_id).where(Organization.project_id == project_id)
            )
        ).all()
    )
    if parent_id not in parents:
        raise BadRequestError("上级组织不存在或不属于该项目")
    current: int | None = parent_id
    for _ in range(MAX_DEPTH):
        current = parents.get(current) if current is not None else None
        if current is None:
            return
        if current == org_id:
            raise BadRequestError("不能把组织自身或其下级组织设为上级")
    raise BadRequestError(f"组织层级过深（最多 {MAX_DEPTH} 层）")


async def _name_taken(db: AsyncSession, project_id: int, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(Organization.id).where(Organization.project_id == project_id, Organization.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Organization.id != exclude_id)
    return await db.scalar(stmt) is not None


async def _commit(db: AsyncSession, conflict: str) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(conflict) from exc


async def new_org(db: AsyncSession, project_id: int, data: OrganizationCreate) -> Organization:
    """校验名称与上级后新建并 flush（不 commit）。"""
    values = data.model_dump()
    values["name"] = values["name"].strip()
    if not values["name"]:
        raise BadRequestError("组织名称不能为空")
    if await _name_taken(db, project_id, values["name"]):
        raise ConflictError(NAME_CONFLICT)
    await check_parent(db, project_id, None, values["parent_id"])
    if values.get("sort") is None:
        values["sort"] = await story_service.next_sort(db, Organization, project_id)
    org = Organization(project_id=project_id, **values)
    db.add(org)
    await db.flush()
    return org


async def list_orgs(db: AsyncSession, user_id: int, project_id: int) -> list[OrganizationOut]:
    await get_manageable_project(db, user_id, project_id)
    orgs = list(
        await db.scalars(
            select(Organization)
            .where(Organization.project_id == project_id)
            .order_by(Organization.sort, Organization.id)
        )
    )
    return await _outs(db, orgs)


async def get_org(db: AsyncSession, user_id: int, project_id: int, org_id: int) -> OrganizationOut:
    return (await _outs(db, [await get_owned_org(db, user_id, project_id, org_id)]))[0]


async def create_org(db: AsyncSession, user_id: int, project_id: int, data: OrganizationCreate) -> OrganizationOut:
    await get_manageable_project(db, user_id, project_id)
    org = await new_org(db, project_id, data)
    await _commit(db, NAME_CONFLICT)
    await db.refresh(org)
    return (await _outs(db, [org]))[0]


async def update_org(
    db: AsyncSession, user_id: int, project_id: int, org_id: int, data: OrganizationUpdate
) -> OrganizationOut:
    org = await get_owned_org(db, user_id, project_id, org_id)
    fields = data.model_fields_set
    values: dict[str, Any] = {}
    for f in ("name", "org_type", "purpose", "location", "description", "sort"):
        v = getattr(data, f)
        if f in fields and v is not None:
            values[f] = v.strip() if f == "name" else v
    if "name" in values:
        if not values["name"]:
            raise BadRequestError("组织名称不能为空")
        if await _name_taken(db, project_id, values["name"], exclude_id=org.id):
            raise ConflictError(NAME_CONFLICT)
    # 可空字段：显式传 null = 清空
    if "power_level" in fields:
        values["power_level"] = data.power_level
    if "parent_id" in fields:
        await check_parent(db, project_id, org.id, data.parent_id)
        values["parent_id"] = data.parent_id
    for f, v in values.items():
        setattr(org, f, v)
    await _commit(db, NAME_CONFLICT)
    await db.refresh(org)
    return (await _outs(db, [org]))[0]


async def delete_org(db: AsyncSession, user_id: int, project_id: int, org_id: int) -> None:
    org = await get_owned_org(db, user_id, project_id, org_id)
    await db.delete(org)
    await db.commit()


# ---------------- 成员 ----------------


async def _member_outs(db: AsyncSession, stmt) -> list[MemberOut]:
    rows = await db.execute(
        stmt.add_columns(Organization.name, Character.name)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .join(Character, Character.id == OrganizationMember.character_id)
        .order_by(OrganizationMember.id)
    )
    return [
        MemberOut(
            id=m.id,
            organization_id=m.organization_id,
            organization_name=org_name,
            character_id=m.character_id,
            character_name=char_name,
            position=m.position,
            status=m.status,
            note=m.note,
        )
        for m, org_name, char_name in rows.all()
    ]


async def _member_out(db: AsyncSession, member_id: int) -> MemberOut:
    return (await _member_outs(db, select(OrganizationMember).where(OrganizationMember.id == member_id)))[0]


async def new_member(db: AsyncSession, org: Organization, data: MemberCreate) -> OrganizationMember:
    """校验角色同项目、成员唯一后新建并 flush（不 commit）。"""
    char_id = await db.scalar(
        select(Character.id).where(Character.id == data.character_id, Character.project_id == org.project_id)
    )
    if char_id is None:
        raise BadRequestError("角色不存在或不属于该项目")
    dup = await db.scalar(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == org.id, OrganizationMember.character_id == data.character_id
        )
    )
    if dup is not None:
        raise ConflictError(MEMBER_CONFLICT)
    member = OrganizationMember(organization_id=org.id, **data.model_dump())
    db.add(member)
    await db.flush()
    return member


async def list_members(db: AsyncSession, user_id: int, project_id: int, org_id: int) -> list[MemberOut]:
    org = await get_owned_org(db, user_id, project_id, org_id)
    return await _member_outs(db, select(OrganizationMember).where(OrganizationMember.organization_id == org.id))


async def add_member(db: AsyncSession, user_id: int, project_id: int, org_id: int, data: MemberCreate) -> MemberOut:
    org = await get_owned_org(db, user_id, project_id, org_id)
    member = await new_member(db, org, data)
    await _commit(db, MEMBER_CONFLICT)
    return await _member_out(db, member.id)


async def _get_member(db: AsyncSession, org: Organization, member_id: int) -> OrganizationMember:
    member = await db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.id == member_id, OrganizationMember.organization_id == org.id
        )
    )
    if member is None:
        raise NotFoundError("成员不存在")
    return member


async def update_member(
    db: AsyncSession, user_id: int, project_id: int, org_id: int, member_id: int, data: MemberUpdate
) -> MemberOut:
    org = await get_owned_org(db, user_id, project_id, org_id)
    member = await _get_member(db, org, member_id)
    for f, v in data.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(member, f, v)
    await db.commit()
    return await _member_out(db, member.id)


async def remove_member(db: AsyncSession, user_id: int, project_id: int, org_id: int, member_id: int) -> None:
    org = await get_owned_org(db, user_id, project_id, org_id)
    member = await _get_member(db, org, member_id)
    await db.delete(member)
    await db.commit()


async def character_memberships(db: AsyncSession, user_id: int, project_id: int, character_id: int) -> list[MemberOut]:
    await get_manageable_project(db, user_id, project_id)
    exists = await db.scalar(
        select(Character.id).where(Character.id == character_id, Character.project_id == project_id)
    )
    if exists is None:
        raise NotFoundError("角色不存在")
    return await _member_outs(db, select(OrganizationMember).where(OrganizationMember.character_id == character_id))


def membership_snapshot(member: OrganizationMember | None) -> dict[str, str] | None:
    return None if member is None else {"position": member.position, "status": member.status}


async def find_membership(db: AsyncSession, org_id: int, character_id: int) -> OrganizationMember | None:
    return await db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.character_id == character_id
        )
    )


async def apply_membership(
    db: AsyncSession, org: Organization, character: Character, values: dict[str, Any] | None
) -> dict[str, str] | None:
    """把成员关系写成 values（{position?, status?}；None = 移除成员）。返回写入前的快照。不 commit。

    校验：组织与角色同项目（400）、职位 ≤50、状态合法。未是成员时新建（默认 active）。
    """
    if org.project_id != character.project_id:
        raise BadRequestError("角色不存在或不属于该项目")
    member = await find_membership(db, org.id, character.id)
    before = membership_snapshot(member)
    if values is None:
        if member is not None:
            await db.delete(member)
            await db.flush()
        return before
    default_position = member.position if member else ""
    position = str(values["position"] if values.get("position") is not None else default_position)
    status = str(values.get("status") or (member.status if member else "active"))
    if len(position) > 50:
        raise BadRequestError("职位最多 50 个字符")
    if status not in MEMBER_STATUSES:
        raise BadRequestError("成员状态不合法")
    if member is None:
        member = OrganizationMember(organization_id=org.id, character_id=character.id, position=position, status=status)
        db.add(member)
    else:
        member.position, member.status = position, status
    await db.flush()
    return before


async def memberships_of(db: AsyncSession, character_ids: list[int]) -> list[tuple[OrganizationMember, Organization]]:
    """角色所属组织（上下文组装用），按组织排序。"""
    if not character_ids:
        return []
    rows = await db.execute(
        select(OrganizationMember, Organization)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(OrganizationMember.character_id.in_(character_ids))
        .order_by(Organization.sort, Organization.id, OrganizationMember.id)
    )
    return [(m, o) for m, o in rows.all()]


async def create_with_members(
    db: AsyncSession, user_id: int, project_id: int, data: OrganizationWithMembers
) -> dict[str, Any]:
    """AI 生成组织预览确认后一次保存：组织 + 勾选的成员（单事务）。"""
    await get_manageable_project(db, user_id, project_id)
    org = await new_org(db, project_id, data.organization)
    members = [await new_member(db, org, m) for m in data.members]
    await _commit(db, NAME_CONFLICT)
    await db.refresh(org)
    return {
        "organization": (await _outs(db, [org]))[0],
        "members": [await _member_out(db, m.id) for m in members],
    }
