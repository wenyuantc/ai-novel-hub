"""组织成员时间线的业务投影；时间线是事实，成员表只保留最新视图。"""

from collections.abc import Mapping, Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models import Character, Organization, OrganizationMember, SettingTimeline
from app.services.setting_progression_resolver import EffectiveSnapshot, validate_baseline


async def find_membership_timeline(
    db: AsyncSession, project_id: int, org_id: int, character_id: int
) -> SettingTimeline | None:
    """按稳定主体读取；不存在时不创建数据。"""
    return await db.scalar(
        select(SettingTimeline).where(
            SettingTimeline.project_id == project_id,
            SettingTimeline.subject_kind == "membership",
            SettingTimeline.organization_id == org_id,
            SettingTimeline.character_id == character_id,
        )
    )


async def get_or_create_membership_timeline(
    db: AsyncSession,
    org: Organization,
    character: Character,
    *,
    baseline_status: str = "confirmed",
) -> SettingTimeline:
    """持有作品写锁时初始化稳定主体；成员行不存在时的基础状态为不存在。

    不在只读路径中调用。已有时间线保留原基线和校准状态；调用方拥有事务。
    """
    if org.project_id != character.project_id:
        raise BadRequestError("角色不存在或不属于该项目")
    if baseline_status not in ("confirmed", "legacy_unverified"):
        raise ValueError("基础设定的校准状态不合法")
    await db.flush()
    timeline = await find_membership_timeline(db, org.project_id, org.id, character.id)
    if timeline is not None:
        return timeline
    member = await db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.character_id == character.id,
        )
    )
    baseline = (
        {"exists": False}
        if member is None
        else {"exists": True, "position": member.position, "status": member.status, "note": member.note}
    )
    timeline = SettingTimeline(
        project_id=org.project_id,
        subject_kind="membership",
        organization_id=org.id,
        character_id=character.id,
        baseline=baseline,
        baseline_status=baseline_status,
        legacy_compat_allowed=baseline_status == "legacy_unverified",
    )
    db.add(timeline)
    await db.flush()
    return timeline


async def materialize_memberships(
    db: AsyncSession,
    project_id: int,
    timelines: Sequence[SettingTimeline],
    snapshots: Mapping[int, EffectiveSnapshot],
) -> None:
    """把一批成员最新快照同步到现有成员表；不提交、不写剧情事件。

    主体为 ``(character_id, organization_id)``，不能用会因离开而删除的成员行 ID。
    调用方必须持有作品写屏障，并在事实变更后调用统一求值器取得 ``snapshots``。
    """
    if not timelines:
        return
    if any(
        timeline.subject_kind != "membership"
        or timeline.project_id != project_id
        or timeline.character_id is None
        or timeline.organization_id is None
        or timeline.id not in snapshots
        for timeline in timelines
    ):
        raise BadRequestError("成员时间线不属于该作品或缺少有效快照")

    character_ids = {cast(int, timeline.character_id) for timeline in timelines}
    organization_ids = {cast(int, timeline.organization_id) for timeline in timelines}
    valid_characters = set(
        (
            await db.scalars(
                select(Character.id).where(Character.project_id == project_id, Character.id.in_(character_ids))
            )
        ).all()
    )
    valid_organizations = set(
        (
            await db.scalars(
                select(Organization.id).where(
                    Organization.project_id == project_id, Organization.id.in_(organization_ids)
                )
            )
        ).all()
    )
    if valid_characters != character_ids or valid_organizations != organization_ids:
        raise BadRequestError("成员关联的角色或组织不存在或不属于该作品")

    # 先校验整批，避免在同一 session 内产生部分业务对象的修改。
    for timeline in timelines:
        try:
            validate_baseline("membership", snapshots[timeline.id].fields)
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc

    await db.flush()
    current = {
        (member.organization_id, member.character_id): member
        for member in (
            await db.scalars(
                select(OrganizationMember)
                .where(
                    OrganizationMember.character_id.in_(character_ids),
                    OrganizationMember.organization_id.in_(organization_ids),
                )
                .execution_options(populate_existing=True)
            )
        ).all()
    }
    for timeline in timelines:
        character_id = cast(int, timeline.character_id)
        organization_id = cast(int, timeline.organization_id)
        member = current.get((organization_id, character_id))
        fields = snapshots[timeline.id].fields
        if fields["exists"] is False:
            if member is not None:
                await db.delete(member)
            continue

        if member is None:
            member = OrganizationMember(organization_id=organization_id, character_id=character_id)
            db.add(member)
        member.position = cast(str, fields["position"])
        member.status = cast(str, fields["status"])
        member.note = cast(str, fields["note"])
    await db.flush()
