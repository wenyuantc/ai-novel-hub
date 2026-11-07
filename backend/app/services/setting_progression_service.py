"""设定时间线持久化、只读求值和人工维护入口。

事实写入原语只 flush；HTTP 包装函数负责项目写屏障和提交。后续分析、导入
等调用者可在自己的事务或保存点内组合这些原语。
"""

from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from typing import cast

from pydantic import JsonValue, ValidationError
from sqlalchemy import and_, case, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import set_committed_value

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, Character, Project, WorldSetting
from app.models.progressions import SettingProgression, SettingTextField, SettingTimeline
from app.models.scenes import Scene
from app.schemas.progression import (
    AnchorKind,
    BaselineOut,
    BaselineStatus,
    BaselineUpdate,
    Boundary,
    EffectiveOut,
    ProgressionCreate,
    ProgressionListOut,
    ProgressionMutationOut,
    ProgressionOperation,
    ProgressionOut,
    ProgressionProtection,
    ProgressionSource,
    ProgressionUpdate,
    RevisionOut,
    StoryPosition,
    SubjectKind,
    TextFieldCreate,
    TextFieldListOut,
    TextFieldMutationOut,
    TextFieldOut,
    TextFieldUpdate,
    TimelineCreate,
    TimelineListOut,
    TimelineOut,
)
from app.services import series_policy
from app.services.project_service import get_manageable_project
from app.services.setting_field_values import CanonicalFieldValue
from app.services.setting_progression_resolver import (
    EffectiveSnapshot,
    ProgressionEvent,
    resolve_progressions,
    validate_baseline,
    validate_progression_value,
)

CHARACTER_FIELDS = (
    "profile",
    "appearance",
    "personality",
    "background",
    "current_state",
    "age",
    "gender",
    "life_status",
)


def _readonly(reader):
    @wraps(reader)
    async def wrapped(db: AsyncSession, *args, **kwargs):
        with db.no_autoflush:
            return await reader(db, *args, **kwargs)

    return wrapped


async def _check_read_revisions(
    db: AsyncSession, project_id: int, timeline_id: int, timeline_revision: int, field_revision: int
):
    from app.services.setting_field_service import fields_revision, stale_revision

    current_timeline = await db.scalar(select(SettingTimeline.revision).where(SettingTimeline.id == timeline_id))
    if current_timeline != timeline_revision:
        raise stale_revision(current_timeline or 0, "timeline")
    current_fields = await fields_revision(db, project_id)
    if current_fields != field_revision:
        raise stale_revision(current_fields)


def _stale_revision() -> ConflictError:
    return ConflictError("设定已被其他操作修改，请刷新后重试", extra={"code": "stale_revision", "scope": "timeline"})


async def _read_project(db: AsyncSession, user_id: int, project_id: int) -> None:
    await get_manageable_project(db, user_id, project_id)


async def _write_project(db: AsyncSession, user_id: int, project_id: int) -> None:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")


async def get_timeline(db: AsyncSession, project_id: int, timeline_id: int) -> SettingTimeline:
    timeline = await db.scalar(
        select(SettingTimeline).where(SettingTimeline.id == timeline_id, SettingTimeline.project_id == project_id)
    )
    if timeline is None:
        raise NotFoundError("设定时间线不存在")
    return timeline


async def _claim_revision(
    db: AsyncSession,
    timeline: SettingTimeline,
    expected_revision: int,
    *,
    preserve_legacy_eligibility: bool = False,
    metadata_only: bool = False,
) -> None:
    """原子比较版本；基础或事实写入关闭 M1 资格，纯元数据不影响资格。"""
    if preserve_legacy_eligibility and metadata_only:
        raise ValueError("不能同时指定旧分析和元数据写入模式")
    stmt = update(SettingTimeline).where(
        SettingTimeline.id == timeline.id,
        SettingTimeline.project_id == timeline.project_id,
        SettingTimeline.revision == expected_revision,
    )
    if preserve_legacy_eligibility:
        stmt = stmt.where(
            SettingTimeline.baseline_status == "legacy_unverified",
            SettingTimeline.legacy_compat_allowed.is_(True),
        ).values(revision=SettingTimeline.revision + 1)
    elif metadata_only:
        stmt = stmt.values(revision=SettingTimeline.revision + 1)
    else:
        stmt = stmt.values(revision=SettingTimeline.revision + 1, legacy_compat_allowed=False)
    result = await db.execute(stmt.execution_options(synchronize_session="fetch"))
    if result.rowcount != 1:
        raise _stale_revision()
    await db.refresh(timeline)


def _identity(timeline: SettingTimeline) -> tuple[int, int] | None:
    if timeline.subject_kind != "relationship":
        return None
    assert timeline.character_id is not None and timeline.related_character_id is not None
    return timeline.character_id, timeline.related_character_id


async def _timeline_for_world(db: AsyncSession, world: WorldSetting) -> SettingTimeline | None:
    return await db.scalar(
        select(SettingTimeline).where(
            SettingTimeline.project_id == world.project_id,
            SettingTimeline.subject_kind == "world",
            SettingTimeline.world_setting_id == world.id,
        )
    )


async def create_world_timeline(
    db: AsyncSession, world: WorldSetting, *, baseline_status: BaselineStatus = "confirmed"
) -> SettingTimeline:
    """显式初始化，不 commit；已有主体及其校准状态原样保留。"""
    await db.flush()
    existing = await _timeline_for_world(db, world)
    if existing is not None:
        return existing
    timeline = SettingTimeline(
        project_id=world.project_id,
        subject_kind="world",
        world_setting_id=world.id,
        baseline={"content": world.content},
        baseline_status=baseline_status,
        legacy_compat_allowed=baseline_status == "legacy_unverified",
    )
    db.add(timeline)
    await db.flush()
    return timeline


async def create_character_timeline(
    db: AsyncSession, character: Character, *, baseline_status: BaselineStatus = "confirmed"
) -> SettingTimeline:
    """供角色创建、卡片导入等复用的幂等初始化原语。"""
    await db.flush()
    existing = await db.scalar(
        select(SettingTimeline).where(
            SettingTimeline.project_id == character.project_id,
            SettingTimeline.subject_kind == "character",
            SettingTimeline.character_id == character.id,
        )
    )
    if existing is not None:
        return existing
    timeline = SettingTimeline(
        project_id=character.project_id,
        subject_kind="character",
        character_id=character.id,
        baseline={name: getattr(character, name) for name in CHARACTER_FIELDS},
        baseline_status=baseline_status,
        legacy_compat_allowed=baseline_status == "legacy_unverified",
    )
    db.add(timeline)
    await db.flush()
    return timeline


def _clean_label(label: str) -> str:
    cleaned = label.strip()
    if not cleaned:
        raise BadRequestError("字段名称不能为空")
    if len(cleaned) > 100:
        raise BadRequestError("字段名称不能超过 100 字")
    return cleaned


async def add_initial_text_fields(
    db: AsyncSession, timeline: SettingTimeline, fields: list[tuple[str, str]]
) -> list[SettingTextField]:
    """初始化文本字段；重复且完全相同可重试，绝不覆盖已有不同内容。"""
    if timeline.subject_kind not in ("character", "world"):
        raise BadRequestError("该设定不支持自定义文本字段")
    existing = {
        field.label: field
        for field in (
            await db.scalars(select(SettingTextField).where(SettingTextField.timeline_id == timeline.id))
        ).all()
    }
    result: list[SettingTextField] = []
    for index, (raw_label, value) in enumerate(fields):
        label = _clean_label(raw_label)
        if not isinstance(value, str):
            raise BadRequestError("自定义字段的基础值必须是文本")
        prior = existing.get(label)
        if prior is not None:
            if prior.base_value != value:
                raise ConflictError("同名自定义字段的基础值不一致")
            result.append(prior)
            continue
        field = SettingTextField(timeline_id=timeline.id, label=label, base_value=value, sort=index)
        db.add(field)
        existing[label] = field
        result.append(field)
    await db.flush()
    return result


