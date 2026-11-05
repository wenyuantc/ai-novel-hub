"""职业时间线的稳定主体、历史区间约束和最新挂接投影。

职业是否在某章存在由统一设定求值器计算。这里负责同一角色各职业的交叉约束，
以及把求值结果投影到现有 CharacterCareer 表；任何入口均不得自行维护另一套回放器。
"""

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from typing import cast

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Career, CareerHistory, CareerStage, Chapter, Character, CharacterCareer
from app.models.progressions import SettingProgression, SettingTimeline
from app.schemas.progression import StoryPosition
from app.services.setting_progression_resolver import (
    EffectiveSnapshot,
    ProgressionEvent,
    resolve_progressions,
    validate_baseline,
    validate_progression_value,
)


async def get_or_create_career_timeline(db: AsyncSession, character: Character, career: Career) -> SettingTimeline:
    """按角色和职业获取稳定主体；只在写操作中初始化，不提交。"""
    if character.project_id != career.project_id:
        raise BadRequestError("职业不存在或不属于该项目")
    timeline = await db.scalar(
        select(SettingTimeline).where(
            SettingTimeline.project_id == character.project_id,
            SettingTimeline.subject_kind == "career",
            SettingTimeline.character_id == character.id,
            SettingTimeline.career_id == career.id,
        )
    )
    if timeline is not None:
        return timeline
    current = await db.scalar(
        select(CharacterCareer).where(
            CharacterCareer.character_id == character.id,
            CharacterCareer.career_id == career.id,
        )
    )
    baseline = (
        {"exists": True, "role": current.role, "stage_id": current.stage_id, "note": current.note}
        if current is not None
        else {"exists": False}
    )
    timeline = SettingTimeline(
        project_id=character.project_id,
        subject_kind="career",
        character_id=character.id,
        career_id=career.id,
        baseline=baseline,
        baseline_status="legacy_unverified" if current is not None else "confirmed",
        legacy_compat_allowed=current is not None,
    )
    db.add(timeline)
    await db.flush()
    return timeline


async def append_analysis_audit(
    db: AsyncSession,
    character_id: int,
    career_id: int,
    changes: dict,
    chapter_no: int,
    note: str,
) -> CareerHistory:
    """仅追加分析审计；不写剧情事件、不更新投影、不提交。"""
    character = await db.get(Character, character_id)
    career = await db.get(Career, career_id)
    if character is None or career is None or character.project_id != career.project_id:
        raise BadRequestError("职业不存在或不属于该项目")
    history = CareerHistory(
        character_id=character_id,
        career_id=career_id,
        career_name=career.name,
        source="analysis",
        chapter_no=chapter_no,
        changes=changes,
        note=(note or "")[:200],
    )
    db.add(history)
    await db.flush()
    return history


def _stage_for_snapshot(fields: Mapping[str, object], career_id: int, stage_careers: Mapping[int, int]) -> int | None:
    if fields.get("exists") is not True:
        return None
    stage_id = fields.get("stage_id")
    if type(stage_id) is not int or stage_careers.get(stage_id) != career_id:
        raise BadRequestError("阶段不属于该职业")
    return stage_id


def _ensure_limits(counts: Mapping[tuple[int, str], int], character_ids: Collection[int]) -> None:
    # 使用现有错误文案；避免此模块在 import 时与 career_service 循环依赖。
    from app.services.career_service import MAIN_LIMIT, MAX_MAIN_CAREERS, MAX_SUB_CAREERS, SUB_LIMIT

    for character_id in character_ids:
        if counts.get((character_id, "main"), 0) > MAX_MAIN_CAREERS:
            raise ConflictError(MAIN_LIMIT)
        if counts.get((character_id, "sub"), 0) > MAX_SUB_CAREERS:
            raise ConflictError(SUB_LIMIT)


