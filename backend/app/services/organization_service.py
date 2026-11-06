"""组织与成员。组织可嵌套（parent_id），成员按 (组织, 角色) 唯一。"""

from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, Character, Organization, OrganizationMember, SettingProgression, SettingTimeline
from app.schemas.organization import (
    MemberCreate,
    MemberOut,
    MemberUpdate,
    OrganizationCreate,
    OrganizationOut,
    OrganizationUpdate,
    OrganizationWithMembers,
    validate_member_anchor,
)
from app.schemas.progression import AnchorKind, BaselineUpdate, ProgressionCreate, StoryPosition
from app.schemas.setting_fields import EntityRef
from app.services import (
    membership_progression_service,
    setting_ai_visibility,
    setting_catalog_service,
    setting_field_service,
    setting_progression_service,
    story_service,
)
from app.services.project_service import get_manageable_project

MAX_DEPTH = 50
NAME_CONFLICT = "同名组织已存在"
MEMBER_CONFLICT = "该角色已是该组织成员"
MEMBER_FIELDS = ("position", "status", "note")
INTENT_REQUIRED = "该成员已有剧情变化，请选择修正基础设定或指定生效章节"


def _intent_conflict() -> ConflictError:
    return ConflictError(INTENT_REQUIRED, extra={"code": "progression_intent_required"})


def _stale_revision() -> ConflictError:
    return ConflictError("设定已被其他操作修改，请刷新后重试", extra={"code": "stale_revision"})


def _require_revision(timeline: SettingTimeline, expected_revision: int | None) -> int:
    if expected_revision is None:
        raise ConflictError("请刷新成员时间线后重试", extra={"code": "progression_revision_required"})
    if timeline.revision != expected_revision:
        raise _stale_revision()
    return expected_revision


async def _member_position(
    db: AsyncSession,
    project_id: int,
    *,
    anchor_kind: AnchorKind | None,
    chapter_id: int | None,
    chapter_no: int | None,
    boundary: Literal["start", "end"],
) -> tuple[str, int | None, int | None, StoryPosition]:
    try:
        validate_member_anchor(anchor_kind, chapter_id, chapter_no)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if (chapter_id is None) == (chapter_no is None):
        raise BadRequestError("请指定一个生效章节")
    if anchor_kind == "planned":
        assert chapter_no is not None
        return "planned", None, chapter_no, StoryPosition(chapter_no=chapter_no, boundary=boundary)
    if chapter_id is not None:
        chapter = await db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id))
        if chapter is None:
            raise BadRequestError("章节不存在或不属于该作品")
        position = StoryPosition(chapter_id=chapter.id, chapter_no=chapter.chapter_no, boundary=boundary)
        return "chapter", chapter.id, None, position
    matches = list(
        (
            await db.scalars(
                select(Chapter)
                .where(Chapter.project_id == project_id, Chapter.chapter_no == chapter_no)
                .order_by(Chapter.id)
                .limit(2)
            )
        ).all()
    )
    if len(matches) > 1:
        raise ConflictError("存在多个同编号章节，请明确选择章节", extra={"code": "ambiguous_chapter"})
    if matches:
        chapter = matches[0]
        position = StoryPosition(chapter_id=chapter.id, chapter_no=chapter.chapter_no, boundary=boundary)
        return "chapter", chapter.id, None, position
    assert chapter_no is not None
    return "planned", None, chapter_no, StoryPosition(chapter_no=chapter_no, boundary=boundary)


async def _has_member_history(
    db: AsyncSession,
    timeline: SettingTimeline,
    fields: set[str] | None = None,
    *,
    include_inactive: bool = False,
) -> bool:
    stmt = select(SettingProgression.id).where(SettingProgression.timeline_id == timeline.id)
    if not include_inactive:
        stmt = stmt.where(SettingProgression.active.is_(True))
    if fields is not None:
        stmt = stmt.where(SettingProgression.field_key.in_(fields | {"presence"}))
    return await db.scalar(stmt.limit(1)) is not None