def _as_event(
    row: SettingProgression, resolved_chapter_no: int | None, *, scene_position: StoryPosition | None = None
) -> ProgressionEvent:
    return ProgressionEvent(
        id=row.id,
        field_key=row.field_key,
        custom_field_id=row.custom_field_id,
        operation=cast(ProgressionOperation, row.operation),
        value=row.value,
        anchor_kind=cast(AnchorKind, row.anchor_kind),
        chapter_id=row.chapter_id,
        chapter_no=row.chapter_no,
        scene_id=row.scene_id,
        resolved_chapter_no=(scene_position.chapter_no if scene_position else None)
        if row.anchor_kind == "scene" else resolved_chapter_no,
        resolved_chapter_id=scene_position.chapter_id if scene_position else None,
        resolved_scene_order=scene_position.scene_order if scene_position else None,
        boundary=cast(Boundary, row.boundary),
        sort_order=row.sort_order,
        protection=cast(ProgressionProtection, row.protection),
        active=row.active,
        source_change_id=row.source_change_id,
    )


async def load_scene_positions(
    db: AsyncSession, project_id: int, scene_ids: Collection[int]
) -> dict[int, StoryPosition]:
    """只从活动身份解析位置；退场或物理失联从不按序号认领。"""
    if not scene_ids:
        return {}
    with db.no_autoflush:
        return {row.id: StoryPosition(
            chapter_id=row.chapter_id, chapter_no=row.chapter_no, scene_id=row.id, scene_order=row.sort
        ) for row in await db.execute(select(Scene.id, Scene.chapter_id, Scene.sort, Chapter.chapter_no)
                                     .join(Chapter, Chapter.id == Scene.chapter_id).where(
            Chapter.project_id == project_id, Scene.id.in_(scene_ids), Scene.active.is_(True),
        ))}


async def resolve_scene_position(
    db: AsyncSession, project_id: int, scene_id: int, boundary: Boundary = "start"
) -> StoryPosition:
    position = (await load_scene_positions(db, project_id, [scene_id])).get(scene_id)
    if position is None:
        raise BadRequestError("场景不存在、已退场或不属于该作品")
    return position.model_copy(update={"boundary": boundary})


async def resolve_story_position(
    db: AsyncSession, project_id: int, *, chapter_id: int | None = None, chapter_no: int | None = None,
    scene_id: int | None = None, boundary: Boundary = "start", optional: bool = False,
) -> StoryPosition | None:
    """HTTP 的位置联合边界共用校验，调用者不可自报 Scene.sort。"""
    count = sum(value is not None for value in (chapter_id, chapter_no, scene_id))
    if count == 0 and optional:
        return None
    if count != 1:
        raise BadRequestError("请选择一个场景、已有章节或计划章节号")
    if scene_id is not None:
        return await resolve_scene_position(db, project_id, scene_id, boundary)
    if chapter_id is not None:
        chapter = await _chapter_for_anchor(db, project_id, chapter_id)
        chapter_no = chapter.chapter_no
    return StoryPosition(chapter_id=chapter_id, chapter_no=chapter_no, boundary=boundary)


@dataclass(frozen=True)
class FieldTimelineSnapshot:
    """仅字段投影所需的时间线身份，不加载无关内置正文。"""

    id: int
    project_id: int
    subject_kind: str
    character_id: int | None
    world_setting_id: int | None
    organization_id: int | None
    career_id: int | None
    revision: int
    baseline_status: str


async def resolve_timelines(
    db: AsyncSession,
    project_id: int,
    timelines: Sequence[SettingTimeline],
    position: StoryPosition | None = None,
    *,
    exclude_event_ids: Iterable[int] = (),
) -> dict[int, EffectiveSnapshot]:
    """批量、只读地求值；事件与字段需由写调用方提前 flush。"""
    return await _resolve_timeline_snapshots(db, project_id, timelines, position, exclude_event_ids=exclude_event_ids)


async def _resolve_field_timelines(
    db: AsyncSession,
    project_id: int,
    timelines: Sequence[FieldTimelineSnapshot],
    position: StoryPosition | None,
    *,
    definition_ids: Collection[int],
) -> dict[int, dict[int, CanonicalFieldValue]]:
    """内部字段范围投影只返回值，不能被误用为完整事实快照。"""
    snapshots = await _resolve_timeline_snapshots(db, project_id, timelines, position, definition_ids=definition_ids)
    return {timeline_id: snapshot.field_values for timeline_id, snapshot in snapshots.items()}