async def validate_career_intervals(
    db: AsyncSession,
    project_id: int,
    character_ids: Collection[int] | None = None,
    *,
    kind_overrides: Mapping[int, str] | None = None,
) -> None:
    """对基础、每个受影响章初／章末验证职业数量及阶段引用，不提交。

    事实和章节一次批量加载；每个位置仅重算发生变化的主体，求值始终调用共享
    resolve_progressions。尚无时间线的旧挂接按当前基础处理，不静默漏算。
    """
    if character_ids is not None and not character_ids:
        return
    await db.flush()
    stmt = select(SettingTimeline).where(
        SettingTimeline.project_id == project_id,
        SettingTimeline.subject_kind == "career",
    )
    if character_ids is not None:
        stmt = stmt.where(SettingTimeline.character_id.in_(character_ids))
    timelines = list((await db.scalars(stmt.execution_options(populate_existing=True))).all())
    assignments_stmt = (
        select(CharacterCareer)
        .join(Career, Career.id == CharacterCareer.career_id)
        .where(Career.project_id == project_id)
    )
    if character_ids is not None:
        assignments_stmt = assignments_stmt.where(CharacterCareer.character_id.in_(character_ids))
    assignments = list((await db.scalars(assignments_stmt)).all())
    if not timelines and not assignments:
        return

    career_ids = {t.career_id for t in timelines} | {a.career_id for a in assignments}
    careers = {
        career.id: career
        for career in (
            await db.scalars(select(Career).where(Career.project_id == project_id, Career.id.in_(career_ids)))
        ).all()
    }
    if len(careers) != len(career_ids):
        raise BadRequestError("职业不存在或不属于该项目")
    stage_rows = await db.execute(
        select(CareerStage.id, CareerStage.career_id).where(CareerStage.career_id.in_(career_ids))
    )
    stage_careers = dict(stage_rows.all())
    kinds = {career_id: career.kind for career_id, career in careers.items()}
    overrides = kind_overrides or {}
    for career_id, kind in overrides.items():
        if career_id in kinds:
            if kind not in ("main", "sub"):
                raise BadRequestError("职业类别不合法")
            kinds[career_id] = kind

    by_id = {timeline.id: timeline for timeline in timelines}
    timeline_pairs = {(t.character_id, t.career_id) for t in timelines}
    counts: dict[tuple[int, str], int] = defaultdict(int)
    state: dict[int, bool] = {}
    affected: set[int] = set()
    for timeline in timelines:
        assert timeline.character_id is not None and timeline.career_id is not None
        try:
            validate_baseline("career", timeline.baseline)
        except ValueError as exc:
            raise ConflictError(f"职业基础设定无法回溯：{exc}") from exc
        exists = timeline.baseline.get("exists") is True
        if exists:
            _stage_for_snapshot(timeline.baseline, timeline.career_id, stage_careers)
            if timeline.career_id not in overrides and timeline.baseline.get("role") != kinds[timeline.career_id]:
                raise ConflictError("职业基础类别与职业定义不一致")
            counts[(timeline.character_id, kinds[timeline.career_id])] += 1
        state[timeline.id] = exists
        affected.add(timeline.character_id)

    for assignment in assignments:
        if (assignment.character_id, assignment.career_id) in timeline_pairs:
            continue
        _stage_for_snapshot({"exists": True, "stage_id": assignment.stage_id}, assignment.career_id, stage_careers)
        counts[(assignment.character_id, kinds[assignment.career_id])] += 1
        affected.add(assignment.character_id)
    _ensure_limits(counts, affected)

    events_by_timeline: dict[int, list[ProgressionEvent]] = defaultdict(list)
    changed_at: dict[tuple[int, str], set[int]] = defaultdict(set)
    if timelines:
        # 复用统一事件转换，避免两处对章节/排序字段定义不同。
        from app.services.setting_progression_service import _as_event

        rows = await db.execute(
            select(SettingProgression, Chapter.chapter_no)
            .outerjoin(Chapter, and_(Chapter.id == SettingProgression.chapter_id, Chapter.project_id == project_id))
            .where(SettingProgression.timeline_id.in_(list(by_id)))
            .execution_options(populate_existing=True)
        )
        for row, linked_number in rows:
            timeline = by_id[row.timeline_id]
            assert timeline.career_id is not None
            try:
                validate_progression_value("career", row.field_key, cast(str, row.operation), row.value)
            except ValueError as exc:
                raise ConflictError(f"职业变化无法回溯：{exc}") from exc
            # 即便记录已停用／失去章节，其被保留的稳定阶段引用也不能指向另一职业。
            if row.field_key == "stage_id":
                _stage_for_snapshot({"exists": True, "stage_id": row.value}, timeline.career_id, stage_careers)
            elif row.field_key == "presence" and isinstance(row.value, dict) and row.value.get("exists") is True:
                _stage_for_snapshot(
                    {"exists": True, "stage_id": row.value["state"]["stage_id"]}, timeline.career_id, stage_careers
                )
            if not row.active:
                continue
            event = _as_event(row, linked_number)
            events_by_timeline[timeline.id].append(event)
            number = row.chapter_no if row.anchor_kind == "planned" else linked_number
            if number is not None:
                changed_at[(number, row.boundary)].add(timeline.id)

    for (number, boundary), changed_ids in sorted(
        changed_at.items(), key=lambda item: (item[0][0], item[0][1] == "end")
    ):
        changed_characters: set[int] = set()
        for timeline_id in changed_ids:
            timeline = by_id[timeline_id]
            assert timeline.character_id is not None and timeline.career_id is not None
            try:
                snapshot = resolve_progressions(
                    "career",
                    timeline.baseline,
                    {},
                    events_by_timeline[timeline_id],
                    StoryPosition(chapter_no=number, boundary=cast(str, boundary)),
                )
            except ValueError as exc:
                raise ConflictError(
                    f"职业变化在该章节无法回溯：{exc}", extra={"code": "progression_lifecycle_conflict"}
                ) from exc
            exists = snapshot.fields.get("exists") is True
            if exists:
                _stage_for_snapshot(snapshot.fields, timeline.career_id, stage_careers)
                if timeline.career_id not in overrides and snapshot.fields.get("role") != kinds[timeline.career_id]:
                    raise ConflictError("职业剧情类别与职业定义不一致")
            if exists != state[timeline_id]:
                counts[(timeline.character_id, kinds[timeline.career_id])] += 1 if exists else -1
                state[timeline_id] = exists
            changed_characters.add(timeline.character_id)
        # 同一边界内多条变化作为一个位置检查，避免同章先更换再解除的瞬态误报。
        _ensure_limits(counts, changed_characters)