async def _materialize_member(db: AsyncSession, timeline: SettingTimeline) -> None:
    try:
        await setting_progression_service.materialize_latest(db, timeline)
    except ValueError as exc:
        raise ConflictError(
            "成员变化与已有剧情顺序冲突，请检查加入和退出的位置",
            extra={"code": "progression_lifecycle_conflict"},
        ) from exc


async def _record_member_change(
    db: AsyncSession,
    timeline: SettingTimeline,
    *,
    field_key: str,
    value: Any,
    anchor_kind: str,
    chapter_id: int | None,
    chapter_no: int | None,
    boundary: Literal["start", "end"],
) -> None:
    await setting_progression_service.apply_progression(
        db,
        timeline,
        ProgressionCreate(
            expected_revision=timeline.revision,
            field_key=field_key,
            operation="replace",
            value=value,
            anchor_kind=anchor_kind,
            chapter_id=chapter_id,
            chapter_no=chapter_no,
            boundary=boundary,
        ),
        materialize=False,
    )


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
    await setting_catalog_service.ensure_entry_timeline(db, project_id, "organization", org.id)
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
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    org = await new_org(db, project_id, data)
    await _commit(db, NAME_CONFLICT)
    await db.refresh(org)
    return (await _outs(db, [org]))[0]


async def update_org(
    db: AsyncSession, user_id: int, project_id: int, org_id: int, data: OrganizationUpdate
) -> OrganizationOut:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
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
    from app.services.planning_lifecycle import bump_revision

    await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id)
    org = await get_owned_org(db, user_id, project_id, org_id)
    await setting_field_service.cleanup_unreferenced_handles(
        db, project_id, [EntityRef(kind="organization", id=org.id)]
    )
    await db.delete(org)
    await bump_revision(db, project_id)
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
    """创建基础成员或记录按章加入；调用方持项目锁，且本函数不提交。"""
    character = await db.scalar(
        select(Character).where(Character.id == data.character_id, Character.project_id == org.project_id)
    )
    if character is None:
        raise BadRequestError("角色不存在或不属于该项目")
    timeline = await membership_progression_service.find_membership_timeline(
        db, org.project_id, org.id, character.id
    )
    if (timeline is None and data.expected_revision not in (None, 0)) or (
        timeline is not None and data.expected_revision is not None and data.expected_revision != timeline.revision
    ):
        # 0 表示读到主体不存在；正版本则要求原时间线仍然存在。
        raise _stale_revision()
    dup = await db.scalar(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == org.id, OrganizationMember.character_id == data.character_id
        )
    )
    if dup is not None:
        raise ConflictError(MEMBER_CONFLICT)
    has_position = data.chapter_id is not None or data.chapter_no is not None
    if not has_position and timeline is None:
        member = OrganizationMember(
            organization_id=org.id,
            character_id=character.id,
            position=data.position,
            status=data.status,
            note=data.note,
        )
        db.add(member)
        await db.flush()
        await membership_progression_service.get_or_create_membership_timeline(db, org, character)
        return member

    existing_timeline = timeline is not None
    if timeline is None:
        timeline = await membership_progression_service.get_or_create_membership_timeline(db, org, character)
    if existing_timeline and (has_position or data.edit_mode == "baseline"):
        _require_revision(timeline, data.expected_revision)
    elif existing_timeline and data.expected_revision is not None and data.expected_revision != timeline.revision:
        raise _stale_revision()

    state = {"position": data.position, "status": data.status, "note": data.note}
    if has_position:
        anchor_kind, chapter_id, chapter_no, _ = await _member_position(
            db,
            org.project_id,
            anchor_kind=data.anchor_kind,
            chapter_id=data.chapter_id,
            chapter_no=data.chapter_no,
            boundary=data.boundary,
        )
        await _record_member_change(
            db,
            timeline,
            field_key="presence",
            value={"exists": True, "state": state},
            anchor_kind=anchor_kind,
            chapter_id=chapter_id,
            chapter_no=chapter_no,
            boundary=data.boundary,
        )
        await _materialize_member(db, timeline)
    else:
        if await _has_member_history(db, timeline):
            raise _intent_conflict()
        await setting_progression_service.apply_baseline(
            db,
            timeline,
            BaselineUpdate(expected_revision=timeline.revision, changes={"exists": True, **state}),
            materialize=False,
        )
        await _materialize_member(db, timeline)
    member = await find_membership(db, org.id, character.id)
    if member is None:
        raise ConflictError(
            "该角色在最新剧情中已退出，请在设定变化中维护早期加入",
            extra={"code": "progression_currently_absent"},
        )
    return member


