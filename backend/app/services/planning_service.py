"""章级规划 CRUD：父项目屏障、整批 CAS 与稳定来源身份。"""

from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, ChapterCharacter, ChapterPlan, Character, Outline, Project, SettingTimeline
from app.models.planning import (
    PlanningTag,
    PlanningUnit,
    PlanningUnitTag,
    Subplot,
    SubplotCrossing,
    SubplotCrossingParticipant,
    SubplotNode,
)
from app.schemas import planning as s
from app.services import planning_queries as query
from app.services.audit_writer import AuditOperation
from app.services.project_service import get_manageable_project


def _stale(revision: int) -> ConflictError:
    return ConflictError(
        "规划已被修改，请刷新后重试；当前编辑内容可继续保留",
        extra={
            "code": "stale_planning_revision",
            "current_revision": revision,
        },
    )


@asynccontextmanager
async def _editing(db: AsyncSession, user_id: int, project_id: int, expected_revision: int):
    async with AuditOperation(db, user_id, "planning.update", "project", project_id, project_id=project_id) as audit:
        project = await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
        current = await query.revision(db, project_id)
        if current != expected_revision:
            raise _stale(current)
        audit.target(project)
        audit.summary.changed_fields = ["references"]
        yield project
        await db.flush()
        result = await db.execute(
            update(Project)
            .where(Project.id == project_id, Project.planning_revision == expected_revision)
            .values(planning_revision=Project.planning_revision + 1)
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            raise _stale(await query.revision(db, project_id))
        await audit.commit()


async def _snapshot(db: AsyncSession, user_id: int, project_id: int, reader):
    with db.no_autoflush:
        await get_manageable_project(db, user_id, project_id)
        for _ in range(2):
            before = await query.revision(db, project_id)
            result = await reader()
            after = await query.revision(db, project_id)
            if before == after:
                return before, result
    raise _stale(after)


async def _unit(db: AsyncSession, project_id: int, unit_id: int) -> PlanningUnit:
    item = await db.scalar(
        select(PlanningUnit).where(
            PlanningUnit.id == unit_id, PlanningUnit.project_id == project_id, PlanningUnit.retired.is_(False)
        )
    )
    if item is None:
        raise NotFoundError("规划单元不存在")
    return item


@dataclass
class SourceTarget:
    source: s.PlanningSource
    chapter: Chapter | None = None
    plan: ChapterPlan | None = None
    unit: PlanningUnit | None = None


async def resolve_source(db: AsyncSession, project_id: int, source: s.PlanningSource) -> SourceTarget:
    """只读解析与别名规范化；不得按标题、章号或旧整数 ID 猜测重连。"""
    if source.kind == "detached":
        unit = await _unit(db, project_id, source.id)
        if unit.chapter_id is not None or unit.plan_id is not None:
            raise ConflictError("规划已经重新关联，请刷新", extra={"code": "planning_source_changed"})
        return SourceTarget(source, unit=unit)
    chapter = None
    plan = None
    if source.kind == "plan":
        plan = await db.scalar(
            select(ChapterPlan).join(Outline).where(ChapterPlan.id == source.id, Outline.project_id == project_id)
        )
        if plan is None:
            raise NotFoundError("细纲不存在")
        if plan.chapter_id is not None:
            chapter = await db.scalar(
                select(Chapter).where(
                    Chapter.id == plan.chapter_id, Chapter.project_id == project_id, Chapter.plan_id == plan.id
                )
            )
            if chapter is None:
                raise ConflictError("细纲与章节绑定已改变，请刷新", extra={"code": "planning_source_changed"})
    else:
        chapter = await db.scalar(select(Chapter).where(Chapter.id == source.id, Chapter.project_id == project_id))
        if chapter is None:
            raise NotFoundError("章节不存在")
        if chapter.plan_id is not None:
            plan = await db.scalar(
                select(ChapterPlan)
                .join(Outline)
                .where(
                    ChapterPlan.id == chapter.plan_id,
                    ChapterPlan.chapter_id == chapter.id,
                    Outline.project_id == project_id,
                )
            )
    if chapter is not None:
        unit = await db.scalar(
            select(PlanningUnit).where(
                PlanningUnit.project_id == project_id,
                PlanningUnit.chapter_id == chapter.id,
                PlanningUnit.retired.is_(False),
            )
        )
        return SourceTarget(s.PlanningSource(kind="chapter", id=chapter.id), chapter, plan, unit)
    unit = await db.scalar(
        select(PlanningUnit).where(
            PlanningUnit.project_id == project_id, PlanningUnit.plan_id == plan.id, PlanningUnit.retired.is_(False)
        )
    )
    if unit is not None and unit.chapter_id is not None:
        raise ConflictError("规划来源绑定不一致，请刷新", extra={"code": "planning_source_changed"})
    return SourceTarget(source, plan=plan, unit=unit)


async def _materialize(db: AsyncSession, project_id: int, target: SourceTarget) -> PlanningUnit:
    if target.unit is None:
        chapter, plan = target.chapter, target.plan
        outline_id = chapter.outline_id if chapter else plan.outline_id
        outline = await db.scalar(select(Outline).where(Outline.id == outline_id, Outline.project_id == project_id))
        target.unit = PlanningUnit(
            project_id=project_id,
            chapter_id=chapter.id if chapter else None,
            plan_id=plan.id if plan else None,
            last_source_label=chapter.title if chapter else plan.title,
            last_volume_label=outline.volume_title if outline else "",
        )
        db.add(target.unit)
        await db.flush()
    return target.unit


async def _validate_ids(db: AsyncSession, project_id: int, model, ids: list[int], *, relationship=False):
    if not ids:
        return
    statement = select(model.id).where(model.project_id == project_id, model.id.in_(ids))
    if relationship:
        statement = statement.where(SettingTimeline.subject_kind == "relationship")
    found = set(await db.scalars(statement))
    if found != set(ids):
        raise BadRequestError("部分关联条目不存在或不属于本作品")


async def _character_ids(db: AsyncSession, target: SourceTarget) -> list[int]:
    if target.chapter:
        return list(
            await db.scalars(
                select(ChapterCharacter.character_id).where(ChapterCharacter.chapter_id == target.chapter.id)
            )
        )
    if target.plan:
        return list(target.plan.character_ids or [])
    return list(target.unit.detached_character_ids or [])


async def _set_characters(db: AsyncSession, target: SourceTarget, ids: list[int]):
    if target.chapter:
        from app.services.planning_lifecycle import sync_cast

        await sync_cast(db, target.chapter, ids)
    elif target.plan:
        target.plan.character_ids = ids
    else:
        target.unit.detached_character_ids = ids


def _apply(current: list[int], op: str, values: list[int]) -> list[int]:
    if op == "clear":
        return []
    if op == "replace":
        return sorted(set(values))
    if op == "remove":
        return sorted(set(current) - set(values))
    return sorted(set(current) | set(values))


async def _set_join(db: AsyncSession, model, column: str, unit_id: int, values: list[int]):
    await db.execute(delete(model).where(model.unit_id == unit_id))
    db.add_all(model(unit_id=unit_id, **{column: identity}) for identity in sorted(set(values)))


async def _join_ids(db: AsyncSession, model, column: str, unit_id: int) -> list[int]:
    return list(await db.scalars(select(getattr(model, column)).where(model.unit_id == unit_id)))


async def batch_update(db: AsyncSession, user_id: int, project_id: int, body: s.PlanningBatch) -> s.PlanningMutation:
    async with _editing(db, user_id, project_id, body.expected_revision):
        targets = [await resolve_source(db, project_id, source) for source in body.targets]
        if len({query.source_key(target.source) for target in targets}) != len(targets):
            raise BadRequestError("同一个章节及其细纲不能重复提交")
        if body.pov and body.pov.character_id is not None:
            await _validate_ids(db, project_id, Character, [body.pov.character_id])
        if body.tags:
            await _validate_ids(db, project_id, PlanningTag, body.tags.ids)
        for operation in body.references:
            if operation.kind == "character":
                await _validate_ids(db, project_id, Character, operation.ids)
            else:
                model = query.REFERENCE_JOINS[operation.kind][2]
                ids = operation.timeline_ids if operation.kind == "relationship" else operation.ids
                await _validate_ids(db, project_id, model, ids, relationship=operation.kind == "relationship")
        for target in targets:
            unit = await _materialize(db, project_id, target)
            if body.pov is not None:
                unit.pov_character_id = body.pov.character_id
            if body.tags is not None:
                existing = await _join_ids(db, PlanningUnitTag, "tag_id", unit.id)
                await _set_join(db, PlanningUnitTag, "tag_id", unit.id, _apply(existing, body.tags.op, body.tags.ids))
            for operation in body.references:
                if operation.kind == "character":
                    existing = await _character_ids(db, target)
                    await _set_characters(db, target, _apply(existing, operation.op, operation.ids))
                else:
                    model, column, _ = query.REFERENCE_JOINS[operation.kind]
                    values = operation.timeline_ids if operation.kind == "relationship" else operation.ids
                    existing = await _join_ids(db, model, column, unit.id)
                    await _set_join(db, model, column, unit.id, _apply(existing, operation.op, values))
        await db.flush()
        keys = {query.source_key(target.source) for target in targets}
        result = [row for row in await query.rows(db, project_id) if row.key in keys]
    return s.PlanningMutation(revision=body.expected_revision + 1, rows=result)


async def list_entries(
    db: AsyncSession, user_id: int, project_id: int, *, kind=None, category=None, q=None, page=1, page_size=50
) -> s.PlanningEntryPage:
    rev, catalog = await _snapshot(db, user_id, project_id, lambda: query.entries(db, project_id))
    selected = [
        entry
        for entry in catalog
        if (kind is None or entry.reference.kind == kind)
        and (category is None or entry.category == category)
        and (not q or q.strip().casefold() in entry.name.casefold())
    ]
    return s.PlanningEntryPage(
        revision=rev,
        items=selected[(page - 1) * page_size : page * page_size],
        total=len(selected),
        page=page,
        page_size=page_size,
    )


async def list_detached(db: AsyncSession, user_id: int, project_id: int, *, q=None, page=1, page_size=50):
    rev, items = await _snapshot(db, user_id, project_id, lambda: query.rows(db, project_id))
    items = [row for row in items if row.source.kind == "detached"
             and (not q or q.strip().casefold() in row.title.casefold())]
    return s.PlanningDetached(
        revision=rev,
        items=items[(page - 1) * page_size : page * page_size],
        total=len(items),
        page=page,
        page_size=page_size,
    )


async def _tag(db: AsyncSession, project_id: int, identity: int):
    item = await db.scalar(select(PlanningTag).where(PlanningTag.id == identity, PlanningTag.project_id == project_id))
    if item is None:
        raise NotFoundError("标签不存在")
    return item


async def list_tags(db: AsyncSession, user_id: int, project_id: int, *, q=None, page=1, page_size=50):
    async def read():
        counts = dict(
            (
                await db.execute(
                    select(PlanningUnitTag.tag_id, func.count())
                    .join(PlanningTag)
                    .where(PlanningTag.project_id == project_id)
                    .group_by(PlanningUnitTag.tag_id)
                )
            ).all()
        )
        return [
            s.PlanningTagOut(id=tag.id, name=tag.name, tone=tag.tone, usage_count=counts.get(tag.id, 0))
            for tag in await db.scalars(
                select(PlanningTag)
                .where(PlanningTag.project_id == project_id)
                .order_by(PlanningTag.id)
                .execution_options(populate_existing=True)
            )
        ]

    rev, items = await _snapshot(db, user_id, project_id, read)
    if q:
        items = [item for item in items if q.strip().casefold() in item.name.casefold()]
    return s.PlanningTagPage(
        revision=rev,
        items=items[(page - 1) * page_size : page * page_size],
        total=len(items),
        page=page,
        page_size=page_size,
    )


async def save_tag(db: AsyncSession, user_id: int, project_id: int, body, tag_id: int | None = None):
    async with _editing(db, user_id, project_id, body.expected_revision):
        item = await _tag(db, project_id, tag_id) if tag_id is not None else PlanningTag(project_id=project_id)
        values = body.model_dump(exclude_unset=True, exclude={"expected_revision"})
        name = values.get("name", item.name)
        duplicate = await db.scalar(
            select(PlanningTag.id).where(
                PlanningTag.project_id == project_id,
                PlanningTag.name == name,
                PlanningTag.id != tag_id if tag_id else True,
            )
        )
        if duplicate is not None:
            raise ConflictError("同名标签已存在")
        for key, value in values.items():
            setattr(item, key, value)
        db.add(item)
        await db.flush()
        result = s.PlanningTagOut.model_validate(item)
    return s.PlanningTagMutation(revision=body.expected_revision + 1, item=result)


async def delete_tag(db: AsyncSession, user_id: int, project_id: int, tag_id: int, expected_revision: int):
    async with _editing(db, user_id, project_id, expected_revision):
        await db.delete(await _tag(db, project_id, tag_id))
    return s.PlanningRevision(revision=expected_revision + 1)


async def _subplot(db: AsyncSession, project_id: int, identity: int) -> Subplot:
    item = await db.scalar(select(Subplot).where(Subplot.id == identity, Subplot.project_id == project_id))
    if item is None:
        raise NotFoundError("支线不存在")
    return item


async def _subplot_outs(db: AsyncSession, project_id: int) -> list[s.SubplotOut]:
    nodes = dict(
        (
            await db.execute(
                select(SubplotNode.subplot_id, func.count())
                .join(Subplot)
                .where(Subplot.project_id == project_id)
                .group_by(SubplotNode.subplot_id)
            )
        ).all()
    )
    crossings = dict(
        (
            await db.execute(
                select(SubplotCrossingParticipant.subplot_id, func.count())
                .join(Subplot)
                .where(Subplot.project_id == project_id)
                .group_by(SubplotCrossingParticipant.subplot_id)
            )
        ).all()
    )
    return [
        s.SubplotOut(
            id=item.id,
            name=item.name,
            description=item.description,
            tone=item.tone,
            sort=item.sort,
            node_count=nodes.get(item.id, 0),
            crossing_count=crossings.get(item.id, 0),
        )
        for item in await db.scalars(
            select(Subplot)
            .where(Subplot.project_id == project_id)
            .order_by(Subplot.sort, Subplot.id)
            .execution_options(populate_existing=True)
        )
    ]


async def list_subplots(db: AsyncSession, user_id: int, project_id: int, *, q=None, page=1, page_size=50):
    rev, items = await _snapshot(db, user_id, project_id, lambda: _subplot_outs(db, project_id))
    if q:
        items = [item for item in items if q.strip().casefold() in item.name.casefold()]
    return s.SubplotPage(
        revision=rev,
        items=items[(page - 1) * page_size : page * page_size],
        total=len(items),
        page=page,
        page_size=page_size,
    )


async def save_subplot(db: AsyncSession, user_id: int, project_id: int, body, subplot_id: int | None = None):
    async with _editing(db, user_id, project_id, body.expected_revision):
        item = await _subplot(db, project_id, subplot_id) if subplot_id else Subplot(project_id=project_id)
        for key, value in body.model_dump(exclude_unset=True, exclude={"expected_revision"}).items():
            setattr(item, key, value)
        db.add(item)
        await db.flush()
        result = s.SubplotOut.model_validate(item)
    return s.SubplotMutation(revision=body.expected_revision + 1, item=result)


async def delete_subplot(db: AsyncSession, user_id: int, project_id: int, subplot_id: int, expected_revision: int):
    async with _editing(db, user_id, project_id, expected_revision):
        item = await _subplot(db, project_id, subplot_id)
        references = list(
            await db.scalars(
                select(SubplotCrossingParticipant.crossing_id).where(
                    SubplotCrossingParticipant.subplot_id == subplot_id
                )
            )
        )
        if references:
            raise ConflictError(
                "该支线仍被交汇事件引用，请先编辑或删除相关事件",
                extra={
                    "code": "subplot_in_crossing",
                    "crossing_ids": references,
                },
            )
        await db.delete(item)
    return s.PlanningRevision(revision=expected_revision + 1)


async def put_node(
    db: AsyncSession, user_id: int, project_id: int, subplot_id: int, source: s.PlanningSource, body: s.SubplotNodePut
):
    async with _editing(db, user_id, project_id, body.expected_revision):
        await _subplot(db, project_id, subplot_id)
        target = await resolve_source(db, project_id, source)
        unit = await _materialize(db, project_id, target)
        node = await db.scalar(
            select(SubplotNode).where(SubplotNode.subplot_id == subplot_id, SubplotNode.unit_id == unit.id)
        )
        if node is None:
            node = SubplotNode(subplot_id=subplot_id, unit_id=unit.id, content=body.content)
            db.add(node)
        else:
            node.content = body.content
        await db.flush()
        result = s.SubplotNodeOut.model_validate(node)
    return s.SubplotNodeMutation(revision=body.expected_revision + 1, item=result)


async def delete_node(
    db: AsyncSession, user_id: int, project_id: int, subplot_id: int, source: s.PlanningSource, expected_revision: int
):
    async with _editing(db, user_id, project_id, expected_revision):
        await _subplot(db, project_id, subplot_id)
        target = await resolve_source(db, project_id, source)
        if target.unit:
            await db.execute(
                delete(SubplotNode).where(SubplotNode.subplot_id == subplot_id, SubplotNode.unit_id == target.unit.id)
            )
    return s.PlanningRevision(revision=expected_revision + 1)


async def _crossing(db: AsyncSession, project_id: int, identity: int) -> SubplotCrossing:
    item = await db.scalar(
        select(SubplotCrossing).where(SubplotCrossing.id == identity, SubplotCrossing.project_id == project_id)
    )
    if item is None:
        raise NotFoundError("交汇事件不存在")
    return item


async def list_crossings(
    db: AsyncSession, user_id: int, project_id: int, *, source=None, subplot_id=None, page=1, page_size=50
):
    async def read():
        all_rows = await query.rows(db, project_id)
        items = await query.crossings(db, project_id, all_rows)
        if source:
            target = await resolve_source(db, project_id, source)
            key = query.source_key(target.source)
            items = [item for item in items if query.source_key(item.source) == key]
        if subplot_id is not None:
            await _subplot(db, project_id, subplot_id)
            items = [item for item in items if subplot_id in item.subplot_ids]
        return items

    rev, items = await _snapshot(db, user_id, project_id, read)
    return s.SubplotCrossingPage(
        revision=rev,
        items=items[(page - 1) * page_size : page * page_size],
        total=len(items),
        page=page,
        page_size=page_size,
    )


async def save_crossing(db: AsyncSession, user_id: int, project_id: int, body, crossing_id: int | None = None):
    async with _editing(db, user_id, project_id, body.expected_revision):
        item = await _crossing(db, project_id, crossing_id) if crossing_id else None
        values = body.model_dump(exclude_unset=True, exclude={"expected_revision", "source", "subplot_ids"})
        if body.subplot_ids is not None:
            await _validate_ids(db, project_id, Subplot, body.subplot_ids)
        if body.source is not None:
            target = await resolve_source(db, project_id, body.source)
            unit = await _materialize(db, project_id, target)
        else:
            unit = await _unit(db, project_id, item.unit_id)
        if item is None:
            item = SubplotCrossing(project_id=project_id, unit_id=unit.id, **values)
            db.add(item)
        else:
            item.unit_id = unit.id
            for key, value in values.items():
                setattr(item, key, value)
        await db.flush()
        if body.subplot_ids is not None:
            await db.execute(
                delete(SubplotCrossingParticipant).where(SubplotCrossingParticipant.crossing_id == item.id)
            )
            db.add_all(
                SubplotCrossingParticipant(crossing_id=item.id, subplot_id=identity) for identity in body.subplot_ids
            )
        await db.flush()
        result = next(
            crossing
            for crossing in await query.crossings(db, project_id, await query.rows(db, project_id))
            if crossing.id == item.id
        )
    return s.SubplotCrossingMutation(revision=body.expected_revision + 1, item=result)


async def delete_crossing(db: AsyncSession, user_id: int, project_id: int, crossing_id: int, expected_revision: int):
    async with _editing(db, user_id, project_id, expected_revision):
        await db.delete(await _crossing(db, project_id, crossing_id))
    return s.PlanningRevision(revision=expected_revision + 1)


async def merge_conflict(db: AsyncSession, project_id: int, first: PlanningUnit, second: PlanningUnit) -> ConflictError:
    all_rows = await query.rows(db, project_id)
    units = [row for row in all_rows if row.unit_id in (first.id, second.id)]
    grouped: dict[int, list[s.SubplotNodeOut]] = defaultdict(list)
    for node in await db.scalars(select(SubplotNode).where(SubplotNode.unit_id.in_((first.id, second.id)))):
        grouped[node.subplot_id].append(s.SubplotNodeOut.model_validate(node))
    names = dict((await db.execute(select(Subplot.id, Subplot.name).where(Subplot.project_id == project_id))).all())
    survivor = first if first.chapter_id is not None else second if second.chapter_id is not None else first
    return ConflictError(
        "两处已有规划，请核对并明确合并后重试",
        extra={
            "code": "planning_merge_conflict",
            "current_revision": await query.revision(db, project_id),
            "unit_ids": [first.id, second.id],
            "units": [row.model_dump() for row in units],
            "survivor_unit_id": survivor.id,
            "pov_character_ids": [first.pov_character_id, second.pov_character_id],
            "node_conflicts": [
                {"subplot_id": identity, "subplot_name": names.get(identity),
                 "nodes": [node.model_dump() for node in nodes]}
                for identity, nodes in grouped.items()
                if len(nodes) > 1
            ],
        },
    )


async def _target_for_unit(db: AsyncSession, project_id: int, unit: PlanningUnit) -> SourceTarget:
    if unit.chapter_id is not None:
        source = s.PlanningSource(kind="chapter", id=unit.chapter_id)
    elif unit.plan_id is not None:
        source = s.PlanningSource(kind="plan", id=unit.plan_id)
    else:
        source = s.PlanningSource(kind="detached", id=unit.id)
    target = await resolve_source(db, project_id, source)
    if target.unit is None or target.unit.id != unit.id:
        raise ConflictError("规划来源已改变，请刷新", extra={"code": "planning_source_changed"})
    return target


async def bind_unit(db: AsyncSession, user_id: int, project_id: int, unit_id: int, body: s.PlanningBinding):
    async with _editing(db, user_id, project_id, body.expected_revision):
        unit = await _unit(db, project_id, unit_id)
        original = await _target_for_unit(db, project_id, unit)
        target = await resolve_source(db, project_id, body.target)
        if target.unit is not None and target.unit.id != unit.id:
            raise await merge_conflict(db, project_id, target.unit, unit)
        character_ids = sorted(set(await _character_ids(db, original)) | set(await _character_ids(db, target)))
        unit.chapter_id = target.chapter.id if target.chapter else None
        unit.plan_id = target.plan.id if target.plan else None
        unit.binding_epoch += 1
        unit.detached_character_ids = []
        unit.last_source_label = target.chapter.title if target.chapter else target.plan.title
        outline_id = target.chapter.outline_id if target.chapter else target.plan.outline_id
        outline = await db.scalar(select(Outline).where(Outline.id == outline_id, Outline.project_id == project_id))
        unit.last_volume_label = outline.volume_title if outline else ""
        target.unit = unit
        await _set_characters(db, target, character_ids)
        await db.flush()
        result = [row for row in await query.rows(db, project_id) if row.unit_id == unit.id]
    return s.PlanningMutation(revision=body.expected_revision + 1, rows=result)


async def merge_units(db: AsyncSession, user_id: int, project_id: int, body: s.PlanningMerge):
    async with _editing(db, user_id, project_id, body.expected_revision):
        survivor = await _unit(db, project_id, body.survivor_unit_id)
        other = await _unit(db, project_id, body.other_unit_id)
        if other.chapter_id is not None and survivor.chapter_id is None:
            raise await merge_conflict(db, project_id, survivor, other)
        left = await _target_for_unit(db, project_id, survivor)
        right = await _target_for_unit(db, project_id, other)
        if body.pov_character_id is not None:
            await _validate_ids(db, project_id, Character, [body.pov_character_id])
        grouped: dict[int, list[SubplotNode]] = defaultdict(list)
        for node in await db.scalars(select(SubplotNode).where(SubplotNode.unit_id.in_((survivor.id, other.id)))):
            grouped[node.subplot_id].append(node)
        conflicts = {identity: nodes for identity, nodes in grouped.items() if len(nodes) == 2}
        resolutions = {item.subplot_id: item for item in body.node_resolutions}
        if set(resolutions) != set(conflicts) or any(
            item.keep_node_id not in {node.id for node in conflicts[identity]} for identity, item in resolutions.items()
        ):
            raise await merge_conflict(db, project_id, survivor, other)
        # 先完整验证，再删除冲突中的另一节点，避免移动时撞唯一约束。
        for identity, nodes in conflicts.items():
            keep = resolutions[identity].keep_node_id
            await db.delete(next(node for node in nodes if node.id != keep))
        await db.flush()
        for identity, nodes in grouped.items():
            for node in nodes:
                if identity in conflicts and node.id != resolutions[identity].keep_node_id:
                    continue
                node.unit_id = survivor.id
                if identity in resolutions:
                    node.content = resolutions[identity].content
        await db.execute(update(SubplotCrossing).where(SubplotCrossing.unit_id == other.id).values(unit_id=survivor.id))
        for model, column in [
            (PlanningUnitTag, "tag_id"),
            *[(value[0], value[1]) for value in query.REFERENCE_JOINS.values()],
        ]:
            wanted = sorted(
                set(await _join_ids(db, model, column, survivor.id)) | set(await _join_ids(db, model, column, other.id))
            )
            await _set_join(db, model, column, survivor.id, wanted)
            await _set_join(db, model, column, other.id, [])
        characters = sorted(set(await _character_ids(db, left)) | set(await _character_ids(db, right)))
        await _set_characters(db, left, characters)
        await _set_characters(db, right, [])
        survivor.pov_character_id = body.pov_character_id
        survivor.binding_epoch += 1
        other.chapter_id = other.plan_id = other.pov_character_id = None
        other.detached_character_ids = []
        other.binding_epoch += 1
        other.retired = True
        await db.flush()
        result = [row for row in await query.rows(db, project_id) if row.unit_id == survivor.id]
    return s.PlanningMutation(revision=body.expected_revision + 1, rows=result)


async def _nodes(db: AsyncSession, project_id: int, unit_ids=None) -> list[s.SubplotNodeOut]:
    if unit_ids is not None and not unit_ids:
        return []
    conditions = [PlanningUnit.project_id == project_id, PlanningUnit.retired.is_(False)]
    if unit_ids is not None:
        conditions.append(SubplotNode.unit_id.in_(unit_ids))
    return [
        s.SubplotNodeOut.model_validate(node)
        for node in await db.scalars(
            select(SubplotNode)
            .join(PlanningUnit)
            .where(*conditions)
            .order_by(SubplotNode.id)
            .execution_options(populate_existing=True)
        )
    ]


async def matrix(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    *,
    page=1,
    page_size=50,
    column_page=1,
    column_mode="settings",
    column_refs=None,
    source_kind=None,
    volume_no=None,
    tag_id=None,
    q=None,
    entity_kind=None,
    entry_q=None,
    source_refs=None,
):
    async def read():
        all_rows = await query.rows(db, project_id)
        catalog = await query.entries(db, project_id)
        subplots = await _subplot_outs(db, project_id)
        return all_rows, catalog, subplots

    rev, (all_rows, catalog, subplots) = await _snapshot(db, user_id, project_id, read)
    selected = [
        row
        for row in all_rows
        if row.source.kind != "detached"
        and (source_kind is None or row.source.kind == source_kind)
        and (volume_no is None or row.volume_no == volume_no)
        and (tag_id is None or any(tag.id == tag_id for tag in row.tags))
        and (not q or q.strip().casefold() in row.title.casefold())
    ]
    warnings = []
    if source_refs is not None:
        requested = {(source.kind, source.id) for source in source_refs}
        selected = [
            row
            for row in all_rows
            if (row.source.kind, row.source.id) in requested
            or (row.plan_id is not None and ("plan", row.plan_id) in requested)
        ]
        found = {(row.source.kind, row.source.id) for row in selected}
        found |= {("plan", row.plan_id) for row in selected if row.plan_id is not None}
        missing = requested - found
        if missing:
            warnings.append(f"{len(missing)} 个原目标已不存在或无法访问，请重新选择")
    total = len(selected)
    selected = selected[(page - 1) * page_size : page * page_size]
    entry_map = {query.reference_key(item.reference): item for item in catalog}
    subplot_map = {item.id: item for item in subplots}
    columns = []
    if column_refs is not None:
        if len({item.model_dump_json() for item in column_refs}) != len(column_refs):
            raise BadRequestError("矩阵列不能重复")
        for item in column_refs:
            if item.kind == "setting":
                entry = entry_map.get(query.reference_key(item.reference))
                if entry is None:
                    raise NotFoundError("矩阵设定列不存在")
                columns.append(
                    s.MatrixColumn(
                        key=query.reference_key(entry.reference),
                        selection=item,
                        name=entry.name,
                        tracking_supported=entry.tracking_supported,
                    )
                )
            else:
                subplot = subplot_map.get(item.subplot_id)
                if subplot is None:
                    raise NotFoundError("矩阵支线列不存在")
                columns.append(
                    s.MatrixColumn(key=f"subplot:{subplot.id}", selection=item, name=subplot.name, tone=subplot.tone)
                )
    elif column_mode == "subplots":
        columns = [
            s.MatrixColumn(
                key=f"subplot:{item.id}",
                selection=s.SubplotColumn(kind="subplot", subplot_id=item.id),
                name=item.name,
                tone=item.tone,
            )
            for item in subplots
        ]
    elif column_mode != "custom":
        columns = [
            s.MatrixColumn(
                key=query.reference_key(item.reference),
                selection=s.SettingColumn(kind="setting", reference=item.reference),
                name=item.name,
                tracking_supported=item.tracking_supported,
            )
            for item in catalog
            if (entity_kind is None or item.reference.kind == entity_kind)
            and (not entry_q or entry_q.strip().casefold() in item.name.casefold())
        ]
    column_total = len(columns)
    columns = columns[(column_page - 1) * 30 : column_page * 30]
    with db.no_autoflush:
        counts = await _mention_counts(db, user_id, project_id, selected, columns)
        has_subplot_columns = any(column.selection.kind == "subplot" for column in columns)
        nodes = (
            await _nodes(db, project_id, [row.unit_id for row in selected if row.unit_id])
            if has_subplot_columns else []
        )
        crossings = await query.crossings(db, project_id, selected) if has_subplot_columns else []
    nodes_by_key = {(node.unit_id, node.subplot_id): node for node in nodes}
    crossing_by_key: dict[tuple[int, int], list[s.SubplotCrossingOut]] = defaultdict(list)
    for crossing in crossings:
        for identity in crossing.subplot_ids:
            crossing_by_key[crossing.unit_id, identity].append(crossing)
    cells = []
    for row in selected:
        manual = {query.reference_key(reference) for reference in row.references}
        for column in columns:
            if column.selection.kind == "setting":
                ref = column.selection.reference
                definite, ambiguous = counts.get((row.chapter_id, column.key), (0, 0))
                cells.append(
                    s.MatrixSettingCell(
                        row_key=row.key,
                        column_key=column.key,
                        manual=column.key in manual,
                        pov=ref.kind == "character" and ref.id == row.pov_character_id,
                        tracking_supported=column.tracking_supported,
                        has_content=row.has_content,
                        mention_count=definite,
                        ambiguous_count=ambiguous,
                    )
                )
            else:
                node = nodes_by_key.get((row.unit_id, column.selection.subplot_id))
                events = crossing_by_key.get((row.unit_id, column.selection.subplot_id), [])
                cells.append(
                    s.MatrixSubplotCell(
                        row_key=row.key,
                        column_key=column.key,
                        node=node,
                        crossings=events,
                        has_progress=bool(node or events),
                    )
                )
    with db.no_autoflush:
        current_revision = await query.revision(db, project_id)
    if current_revision != rev:
        raise _stale(current_revision)
    return s.PlanningMatrix(
        revision=rev,
        rows=selected,
        columns=columns,
        cells=cells,
        total=total,
        column_total=column_total,
        page=page,
        page_size=page_size,
        column_page=column_page,
        warnings=warnings,
    )


async def _mention_counts(db: AsyncSession, user_id: int, project_id: int, rows, columns):
    from app.services.mentions.context import query_context
    from app.services.mentions.matcher import cache

    keys = {column.key for column in columns if column.tracking_supported}
    chapter_ids = [row.chapter_id for row in rows if row.chapter_id is not None]
    if not keys or not chapter_ids:
        return {}
    dictionary = (await query_context(db, user_id, project_id)).dictionary
    documents = list(
        await db.execute(
            select(Chapter.id, Chapter.content).where(Chapter.project_id == project_id, Chapter.id.in_(chapter_ids))
        )
    )

    def count():
        found = {}
        for chapter_id, content in documents:
            for hit in cache.iter_matches((project_id, "chapter", chapter_id), content, dictionary):
                owners = dictionary.owners[hit.term]
                bucket = 0 if len(owners) == 1 else 1
                for owner in owners:
                    key = f"{owner.kind}:{owner.id}"
                    if key in keys:
                        counts = found.setdefault((chapter_id, key), [0, 0])
                        counts[bucket] += 1
        return found

    return await anyio.to_thread.run_sync(count)


async def weave(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    *,
    axis="chapter",
    page=1,
    page_size=50,
    subplot_page=1,
    subplot_page_size=20,
    subplot_ids=None,
    gap_threshold=5,
):
    async def read():
        all_rows = await query.rows(db, project_id)
        nodes = list(
            await db.execute(
                select(SubplotNode.unit_id, SubplotNode.subplot_id)
                .join(PlanningUnit)
                .where(PlanningUnit.project_id == project_id, PlanningUnit.retired.is_(False))
            )
        )
        crossings = list(
            await db.execute(
                select(SubplotCrossing.unit_id, SubplotCrossingParticipant.subplot_id)
                .join(SubplotCrossingParticipant, SubplotCrossingParticipant.crossing_id == SubplotCrossing.id)
                .where(SubplotCrossing.project_id == project_id)
            )
        )
        return (
            all_rows,
            await _subplot_outs(db, project_id),
            nodes,
            crossings,
        )

    rev, (all_rows, subplots, node_positions, crossing_positions) = await _snapshot(db, user_id, project_id, read)
    axis_rows = [row for row in all_rows if row.source.kind == axis]
    by_unit = {row.unit_id: row for row in axis_rows if row.unit_id is not None}
    rank = {row.key: index for index, row in enumerate(axis_rows)}
    labels = {
        row.key: (f"第 {row.chapter_no} 章 · {row.title}" if row.chapter_no is not None
                  else f"第 {row.volume_no} 卷 · 细纲 {row.seq} · {row.title}")
        for row in axis_rows
    }
    if subplot_ids is not None:
        if set(subplot_ids) - {item.id for item in subplots}:
            raise NotFoundError("部分支线不存在")
        subplots = [item for item in subplots if item.id in subplot_ids]
    subplot_total = len(subplots)
    subplots = subplots[(subplot_page - 1) * subplot_page_size : subplot_page * subplot_page_size]
    chosen = {item.id for item in subplots}
    visible_rows = axis_rows[(page - 1) * page_size : page * page_size]
    with db.no_autoflush:
        nodes = await _nodes(db, project_id, [row.unit_id for row in visible_rows if row.unit_id])
        visible_crossings = [
            item for item in await query.crossings(db, project_id, visible_rows)
            if chosen.intersection(item.subplot_ids)
        ]
    all_progress: dict[str, set[int]] = defaultdict(set)
    for unit_id, subplot_id in [*node_positions, *crossing_positions]:
        if unit_id in by_unit:
            all_progress[by_unit[unit_id].key].add(subplot_id)
    tracks = []
    for subplot in subplots:
        keys = [row.key for row in axis_rows if subplot.id in all_progress[row.key]]
        gaps = []
        for index, key in enumerate(keys):
            following = keys[index + 1] if index + 1 < len(keys) else None
            count = (rank[following] if following else len(axis_rows)) - rank[key] - 1
            if count:
                gaps.append(
                    s.WeaveGap(
                        after_key=key,
                        before_key=following,
                        after_label=labels[key],
                        before_label=labels[following] if following is not None else None,
                        gap_units=count,
                        is_tail=following is None,
                        exceeds_threshold=count >= gap_threshold,
                    )
                )
        tracks.append(
            s.WeaveTrack(
                subplot=subplot,
                progress_keys=keys,
                nodes=[node for node in nodes if node.subplot_id == subplot.id and node.unit_id in by_unit],
                crossing_ids=[item.id for item in visible_crossings if subplot.id in item.subplot_ids],
                gaps=gaps,
                unplanned=not keys,
            )
        )
    with db.no_autoflush:
        current_revision = await query.revision(db, project_id)
    if current_revision != rev:
        raise _stale(current_revision)
    return s.PlanningWeave(
        revision=rev,
        axis=axis,
        rows=visible_rows,
        tracks=tracks,
        crossings=visible_crossings,
        same_position={key: sorted(ids) for key, ids in all_progress.items() if len(ids) >= 2},
        total=len(axis_rows),
        page=page,
        page_size=page_size,
        subplot_total=subplot_total,
        subplot_page=subplot_page,
        gap_threshold=gap_threshold,
    )