async def materialize_career_timelines(
    db: AsyncSession,
    project_id: int,
    timelines: Sequence[SettingTimeline],
    snapshots: Mapping[int, EffectiveSnapshot],
) -> None:
    """把已验证的职业最新快照写回现有挂接表；不写审计、不提交。"""
    if not timelines:
        return
    if any(t.project_id != project_id or t.subject_kind != "career" for t in timelines):
        raise ValueError("职业时间线必须属于同一作品")
    character_ids = {t.character_id for t in timelines}
    career_ids = {t.career_id for t in timelines}
    characters = {
        character.id: character
        for character in (
            await db.scalars(
                select(Character).where(Character.project_id == project_id, Character.id.in_(character_ids))
            )
        ).all()
    }
    careers = {
        career.id: career
        for career in (
            await db.scalars(select(Career).where(Career.project_id == project_id, Career.id.in_(career_ids)))
        ).all()
    }
    stage_rows = await db.execute(
        select(CareerStage.id, CareerStage.career_id).where(CareerStage.career_id.in_(career_ids))
    )
    stage_careers = dict(stage_rows.all())
    current = {
        (row.character_id, row.career_id): row
        for row in (
            await db.scalars(
                select(CharacterCareer).where(
                    CharacterCareer.character_id.in_(character_ids), CharacterCareer.career_id.in_(career_ids)
                )
            )
        ).all()
    }
    for timeline in timelines:
        assert timeline.character_id is not None and timeline.career_id is not None
        if timeline.character_id not in characters or timeline.career_id not in careers:
            raise NotFoundError("角色或职业不存在")
        if timeline.id not in snapshots:
            raise ValueError("缺少职业时间线求值结果")
        fields = snapshots[timeline.id].fields
        key = (timeline.character_id, timeline.career_id)
        assignment = current.get(key)
        if fields.get("exists") is not True:
            if assignment is not None:
                await db.delete(assignment)
            continue
        stage_id = _stage_for_snapshot(fields, timeline.career_id, stage_careers)
        assert stage_id is not None
        role = careers[timeline.career_id].kind
        if fields.get("role") != role:
            raise ConflictError("职业剧情类别与职业定义不一致")
        note = fields.get("note", "")
        if not isinstance(note, str) or len(note) > 200:
            raise BadRequestError("职业备注不合法")
        if assignment is None:
            assignment = CharacterCareer(character_id=key[0], career_id=key[1], role=role, stage_id=stage_id, note=note)
            db.add(assignment)
            current[key] = assignment
        else:
            assignment.role, assignment.stage_id, assignment.note = role, stage_id, note
    await db.flush()


async def protected_stage_ids(db: AsyncSession, career_id: int) -> set[int]:
    """收集仍被职业基础或任何保留变化引用的阶段，含停用和孤儿事件。"""
    timelines = list(
        (
            await db.scalars(
                select(SettingTimeline).where(
                    SettingTimeline.subject_kind == "career", SettingTimeline.career_id == career_id
                )
            )
        ).all()
    )
    stage_ids: set[int] = set()
    for timeline in timelines:
        stage_id = timeline.baseline.get("stage_id")
        if type(stage_id) is int:
            stage_ids.add(stage_id)
    if not timelines:
        return stage_ids
    events = await db.scalars(
        select(SettingProgression).where(SettingProgression.timeline_id.in_([t.id for t in timelines]))
    )
    for event in events:
        value = event.value
        if event.field_key == "presence" and isinstance(value, dict) and isinstance(value.get("state"), dict):
            value = value["state"].get("stage_id")
        if event.field_key in ("stage_id", "presence") and type(value) is int:
            stage_ids.add(value)
    return stage_ids