async def list_members(db: AsyncSession, user_id: int, project_id: int, org_id: int) -> list[MemberOut]:
    org = await get_owned_org(db, user_id, project_id, org_id)
    return await _member_outs(db, select(OrganizationMember).where(OrganizationMember.organization_id == org.id))


async def add_member(db: AsyncSession, user_id: int, project_id: int, org_id: int, data: MemberCreate) -> MemberOut:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
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
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    org = await get_owned_org(db, user_id, project_id, org_id)
    member = await _get_member(db, org, member_id)
    character = await db.scalar(select(Character).where(Character.id == member.character_id))
    assert character is not None
    supplied = {
        field: getattr(data, field)
        for field in MEMBER_FIELDS
        if field in data.model_fields_set and getattr(data, field) is not None
    }
    has_position = data.chapter_id is not None or data.chapter_no is not None
    if not supplied and data.edit_mode is None and not has_position and data.expected_revision is None:
        return await _member_out(db, member.id)
    timeline = await membership_progression_service.get_or_create_membership_timeline(
        db, org, character, baseline_status="legacy_unverified"
    )
    if has_position or data.edit_mode == "baseline":
        _require_revision(timeline, data.expected_revision)
    elif data.expected_revision is not None and data.expected_revision != timeline.revision:
        raise _stale_revision()

    if has_position:
        anchor_kind, chapter_id, chapter_no, position = await _member_position(
            db,
            project_id,
            anchor_kind=data.anchor_kind,
            chapter_id=data.chapter_id,
            chapter_no=data.chapter_no,
            boundary=data.boundary,
        )
        try:
            snapshot = (await setting_progression_service.resolve_timelines(db, project_id, [timeline], position))[
                timeline.id
            ]
        except ValueError as exc:
            raise ConflictError(
                "成员变化与已有剧情顺序冲突，请检查加入和退出的位置",
                extra={"code": "progression_lifecycle_conflict"},
            ) from exc
        if snapshot.fields.get("exists") is not True:
            raise ConflictError("角色在该章节尚非该组织成员", extra={"code": "progression_lifecycle_conflict"})
        for field, value in supplied.items():
            if snapshot.fields.get(field) != value:
                await _record_member_change(
                    db,
                    timeline,
                    field_key=field,
                    value=value,
                    anchor_kind=anchor_kind,
                    chapter_id=chapter_id,
                    chapter_no=chapter_no,
                    boundary=data.boundary,
                )
        await _materialize_member(db, timeline)
    else:
        if data.edit_mode == "baseline":
            if timeline.baseline.get("exists") is not True:
                raise ConflictError("该角色不是基础成员，请先修正成员基础归属")
            changes = {field: value for field, value in supplied.items() if timeline.baseline.get(field) != value}
        else:
            changes = {field: value for field, value in supplied.items() if getattr(member, field) != value}
            if changes and await _has_member_history(db, timeline, set(changes)):
                raise _intent_conflict()
        if changes:
            await setting_progression_service.apply_baseline(
                db,
                timeline,
                BaselineUpdate(expected_revision=timeline.revision, changes=changes),
                materialize=False,
            )
            await _materialize_member(db, timeline)
    await db.commit()
    return await _member_out(db, member.id)