async def _resolve_timeline_snapshots(
    db: AsyncSession,
    project_id: int,
    timelines: Sequence[SettingTimeline | FieldTimelineSnapshot],
    position: StoryPosition | None,
    *,
    exclude_event_ids: Iterable[int] = (),
    definition_ids: Collection[int] | None = None,
) -> dict[int, EffectiveSnapshot]:
    """共享SQL与纯resolver；部分快照只允许由内部字段入口消费。"""
    if not timelines:
        return {}
    if any(t.project_id != project_id or t.id is None for t in timelines):
        raise ValueError("时间线必须已保存且全部属于同一作品")
    ids = [t.id for t in timelines]
    custom_by_timeline: dict[int, dict[int, JsonValue]] = defaultdict(dict)
    events_by_timeline: dict[int, list[ProgressionEvent]] = defaultdict(list)
    # 只读标量列而非 ORM 实例，避免 populate_existing 覆盖调用方尚未保存的对象；
    # no_autoflush 防止预览查询意外把同一 session 的其他脏对象写入事务。
    with db.no_autoflush:
        from app.services.setting_field_service import (
            fields_revision,
            load_field_specs,
            read_field_bindings,
            stale_revision,
        )

        story_revision = await db.scalar(select(Project.planning_revision).where(Project.id == project_id))
        before_fields = await fields_revision(db, project_id)
        before_timelines = dict(
            (
                await db.execute(
                    select(SettingTimeline.id, SettingTimeline.revision).where(
                        SettingTimeline.id.in_(ids),
                    )
                )
            ).all()
        )
        if any(before_timelines.get(timeline.id) != timeline.revision for timeline in timelines):
            raise stale_revision(max(before_timelines.values(), default=0), "timeline")
        fields = (
            await read_field_bindings(db, ids)
            if definition_ids is None
            else await read_field_bindings(
                db,
                ids,
                definition_ids=definition_ids,
            )
        )
        specs = (
            await load_field_specs(db, project_id, fields)
            if definition_ids is None
            else await load_field_specs(
                db,
                project_id,
                fields,
                definition_ids=definition_ids,
            )
        )
        for field in fields:
            custom_by_timeline[field.timeline_id][field.id] = deepcopy(field.base_value)
        event_predicates = [SettingProgression.timeline_id.in_(ids)]
        if definition_ids is not None:
            event_predicates.append(SettingProgression.custom_field_id.in_([field.id for field in fields]))
        rows = (
            (await db.execute(
                select(
                    SettingProgression.id,
                    SettingProgression.timeline_id,
                    SettingProgression.field_key,
                    SettingProgression.custom_field_id,
                    SettingProgression.operation,
                    SettingProgression.value,
                    SettingProgression.anchor_kind,
                    SettingProgression.chapter_id,
                    SettingProgression.chapter_no,
                    SettingProgression.scene_id,
                    SettingProgression.boundary,
                    SettingProgression.sort_order,
                    SettingProgression.protection,
                    SettingProgression.active,
                    SettingProgression.source_change_id,
                    Chapter.chapter_no.label("resolved_chapter_no"),
                )
                .outerjoin(
                    Chapter,
                    and_(Chapter.id == SettingProgression.chapter_id, Chapter.project_id == project_id),
                )
                .where(*event_predicates)
            )).all()
            if definition_ids is None or fields
            else []
        )
        scene_ids = {row.scene_id for row in rows if row.scene_id is not None}
        if position is not None and position.scene_id is not None:
            scene_ids.add(position.scene_id)
        scene_positions = await load_scene_positions(db, project_id, scene_ids)
        if position is not None and position.scene_id is not None:
            current = scene_positions.get(position.scene_id)
            if (current is None or current.chapter_id != position.chapter_id
                    or current.chapter_no != position.chapter_no or current.scene_order != position.scene_order):
                raise ConflictError("场景故事位置已变化，请重新预览", extra={"code": "scene_context_changed"})
        for row in rows:
            events_by_timeline[row.timeline_id].append(
                _as_event(cast(SettingProgression, row), row.resolved_chapter_no,
                          scene_position=scene_positions.get(row.scene_id))
            )
        after_fields = await fields_revision(db, project_id)
        if before_fields != after_fields:
            raise stale_revision(after_fields)
        after_timelines = dict(
            (
                await db.execute(
                    select(SettingTimeline.id, SettingTimeline.revision).where(
                        SettingTimeline.id.in_(ids),
                    )
                )
            ).all()
        )
        if before_timelines != after_timelines:
            raise stale_revision(max(after_timelines.values(), default=0), "timeline")
        if scene_ids and story_revision != await db.scalar(select(Project.planning_revision).where(
            Project.id == project_id
        )):
            raise ConflictError("场景故事位置读取期间已变化，请重新预览", extra={"code": "scene_context_changed"})
    excluded = tuple(exclude_event_ids)
    return {
        timeline.id: resolve_progressions(
            cast(SubjectKind, timeline.subject_kind),
            cast(SettingTimeline, timeline).baseline if definition_ids is None else {},
            custom_by_timeline[timeline.id],
            events_by_timeline[timeline.id],
            position,
            baseline_status=cast(BaselineStatus, timeline.baseline_status),
            exclude_event_ids=excluded,
            subject_identity=_identity(cast(SettingTimeline, timeline)) if definition_ids is None else None,
            field_specs={field_id: specs[field_id] for field_id in custom_by_timeline[timeline.id]},
        )
        for timeline in timelines
    }


async def latest_character_snapshots(
    db: AsyncSession, project_id: int, character_ids: Sequence[int]
) -> dict[int, list[dict[str, str | int]]]:
    """批量返回角色自定义字段的最新值，供卡片导出使用。"""
    result: dict[int, list[dict[str, str | int]]] = {character_id: [] for character_id in character_ids}
    if not character_ids:
        return result
    timelines = list(
        (
            await db.scalars(
                select(SettingTimeline).where(
                    SettingTimeline.project_id == project_id,
                    SettingTimeline.subject_kind == "character",
                    SettingTimeline.character_id.in_(character_ids),
                )
            )
        ).all()
    )
    snapshots = await resolve_timelines(db, project_id, timelines)
    if not timelines:
        return result
    fields = await db.scalars(
        select(SettingTextField)
        .where(SettingTextField.timeline_id.in_([timeline.id for timeline in timelines]))
        .order_by(SettingTextField.timeline_id, SettingTextField.sort, SettingTextField.id)
    )
    by_timeline = {timeline.id: timeline.character_id for timeline in timelines}
    for field in fields:
        character_id = by_timeline[field.timeline_id]
        assert character_id is not None
        if field.id not in snapshots[field.timeline_id].custom_fields:
            continue
        label = field.label
        if field.definition_id is not None:
            from app.services.setting_field_service import get_definition

            label = (await get_definition(db, project_id, field.definition_id)).label
        result[character_id].append(
            {"field_id": field.id, "label": label, "value": snapshots[field.timeline_id].custom_fields[field.id]}
        )
    return result


async def _materialize_many(db: AsyncSession, project_id: int, timelines: Sequence[SettingTimeline]) -> None:
    """验证并批量同步各主体最新投影；不提交调用方事务。"""
    if not timelines:
        return
    supported = {"world", "character", "relationship", "career", "membership", "organization", "career_entry"}
    unsupported = [t for t in timelines if t.subject_kind not in supported or t.project_id != project_id]
    if unsupported:
        raise ConflictError("设定主体类型或作品不合法", extra={"code": "unsupported_projection"})
    careers = [timeline for timeline in timelines if timeline.subject_kind == "career"]
    relationships = [timeline for timeline in timelines if timeline.subject_kind == "relationship"]
    memberships = [timeline for timeline in timelines if timeline.subject_kind == "membership"]
    if relationships:
        from app.services.relationship_progression_service import validate_relationship_references

        await validate_relationship_references(db, project_id, relationships)
    if careers:
        from app.services.career_progression_service import validate_career_intervals

        await validate_career_intervals(
            db, project_id, {timeline.character_id for timeline in careers if timeline.character_id is not None}
        )
    try:
        snapshots = await resolve_timelines(db, project_id, timelines)
    except ValueError as exc:
        raise ConflictError(
            f"设定变化的先后顺序或内容无法回溯，请调整相关变化：{exc}",
            extra={"code": "progression_lifecycle_conflict"},
        ) from exc
    world_ids = [t.world_setting_id for t in timelines if t.world_setting_id is not None]
    character_ids = [t.character_id for t in timelines if t.subject_kind == "character" and t.character_id is not None]
    worlds = (
        {row.id: row for row in (await db.scalars(select(WorldSetting).where(WorldSetting.id.in_(world_ids)))).all()}
        if world_ids
        else {}
    )
    characters = (
        {row.id: row for row in (await db.scalars(select(Character).where(Character.id.in_(character_ids)))).all()}
        if character_ids
        else {}
    )
    for timeline in timelines:
        snapshot = snapshots[timeline.id]
        if timeline.subject_kind == "world":
            world = worlds.get(timeline.world_setting_id)
            if world is None or world.project_id != project_id:
                raise NotFoundError("世界观条目不存在")
            content = snapshot.fields.get("content")
            if not isinstance(content, str):
                raise ValueError("世界观最新内容不是文本")
            world.content = content
        elif timeline.subject_kind == "character":
            character = characters.get(timeline.character_id)
            if character is None or character.project_id != project_id:
                raise NotFoundError("角色不存在")
            for name in CHARACTER_FIELDS:
                if name in snapshot.fields:
                    setattr(character, name, snapshot.fields[name])
    if relationships:
        from app.services.relationship_progression_service import materialize_relationships

        await materialize_relationships(db, project_id, relationships, snapshots)
    if careers:
        from app.services.career_progression_service import materialize_career_timelines

        await materialize_career_timelines(db, project_id, careers, snapshots)
    if memberships:
        from app.services.membership_progression_service import materialize_memberships

        await materialize_memberships(db, project_id, memberships, snapshots)
    await db.flush()