async def remove_member(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    org_id: int,
    member_id: int,
    *,
    edit_mode: Literal["progression", "delete"] | None = None,
    expected_revision: int | None = None,
    anchor_kind: AnchorKind | None = None,
    chapter_id: int | None = None,
    chapter_no: int | None = None,
    boundary: Literal["start", "end"] = "start",
) -> None:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    org = await get_owned_org(db, user_id, project_id, org_id)
    member = await _get_member(db, org, member_id)
    character = await db.scalar(select(Character).where(Character.id == member.character_id))
    assert character is not None
    timeline = await membership_progression_service.find_membership_timeline(db, project_id, org.id, character.id)
    try:
        validate_member_anchor(anchor_kind, chapter_id, chapter_no)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    has_position = chapter_id is not None or chapter_no is not None
    if edit_mode == "delete" and has_position:
        raise BadRequestError("删除成员资料不能指定生效章节")
    if edit_mode == "progression" and not has_position:
        raise BadRequestError("记录剧情退出必须指定生效章节")
    if boundary != "start" and not has_position:
        raise BadRequestError("指定章节后才能选择生效边界")

    if has_position:
        if timeline is None:
            timeline = await membership_progression_service.get_or_create_membership_timeline(
                db, org, character, baseline_status="legacy_unverified"
            )
        _require_revision(timeline, expected_revision)
        resolved_kind, anchor_id, anchor_no, position = await _member_position(
            db, project_id, anchor_kind=anchor_kind, chapter_id=chapter_id, chapter_no=chapter_no, boundary=boundary
        )
        try:
            snapshot = (await setting_progression_service.resolve_timelines(db, project_id, [timeline], position))[
                timeline.id
            ]
        except ValueError as exc:
            raise ConflictError(
                "成员变化与已有剧情顺序冲突，请检查加入和退出的位置",
                extra={"code": "progression_lifecycle_conflict"},
            ) from exc
        if snapshot.fields.get("exists") is not True:
            raise ConflictError("角色在该章节尚非该组织成员", extra={"code": "progression_lifecycle_conflict"})
        await _record_member_change(
            db,
            timeline,
            field_key="presence",
            value={"exists": False},
            anchor_kind=resolved_kind,
            chapter_id=anchor_id,
            chapter_no=anchor_no,
            boundary=boundary,
        )
        await _materialize_member(db, timeline)
    else:
        if timeline is not None:
            if edit_mode == "delete":
                _require_revision(timeline, expected_revision)
            elif await _has_member_history(db, timeline, include_inactive=True):
                raise _intent_conflict()
            elif expected_revision is not None and expected_revision != timeline.revision:
                raise _stale_revision()
            await db.delete(timeline)
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


async def delete_membership_record(
    db: AsyncSession, user_id: int, project_id: int, org_id: int, character_id: int, expected_revision: int
) -> None:
    """明确删除稳定成员主体的整份资料；即使当前角色已经退出也可操作。"""
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    org = await get_owned_org(db, user_id, project_id, org_id)
    character = await db.scalar(
        select(Character).where(Character.id == character_id, Character.project_id == project_id)
    )
    if character is None:
        raise NotFoundError("角色不存在")
    timeline = await membership_progression_service.find_membership_timeline(db, project_id, org.id, character.id)
    if timeline is None:
        raise NotFoundError("成员资料不存在")
    _require_revision(timeline, expected_revision)
    member = await find_membership(db, org.id, character.id)
    await db.delete(timeline)
    if member is not None:
        await db.delete(member)
    await db.commit()


def membership_snapshot(member: OrganizationMember | None) -> dict[str, str] | None:
    return None if member is None else {"position": member.position, "status": member.status}


async def find_membership(db: AsyncSession, org_id: int, character_id: int) -> OrganizationMember | None:
    return await db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.character_id == character_id
        )
    )


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
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    org = await new_org(db, project_id, data.organization)
    members = [await new_member(db, org, m) for m in data.members]
    await _commit(db, NAME_CONFLICT)
    await db.refresh(org)
    return {
        "organization": (await _outs(db, [org]))[0],
        "members": [await _member_out(db, m.id) for m in members],
    }