async def materialize_world_timeline(db: AsyncSession, timeline: SettingTimeline) -> None:
    if timeline.subject_kind != "world":
        raise ValueError("必须提供世界观时间线")
    await _materialize_many(db, timeline.project_id, [timeline])


async def materialize_timeline(db: AsyncSession, timeline: SettingTimeline) -> None:
    await _materialize_many(db, timeline.project_id, [timeline])


async def materialize_latest(db: AsyncSession, timeline: SettingTimeline) -> None:
    """按新事实同步最新业务投影；供分析保存点等组合调用。"""
    await materialize_timeline(db, timeline)


async def materialize_project(db: AsyncSession, project_id: int) -> None:
    """持项目锁时刷新全部主体及历史约束；只 flush，由调用方负责事务。"""
    await db.flush()
    timelines = list(
        (
            await db.scalars(
                select(SettingTimeline)
                .where(SettingTimeline.project_id == project_id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    await _materialize_many(db, project_id, timelines)


async def on_story_positions_changed(db: AsyncSession, project_id: int) -> None:
    """章节位置和外键已 flush 后，同事务重验并重算全部设定。"""
    await db.flush()
    # 数据库的 ON DELETE SET NULL 不会更新已加载的 ORM 外键缓存。只同步已加载
    # 事件的 chapter_id，避免覆盖同一 session 中其他属性；批量查询避免逐行刷新。
    loaded_events = {
        obj.__dict__["id"]: obj
        for obj in db.identity_map.values()
        if isinstance(obj, SettingProgression)
        and obj.__dict__.get("id") is not None
        and (obj.__dict__.get("chapter_id") is not None or obj.__dict__.get("scene_id") is not None)
    }
    if loaded_events:
        rows = await db.execute(
            select(SettingProgression.id, SettingProgression.chapter_id, SettingProgression.scene_id)
            .join(SettingTimeline, SettingTimeline.id == SettingProgression.timeline_id)
            .where(SettingTimeline.project_id == project_id, SettingProgression.id.in_(loaded_events))
        )
        for event_id, chapter_id, scene_id in rows:
            event = loaded_events[event_id]
            if event.__dict__["chapter_id"] != chapter_id:
                set_committed_value(event, "chapter_id", chapter_id)
            if event.__dict__.get("scene_id") != scene_id:
                set_committed_value(event, "scene_id", scene_id)
    await materialize_project(db, project_id)


@_readonly
async def list_timelines(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    *,
    subject_kind: SubjectKind | None = None,
    character_id: int | None = None,
    related_character_id: int | None = None,
    world_setting_id: int | None = None,
    career_id: int | None = None,
    organization_id: int | None = None,
) -> TimelineListOut:
    await _read_project(db, user_id, project_id)
    stmt = select(SettingTimeline).where(SettingTimeline.project_id == project_id)
    for name, value in (
        ("subject_kind", subject_kind),
        ("character_id", character_id),
        ("related_character_id", related_character_id),
        ("world_setting_id", world_setting_id),
        ("career_id", career_id),
        ("organization_id", organization_id),
    ):
        if value is not None:
            stmt = stmt.where(getattr(SettingTimeline, name) == value)
    rows = list((await db.scalars(stmt.order_by(SettingTimeline.id))).all())
    return TimelineListOut(items=[TimelineOut.model_validate(row) for row in rows], total=len(rows))


async def count_unverified_timelines(db: AsyncSession, user_id: int, project_id: int) -> int:
    """统计本作品待校准主体；不加载时间线及基础设定 JSON。"""
    with db.no_autoflush:
        await _read_project(db, user_id, project_id)
        result = await db.scalar(
            select(func.count())
            .select_from(SettingTimeline)
            .where(SettingTimeline.project_id == project_id, SettingTimeline.baseline_status == "legacy_unverified")
        )
    return result or 0


async def create_timeline(db: AsyncSession, user_id: int, project_id: int, data: TimelineCreate) -> TimelineOut:
    await _write_project(db, user_id, project_id)
    if data.subject_kind == "world":
        world = await db.scalar(
            select(WorldSetting).where(WorldSetting.id == data.world_setting_id, WorldSetting.project_id == project_id)
        )
        if world is None:
            raise NotFoundError("世界观条目不存在")
        if await _timeline_for_world(db, world) is not None:
            raise ConflictError("该设定已有时间线")
        timeline = await create_world_timeline(db, world, baseline_status="legacy_unverified")
    elif data.subject_kind == "character":
        character = await db.scalar(
            select(Character).where(Character.id == data.character_id, Character.project_id == project_id)
        )
        if character is None:
            raise NotFoundError("角色不存在")
        existing = await db.scalar(
            select(SettingTimeline.id).where(
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind == "character",
                SettingTimeline.character_id == character.id,
            )
        )
        if existing is not None:
            raise ConflictError("该设定已有时间线")
        timeline = await create_character_timeline(db, character, baseline_status="legacy_unverified")
    else:
        raise BadRequestError("该设定类型不能直接创建时间线")
    await db.commit()
    return TimelineOut.model_validate(timeline)


def _baseline_out(timeline: SettingTimeline) -> BaselineOut:
    return BaselineOut(
        timeline_id=timeline.id,
        baseline=deepcopy(timeline.baseline),
        baseline_status=cast(BaselineStatus, timeline.baseline_status),
        legacy_compat_allowed=timeline.legacy_compat_allowed,
        timeline_revision=timeline.revision,
    )


@_readonly
async def read_baseline(db: AsyncSession, user_id: int, project_id: int, timeline_id: int) -> BaselineOut:
    await _read_project(db, user_id, project_id)
    return _baseline_out(await get_timeline(db, project_id, timeline_id))


async def apply_baseline(
    db: AsyncSession,
    timeline: SettingTimeline,
    data: BaselineUpdate,
    *,
    materialize: bool = True,
    preserve_legacy_eligibility: bool = False,
) -> None:
    """按实际基础值比较并修改，供同事务表单与内部流程调用。"""
    await _reject_shared_baseline_changes(db, timeline, data.changes)
    await _apply_baseline(
        db, timeline, data, materialize=materialize, preserve_legacy_eligibility=preserve_legacy_eligibility,
    )


async def _reject_shared_baseline_changes(
    db: AsyncSession, timeline: SettingTimeline, changes: dict[str, JsonValue],
) -> None:
    kind = timeline.subject_kind
    if kind not in ("character", "world"):
        return
    entry_id = timeline.character_id if kind == "character" else timeline.world_setting_id
    if entry_id is None or not await series_policy.used_local_ids(
        db, timeline.project_id, cast(series_policy.ProjectionKind, kind), {entry_id},
    ):
        return
    # 基线整体落库；即使只改本书键，也不能从 session 缓存合并旧的共享键。
    await db.refresh(timeline, attribute_names=("baseline", "revision", "baseline_status", "legacy_compat_allowed"))
    shared = series_policy.shared_baseline_fields(kind)
    if any(key in shared and value != timeline.baseline.get(key) for key, value in changes.items()):
        raise series_policy.managed_setting_error()


async def _apply_series_baseline(
    db: AsyncSession, timeline: SettingTimeline, changes: dict[str, JsonValue],
) -> None:
    """仅供已持锁的系列投影刷新；不接收外部版本或允许写入本书基础键。"""
    if not changes or not changes.keys() <= series_policy.shared_baseline_fields(timeline.subject_kind):
        raise ValueError("系列投影只能更新共享基础字段")
    if timeline.baseline_status != "confirmed":
        raise ConflictError("书内基础设定尚未校准", extra={"code": "series_baseline_unverified"})
    await _apply_baseline(db, timeline, BaselineUpdate(expected_revision=timeline.revision, changes=changes))


async def _apply_baseline(
    db: AsyncSession,
    timeline: SettingTimeline,
    data: BaselineUpdate,
    *,
    materialize: bool = True,
    preserve_legacy_eligibility: bool = False,
) -> None:
    if preserve_legacy_eligibility:
        if timeline.baseline_status != "legacy_unverified" or not timeline.legacy_compat_allowed or data.confirm:
            raise ConflictError("基础设定已校准或修改，不能按旧分析撤销", extra={"code": "legacy_baseline_changed"})
        event = await db.scalar(
            select(SettingProgression.id).where(SettingProgression.timeline_id == timeline.id).limit(1)
        )
        if event is not None:
            raise ConflictError("该设定已有新的剧情变化，请在设定详情中修正", extra={"code": "legacy_baseline_changed"})
    if data.changes.get("exists") is False:
        if set(data.changes) != {"exists"}:
            raise BadRequestError("解除基础归属时不能附带其他状态")
        merged: dict[str, JsonValue] = {"exists": False}
    else:
        merged = deepcopy(timeline.baseline)
        merged.update(data.changes)
    try:
        validate_baseline(cast(SubjectKind, timeline.subject_kind), merged, subject_identity=_identity(timeline))
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if timeline.subject_kind == "relationship":
        from app.services.relationship_progression_service import validate_relationship_type_reference

        await validate_relationship_type_reference(db, timeline, None, merged)
    await _claim_revision(db, timeline, data.expected_revision, preserve_legacy_eligibility=preserve_legacy_eligibility)
    timeline.baseline = merged
    if data.confirm:
        timeline.baseline_status = "confirmed"
    await db.flush()
    if materialize:
        await materialize_timeline(db, timeline)


async def apply_world_content_update(
    db: AsyncSession,
    world: WorldSetting,
    *,
    content: str | None,
    edit_mode: str | None,
    expected_revision: int | None,
    metadata_changed: bool = False,
) -> None:
    """将旧 world PATCH 的正文与显式初始修正映射到同一个事务。"""
    if content is None and edit_mode is None:
        return
    timeline = await _timeline_for_world(db, world)
    if timeline is None:
        # 兼容尚未接入初始化的旧创建路径；只在写入时建立待校准基础。
        timeline = await create_world_timeline(db, world, baseline_status="legacy_unverified")
    if edit_mode == "baseline":
        if expected_revision is None:
            raise BadRequestError("修正基础设定时请提供当前版本")
        if timeline.revision != expected_revision:
            raise _stale_revision()
        if content is not None and content != timeline.baseline.get("content"):
            await apply_baseline(
                db, timeline, BaselineUpdate(expected_revision=expected_revision, changes={"content": content})
            )
        elif metadata_changed:
            await _claim_revision(db, timeline, expected_revision, metadata_only=True)
        return
    if content is None or content == world.content:
        return
    has_changes = await db.scalar(
        select(SettingProgression.id)
        .where(
            SettingProgression.timeline_id == timeline.id,
            SettingProgression.field_key == "content",
            SettingProgression.active.is_(True),
        )
        .limit(1)
    )
    if has_changes is not None:
        await _reject_shared_baseline_changes(db, timeline, {"content": content})
        raise ConflictError(
            "该字段已有剧情变化，请选择修正基础设定或记录新变化",
            extra={"code": "progression_intent_required"},
        )
    await apply_baseline(
        db, timeline, BaselineUpdate(expected_revision=timeline.revision, changes={"content": content})
    )


async def apply_character_fields_update(
    db: AsyncSession,
    character: Character,
    values: dict[str, JsonValue],
    *,
    edit_mode: str | None,
    expected_revision: int | None,
) -> None:
    """消费角色时间线字段；调用方在同一事务内再保存名字等全书资料。"""
    provided = {name: values.pop(name) for name in CHARACTER_FIELDS if name in values and values[name] is not None}
    # 保持既有 PATCH 的 null=不修改语义，不将这些键交给 setattr。
    for name in CHARACTER_FIELDS:
        values.pop(name, None)
    if not provided and edit_mode is None:
        return
    timeline = await db.scalar(
        select(SettingTimeline).where(
            SettingTimeline.project_id == character.project_id,
            SettingTimeline.subject_kind == "character",
            SettingTimeline.character_id == character.id,
        )
    )
    if timeline is None:
        timeline = await create_character_timeline(db, character, baseline_status="legacy_unverified")
    if edit_mode == "baseline":
        if expected_revision is None:
            raise BadRequestError("修正基础设定时请提供当前版本")
        if timeline.revision != expected_revision:
            raise _stale_revision()
        changes = {name: value for name, value in provided.items() if value != timeline.baseline.get(name)}
    else:
        changes = {name: value for name, value in provided.items() if value != getattr(character, name)}
        if changes:
            has_changes = await db.scalar(
                select(SettingProgression.id)
                .where(
                    SettingProgression.timeline_id == timeline.id,
                    SettingProgression.field_key.in_(changes),
                    SettingProgression.active.is_(True),
                )
                .limit(1)
            )
            if has_changes is not None:
                await _reject_shared_baseline_changes(db, timeline, changes)
                raise ConflictError(
                    "该字段已有剧情变化，请选择修正基础设定或记录新变化",
                    extra={"code": "progression_intent_required"},
                )
    if changes:
        await apply_baseline(
            db,
            timeline,
            BaselineUpdate(expected_revision=timeline.revision, changes=changes),
        )
    elif edit_mode == "baseline" and any(
        value is not None and value != getattr(character, name) for name, value in values.items()
    ):
        assert expected_revision is not None
        await _claim_revision(db, timeline, expected_revision, metadata_only=True)


async def update_baseline(
    db: AsyncSession, user_id: int, project_id: int, timeline_id: int, data: BaselineUpdate
) -> BaselineOut:
    await _write_project(db, user_id, project_id)
    timeline = await get_timeline(db, project_id, timeline_id)
    await apply_baseline(db, timeline, data)
    await db.commit()
    return _baseline_out(timeline)


async def _chapter_for_anchor(db: AsyncSession, project_id: int, chapter_id: int) -> Chapter:
    chapter = await db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id))
    if chapter is None:
        raise BadRequestError("章节不存在或不属于该作品")
    return chapter


async def _validate_progression(
    db: AsyncSession,
    timeline: SettingTimeline,
    data: ProgressionCreate,
    *,
    value_is_canonical: bool = False,
    prior_value: object = None,
) -> tuple[str, int | None, JsonValue]:
    if data.sort_order < 0:
        raise BadRequestError("变化顺序不能小于 0")
    canonical = deepcopy(data.value)
    if data.custom_field_id is not None:
        field = await db.scalar(
            select(SettingTextField).where(
                SettingTextField.id == data.custom_field_id, SettingTextField.timeline_id == timeline.id
            )
        )
        if field is None:
            raise BadRequestError("自定义字段不存在或不属于该设定")
        from app.services.setting_field_service import check_fields_revision, encode_field_value

        if field.definition_id is not None:
            await check_fields_revision(db, timeline.project_id, data.expected_fields_revision)
        canonical = await encode_field_value(
            db,
            timeline,
            field,
            data.value,
            operation=data.operation,
            prior_value=prior_value,
            preserve_existing=value_is_canonical,
        )
    else:
        try:
            validate_progression_value(
                cast(SubjectKind, timeline.subject_kind),
                data.field_key,
                data.operation,
                data.value,
                custom_field_id=None,
                subject_identity=_identity(timeline),
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
    if timeline.subject_kind == "relationship":
        from app.services.relationship_progression_service import validate_relationship_type_reference

        await validate_relationship_type_reference(db, timeline, data.field_key, data.value)
    if data.anchor_kind == "planned":
        assert data.chapter_no is not None
        return f"第{data.chapter_no}章（计划）", data.chapter_no, canonical
    if data.anchor_kind == "scene":
        position = await resolve_scene_position(db, timeline.project_id, data.scene_id, data.boundary)
        title = await db.scalar(select(Scene.title).where(Scene.id == data.scene_id))
        return f"第{position.chapter_no}章 场景{position.scene_order + 1} {title}"[:250], position.chapter_no, canonical
    assert data.chapter_id is not None
    chapter = await _chapter_for_anchor(db, timeline.project_id, data.chapter_id)
    return f"第{chapter.chapter_no}章 {chapter.title}"[:250], chapter.chapter_no, canonical


async def apply_progression(
    db: AsyncSession,
    timeline: SettingTimeline,
    data: ProgressionCreate,
    *,
    source: ProgressionSource = "manual",
    protection: ProgressionProtection = "manual",
    source_change_id: int | None = None,
    source_content_hash: str | None = None,
    materialize: bool = True,
    value_is_canonical: bool = False,
) -> SettingProgression:
    """持有项目写锁时创建一条事实；不 commit，可用于单项 SAVEPOINT。"""
    label, _, canonical = await _validate_progression(db, timeline, data, value_is_canonical=value_is_canonical)
    data = data.model_copy(update={"value": canonical})
    if source_change_id is not None:
        stmt = select(SettingProgression).where(
            SettingProgression.source_change_id == source_change_id,
            SettingProgression.timeline_id == timeline.id,
            SettingProgression.field_key == data.field_key,
            SettingProgression.custom_field_id == data.custom_field_id,
        )
        prior = await db.scalar(stmt)
        if prior is not None:
            if (
                prior.operation != data.operation
                or prior.value != data.value
                or prior.anchor_kind != data.anchor_kind
                or prior.chapter_id != data.chapter_id
                or prior.chapter_no != data.chapter_no
                or prior.scene_id != data.scene_id
                or prior.boundary != data.boundary
                or prior.protection != protection
            ):
                raise ConflictError("同一分析结果已对应不同的设定变化")
            return prior
    await _claim_revision(db, timeline, data.expected_revision)
    values = data.model_dump(exclude={"expected_revision", "expected_fields_revision"})
    event = SettingProgression(
        timeline_id=timeline.id,
        **values,
        anchor_label=label,
        source=source,
        protection=protection,
        source_change_id=source_change_id,
        source_content_hash=source_content_hash,
    )
    db.add(event)
    await db.flush()
    if materialize:
        await materialize_latest(db, timeline)
    return event


async def revoke_progression(
    db: AsyncSession,
    timeline: SettingTimeline,
    event: SettingProgression,
    expected_revision: int,
    *,
    protect: bool = True,
    materialize: bool = True,
) -> None:
    """停用原事件而非追加反向事件；protect=False 供分析组替代。"""
    if event.timeline_id != timeline.id or not event.active:
        raise NotFoundError("设定变化不存在")
    await _claim_revision(db, timeline, expected_revision)
    event.active = False
    event.revision += 1
    event.reverted_at = datetime.now(UTC)
    if protect:
        event.protection = "manual"
    await db.flush()
    if materialize:
        await materialize_latest(db, timeline)


async def _event_out(db: AsyncSession, project_id: int, event: SettingProgression) -> ProgressionOut:
    chapter_no = None
    if event.chapter_id is not None:
        chapter_no = await db.scalar(
            select(Chapter.chapter_no).where(Chapter.id == event.chapter_id, Chapter.project_id == project_id)
        )
    return (await _public_events(db, project_id, [(event, chapter_no)]))[0]


async def _public_events(db: AsyncSession, project_id: int, rows) -> list[ProgressionOut]:
    """列表和所有变更响应共用解码及显示元数据，内部句柄永不外露。"""
    from app.services import setting_catalog_service as catalog
    from app.services import setting_field_service as fields
    from app.services.setting_field_values import decode_public_value

    custom_ids = {event.custom_field_id for event, _ in rows if event.custom_field_id is not None}
    bindings = {
        field.id: field
        for field in await db.scalars(
            select(SettingTextField).where(
                SettingTextField.id.in_(custom_ids),
            )
        )
    }
    specs = await fields.load_field_specs(db, project_id, list(bindings.values()))
    definitions, _ = await fields._metadata(db, project_id)
    definitions = {definition.id: definition for definition in definitions}
    targets = await fields._handle_targets(db, project_id)
    entries = await catalog.get_entries(db, project_id, list(targets.values()))
    result = []
    scene_positions = await load_scene_positions(
        db, project_id, {event.scene_id for event, _ in rows if event.scene_id is not None}
    )
    for event, chapter_no in rows:
        scene_position = scene_positions.get(event.scene_id)
        values = {
            "resolved_chapter_no": (scene_position.chapter_no if scene_position else None)
            if event.anchor_kind == "scene" else event.chapter_no if event.anchor_kind == "planned" else chapter_no,
            "resolved_scene_order": scene_position.scene_order if scene_position else None,
            "orphaned": (event.anchor_kind == "chapter" and chapter_no is None)
            or (event.anchor_kind == "scene" and scene_position is None),
        }
        if event.custom_field_id is not None:
            field = bindings.get(event.custom_field_id)
            if field is None:
                raise ConflictError("设定变化缺少字段身份", extra={"code": "invalid_field_graph"})
            spec = specs[field.id]
            public = decode_public_value(spec, event.value, reference_targets=targets)
            definition = definitions.get(field.definition_id)
            values.update(
                value=public,
                field_type=spec.field_type,
                definition_id=field.definition_id,
                field_label=definition.label if definition else field.label,
            )
            if spec.field_type == "reference" and public is not None:
                entry = entries[public["kind"], public["id"]]
                values.update(reference=entry.reference, reference_name=entry.name, reference_type=entry.type)
        result.append(ProgressionOut.model_validate(event).model_copy(update=values))
    return result


async def _get_event(db: AsyncSession, timeline_id: int, event_id: int) -> SettingProgression:
    event = await db.scalar(
        select(SettingProgression).where(
            SettingProgression.id == event_id,
            SettingProgression.timeline_id == timeline_id,
            SettingProgression.active.is_(True),
        )
    )
    if event is None:
        raise NotFoundError("设定变化不存在")
    return event


@_readonly
async def list_progressions(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    timeline_id: int,
    *,
    offset: int = 0,
    limit: int = 50,
    field: str | None = None,
    include_inactive: bool = False,
) -> ProgressionListOut:
    await _read_project(db, user_id, project_id)
    timeline = await get_timeline(db, project_id, timeline_id)
    from app.services.setting_field_service import fields_revision

    before_fields = await fields_revision(db, project_id)
    before_timeline = timeline.revision
    predicates = [SettingProgression.timeline_id == timeline_id]
    if not include_inactive:
        predicates.append(SettingProgression.active.is_(True))
    if field:
        if field.startswith("custom:"):
            try:
                field_id = int(field.removeprefix("custom:"))
            except ValueError as exc:
                raise BadRequestError("字段筛选不合法") from exc
            if field_id <= 0:
                raise BadRequestError("字段筛选不合法")
            predicates.append(SettingProgression.custom_field_id == field_id)
        else:
            predicates.append(SettingProgression.field_key == field)
    total = await db.scalar(select(func.count()).select_from(SettingProgression).where(*predicates))
    scene_chapter = aliased(Chapter)
    resolved_no = case(
        (SettingProgression.anchor_kind == "planned", SettingProgression.chapter_no),
        (SettingProgression.anchor_kind == "scene", scene_chapter.chapter_no), else_=Chapter.chapter_no
    )
    rows = await db.execute(
        select(SettingProgression, Chapter.chapter_no)
        .outerjoin(Chapter, and_(Chapter.id == SettingProgression.chapter_id, Chapter.project_id == project_id))
        .outerjoin(Scene, and_(Scene.id == SettingProgression.scene_id, Scene.active.is_(True)))
        .outerjoin(scene_chapter, and_(scene_chapter.id == Scene.chapter_id, scene_chapter.project_id == project_id))
        .where(*predicates)
        .order_by(
            case((resolved_no.is_(None), 1), else_=0),
            resolved_no,
            case((SettingProgression.anchor_kind == "scene", 1),
                 (SettingProgression.boundary == "start", 0), else_=2),
            case((SettingProgression.anchor_kind == "scene", scene_chapter.id), else_=0),
            case((SettingProgression.anchor_kind == "scene", Scene.sort), else_=0),
            case((SettingProgression.boundary == "start", 0), else_=1),
            case((SettingProgression.protection == "automatic", 0), else_=1),
            SettingProgression.sort_order,
            SettingProgression.id,
        )
        .offset(offset)
        .limit(limit)
    )
    items = await _public_events(db, project_id, list(rows))
    await _check_read_revisions(db, project_id, timeline.id, before_timeline, before_fields)
    return ProgressionListOut(
        items=items, total=total or 0, timeline_revision=before_timeline, fields_revision=before_fields
    )


async def create_progression(
    db: AsyncSession, user_id: int, project_id: int, timeline_id: int, data: ProgressionCreate
) -> ProgressionMutationOut:
    from app.services.setting_field_service import _editing, fields_revision

    async with _editing(db, user_id, project_id) as audit:
        timeline = await get_timeline(db, project_id, timeline_id)
        event = await apply_progression(db, timeline, data)
        item = await _event_out(db, project_id, event)
        catalog_revision = await fields_revision(db, project_id)
        await audit.commit()

        return ProgressionMutationOut(item=item, timeline_revision=timeline.revision, fields_revision=catalog_revision)


@_readonly
async def read_progression(
    db: AsyncSession, user_id: int, project_id: int, timeline_id: int, event_id: int
) -> ProgressionMutationOut:
    """详情包含停用项，便于发生版本冲突时比较当前事实。"""
    await _read_project(db, user_id, project_id)
    timeline = await get_timeline(db, project_id, timeline_id)
    event = await db.scalar(
        select(SettingProgression).where(
            SettingProgression.id == event_id, SettingProgression.timeline_id == timeline_id
        )
    )
    if event is None:
        raise NotFoundError("设定变化不存在")
    from app.services.setting_field_service import fields_revision

    before_fields = await fields_revision(db, project_id)
    before_timeline = timeline.revision
    item = await _event_out(db, project_id, event)
    await _check_read_revisions(db, project_id, timeline.id, before_timeline, before_fields)
    return ProgressionMutationOut(item=item, timeline_revision=before_timeline, fields_revision=before_fields)


async def update_progression(
    db: AsyncSession, user_id: int, project_id: int, timeline_id: int, event_id: int, data: ProgressionUpdate
) -> ProgressionMutationOut:
    from app.services.setting_field_service import _editing, fields_revision

    async with _editing(db, user_id, project_id) as audit:
        timeline = await get_timeline(db, project_id, timeline_id)
        event = await _get_event(db, timeline.id, event_id)
        values = {
            key: getattr(event, key)
            for key in (
                "field_key",
                "custom_field_id",
                "operation",
                "value",
                "anchor_kind",
                "chapter_id",
                "chapter_no",
                "scene_id",
                "boundary",
                "sort_order",
                "note",
            )
        }
        values.update(data.model_dump(exclude_unset=True, exclude={"expected_revision", "expected_fields_revision"}))
        try:
            merged = ProgressionCreate(
                expected_revision=data.expected_revision,
                expected_fields_revision=data.expected_fields_revision,
                **values,
            )
        except ValidationError as exc:
            raise BadRequestError("设定变化字段或章节位置不合法") from exc
        # 分析来源的事件靠 (source_change_id, 字段引用) 建立部分唯一索引，重跑时也依赖该关联定位旧组。
        # 改字段引用会撞索引（未捕获的 IntegrityError→500）或让来源指向错误字段，统一以 409 拒绝。
        if event.source_change_id is not None and (
            merged.field_key != event.field_key or merged.custom_field_id != event.custom_field_id
        ):
            raise ConflictError(
                "分析来源的设定变化不支持修改字段引用，请删除后重建",
                extra={"code": "source_field_immutable"},
            )
        from app.services.setting_field_service import check_fields_revision, fields_revision

        if event.custom_field_id is not None:
            original_field = await _get_text_field(db, timeline.id, event.custom_field_id)
            if original_field.definition_id is not None:
                await check_fields_revision(db, project_id, data.expected_fields_revision)
        label, _, canonical = await _validate_progression(
            db,
            timeline,
            merged,
            value_is_canonical="value" not in data.model_fields_set and merged.custom_field_id == event.custom_field_id,
            prior_value=event.value if merged.custom_field_id == event.custom_field_id else None,
        )
        merged = merged.model_copy(update={"value": canonical})
        await _claim_revision(db, timeline, data.expected_revision)
        for key, value in merged.model_dump(exclude={"expected_revision", "expected_fields_revision"}).items():
            setattr(event, key, value)
        event.anchor_label = label
        event.protection = "manual"
        event.revision += 1
        try:
            await db.flush()
            await materialize_latest(db, timeline)
            # onupdate 生成的时间戳在 flush 后可能处于过期状态，显式异步刷新以供 DTO 使用。
            await db.refresh(event, attribute_names=["updated_at"])
            item = await _event_out(db, project_id, event)
            catalog_revision = await fields_revision(db, project_id)
            await audit.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise ConflictError("设定变化与已有记录冲突，请刷新后重试", extra={"code": "progression_conflict"}) from exc
        return ProgressionMutationOut(item=item, timeline_revision=timeline.revision, fields_revision=catalog_revision)


async def delete_progression(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    timeline_id: int,
    event_id: int,
    expected_revision: int,
    expected_fields_revision: int | None = None,
) -> RevisionOut:
    from app.services.setting_field_service import _editing, fields_revision

    async with _editing(db, user_id, project_id) as audit:
        timeline = await get_timeline(db, project_id, timeline_id)
        event = await _get_event(db, timeline.id, event_id)
        from app.services.setting_field_service import check_fields_revision, fields_revision

        if event.custom_field_id is not None:
            field = await _get_text_field(db, timeline.id, event.custom_field_id)
            if field.definition_id is not None:
                await check_fields_revision(db, project_id, expected_fields_revision)
        await revoke_progression(db, timeline, event, expected_revision)
        catalog_revision = await fields_revision(db, project_id)
        await audit.commit()
        return RevisionOut(timeline_revision=timeline.revision, fields_revision=catalog_revision)


async def read_effective(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    timeline_id: int,
    *,
    chapter_id: int | None,
    chapter_no: int | None,
    boundary: Boundary,
    scene_id: int | None = None,
) -> EffectiveOut:
    with db.no_autoflush:
        await _read_project(db, user_id, project_id)
        timeline = await get_timeline(db, project_id, timeline_id)
        from app.services import setting_catalog_service as catalog
        from app.services import setting_field_service as fields
        from app.services.setting_field_values import decode_public_value

        before_fields = await fields.fields_revision(db, project_id)
        before_timeline = timeline.revision
        position = await resolve_story_position(
            db, project_id, chapter_id=chapter_id, chapter_no=chapter_no, scene_id=scene_id, boundary=boundary
        )
        result = (await resolve_timelines(db, project_id, [timeline], position))[timeline.id]
        bindings = list(await db.scalars(select(SettingTextField).where(SettingTextField.timeline_id == timeline.id)))
        specs = await fields.load_field_specs(db, project_id, bindings)
        targets = await fields._handle_targets(db, project_id)
        target_entries = await catalog.get_entries(db, project_id, list(targets.values()))
        definitions, _ = await fields._metadata(db, project_id)
        definitions = {d.id: d for d in definitions}
        labels, reference_names, reference_types, typed_values = {}, {}, {}, {}
        for field in bindings:
            public = decode_public_value(specs[field.id], result.field_values[field.id], reference_targets=targets)
            typed_values[field.id] = {"field_type": specs[field.id].field_type, "value": public}
            definition = definitions.get(field.definition_id)
            labels[field.id] = definition.label if definition else field.label
            if specs[field.id].field_type == "reference" and public is not None:
                target = target_entries[public["kind"], public["id"]]
                reference_names[field.id], reference_types[field.id] = target.name, target.type
        await _check_read_revisions(db, project_id, timeline.id, before_timeline, before_fields)
        return EffectiveOut(
            timeline_id=timeline.id,
            position=position,
            fields=result.fields,
            custom_fields=result.custom_fields,
            field_values=typed_values,
            applied_event_ids=list(result.applied_event_ids),
            orphaned_event_ids=list(result.orphaned_event_ids),
            baseline_status=result.baseline_status,
            timeline_revision=before_timeline,
            fields_revision=before_fields,
            field_labels=labels,
            reference_names=reference_names,
            reference_types=reference_types,
        )


async def _get_text_field(db: AsyncSession, timeline_id: int, field_id: int) -> SettingTextField:
    field = await db.scalar(
        select(SettingTextField).where(SettingTextField.id == field_id, SettingTextField.timeline_id == timeline_id)
    )
    if field is None:
        raise NotFoundError("自定义字段不存在")
    return field


async def _require_unique_label(
    db: AsyncSession, timeline_id: int, label: str, *, exclude_id: int | None = None
) -> None:
    stmt = select(SettingTextField.id).where(
        SettingTextField.timeline_id == timeline_id, SettingTextField.label == label
    )
    if exclude_id is not None:
        stmt = stmt.where(SettingTextField.id != exclude_id)
    if await db.scalar(stmt) is not None:
        raise ConflictError("该设定已存在同名自定义字段", extra={"code": "duplicate_field"})


@_readonly
async def list_text_fields(db: AsyncSession, user_id: int, project_id: int, timeline_id: int) -> TextFieldListOut:
    await _read_project(db, user_id, project_id)
    timeline = await get_timeline(db, project_id, timeline_id)
    fields = list(
        (
            await db.scalars(
                select(SettingTextField)
                .where(SettingTextField.timeline_id == timeline_id, SettingTextField.definition_id.is_(None))
                .order_by(SettingTextField.sort, SettingTextField.id)
            )
        ).all()
    )
    return TextFieldListOut(
        items=[TextFieldOut.model_validate(field) for field in fields],
        total=len(fields),
        timeline_revision=timeline.revision,
    )


async def create_text_field(
    db: AsyncSession, user_id: int, project_id: int, timeline_id: int, data: TextFieldCreate
) -> TextFieldMutationOut:
    await _write_project(db, user_id, project_id)
    timeline = await get_timeline(db, project_id, timeline_id)
    if timeline.subject_kind not in ("character", "world"):
        raise BadRequestError("该设定不支持自定义文本字段")
    label = _clean_label(data.label)
    await _require_unique_label(db, timeline.id, label)
    await _claim_revision(db, timeline, data.expected_revision)
    field = SettingTextField(timeline_id=timeline.id, label=label, base_value=data.base_value, sort=data.sort)
    db.add(field)
    await db.flush()
    item = TextFieldOut.model_validate(field)
    await db.commit()
    return TextFieldMutationOut(item=item, timeline_revision=timeline.revision)


async def update_text_field(
    db: AsyncSession, user_id: int, project_id: int, timeline_id: int, field_id: int, data: TextFieldUpdate
) -> TextFieldMutationOut:
    await _write_project(db, user_id, project_id)
    timeline = await get_timeline(db, project_id, timeline_id)
    field = await _get_text_field(db, timeline.id, field_id)
    if field.definition_id is not None:
        raise ConflictError("共享字段请在作品字段配置和值入口维护", extra={"code": "shared_field_requires_typed_api"})
    values = data.model_dump(exclude_unset=True, exclude={"expected_revision"})
    if not values or any(value is None for value in values.values()):
        raise BadRequestError("请选择有效的字段修改")
    if "label" in values:
        label = _clean_label(cast(str, values["label"]))
        await _require_unique_label(db, timeline.id, label, exclude_id=field.id)
        values["label"] = label
    await _claim_revision(db, timeline, data.expected_revision)
    for key, value in values.items():
        setattr(field, key, value)
    await db.flush()
    item = TextFieldOut.model_validate(field)
    await db.commit()
    return TextFieldMutationOut(item=item, timeline_revision=timeline.revision)


async def delete_text_field(
    db: AsyncSession, user_id: int, project_id: int, timeline_id: int, field_id: int, expected_revision: int
) -> RevisionOut:
    await _write_project(db, user_id, project_id)
    timeline = await get_timeline(db, project_id, timeline_id)
    field = await _get_text_field(db, timeline.id, field_id)
    if field.definition_id is not None:
        raise ConflictError("共享字段请在作品字段配置和值入口维护", extra={"code": "shared_field_requires_typed_api"})
    active = await db.scalar(
        select(SettingProgression.id)
        .where(
            SettingProgression.timeline_id == timeline.id,
            SettingProgression.custom_field_id == field.id,
            SettingProgression.active.is_(True),
        )
        .limit(1)
    )
    if active is not None:
        raise ConflictError("该字段仍有关联的剧情变化，请先删除变化", extra={"code": "referenced_field"})
    await _claim_revision(db, timeline, expected_revision)
    # 已被作者删除的变化不再有效，清理这些行后才能删除 NO ACTION 引用的字段。
    await db.execute(
        delete(SettingProgression).where(
            SettingProgression.timeline_id == timeline.id,
            SettingProgression.custom_field_id == field.id,
            SettingProgression.active.is_(False),
        )
    )
    await db.delete(field)
    await db.commit()
    return RevisionOut(timeline_revision=timeline.revision)
