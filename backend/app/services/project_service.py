import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import ProjectInfo
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.models import (
    AuthorizationState,
    Chapter,
    PenName,
    Project,
    ProjectCollaborator,
    ProjectCover,
    ProjectPenName,
    Series,
    SeriesBook,
    SeriesCollaborator,
    User,
    WritingStyle,
)
from app.schemas.pen_names import PenNameBrief
from app.schemas.project import (
    ProjectAccessOut,
    ProjectCreate,
    ProjectOut,
    ProjectOwner,
    ProjectSeriesBrief,
    ProjectUpdate,
    ReadingOut,
    WizardProgress,
    WizardProgressUpdate,
)
from app.services import authorization_service as authz
from app.services import project_access, project_lifecycle, style_service
from app.services.series_authorization import authorization_version

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine
from app.services.audit_writer import AuditOperation


async def get_owned_project(
    db: AsyncSession, user_id: int, project_id: int, *, lock: bool = False
) -> Project:
    """严格拥有权；读生命周期时刷新 identity map，修改入口显式请求父行锁。"""
    project = await project_lifecycle.load_record(db, project_id)
    if project is None or project.user_id != user_id:
        raise NotFoundError("项目不存在")
    project_lifecycle.ensure_active(project)
    return await project_lifecycle.lock_active(db, project_id) if lock else project


async def get_execution_project(db: AsyncSession, user_id: int, project_id: int, execution: Any) -> Project:
    """任务执行读取作品：作者（含无执行身份的直调）沿用严格归属；协作执行只按原领取 grant 读取 ai.use，
    不借当前邀请补签。`execution` 为 TaskExecutionIdentity 或 None（E4-g3a 大纲、E4-g3b 实体共用）。"""
    if execution is None or execution.grant_identity is None:
        return await get_owned_project(db, user_id, project_id)
    if execution.actor_id != user_id or execution.project_id != project_id:
        raise ConflictError("任务执行身份已变化")
    with db.no_autoflush:
        return await project_access.require_project(
            db, user_id, project_id, "ai.use", grant_identity=execution.grant_identity,
        )


async def get_manageable_project(
    db: AsyncSession, actor_user_id: int, project_id: int, *, lock: bool = False,
    permission: str = "projects.read",
) -> Project:
    """人工内容的明确read/edit/recycle；严格owner能力绝不由此开放。"""
    project = await project_lifecycle.load_record(db, project_id)
    if project is None:
        raise NotFoundError("项目不存在")
    if project.user_id != actor_user_id:
        if not await authz.allows(db, actor_user_id, "projects.read"):
            raise NotFoundError("项目不存在")
        if lock and permission == "projects.read":
            raise ValueError("人工写入口必须显式声明操作权限")
        if lock:
            await authz.require(db, actor_user_id, permission, write=True, targets=(project.user_id,))
            project = await project_lifecycle.load_record(db, project_id)
            if project is None:
                raise NotFoundError("项目不存在")
        elif not await authz.allows(db, actor_user_id, permission):
            raise NotFoundError("项目不存在")
    project_lifecycle.ensure_active(project)
    return await project_lifecycle.lock_active(db, project_id) if lock else project


async def has_unsettled_external_change(
    db: AsyncSession, *, project_id: int | None = None, user_id: int | None = None,
) -> bool:
    """仅作锁内删除预检；任一外键或任务 UUID 指向目标时保留未决证据。"""
    from app.models import AgentChange, AgentConversation, Task

    if (project_id is None) == (user_id is None):
        raise ValueError("必须明确指定作品或用户")
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        value_type = func.json_type(AgentChange.result)
        claim_type = func.json_type(AgentChange.result, '$."_external_claim_id"')
        task_key_type = func.json_type(AgentChange.result, '$."_external_task_key"')
        execution_type = func.json_type(AgentChange.result, '$."_external_execution"')
        snapshot_mac_type = func.json_type(AgentChange.result, '$."_external_snapshot_mac"')
        outcome_type = func.json_type(AgentChange.result, '$."_external_outcome_unknown"')
        error_type = func.json_type(AgentChange.result, '$."error"')
        text_type = func.json_type(AgentChange.result, '$."text"')
        string_type = "text"
    elif dialect == "postgresql":
        value_type = func.json_typeof(AgentChange.result)
        claim_type = func.json_typeof(AgentChange.result["_external_claim_id"])
        task_key_type = func.json_typeof(AgentChange.result["_external_task_key"])
        execution_type = func.json_typeof(AgentChange.result["_external_execution"])
        snapshot_mac_type = func.json_typeof(AgentChange.result["_external_snapshot_mac"])
        outcome_type = func.json_typeof(AgentChange.result["_external_outcome_unknown"])
        error_type = func.json_typeof(AgentChange.result["error"])
        text_type = func.json_typeof(AgentChange.result["text"])
        string_type = "string"
    else:
        # 尚未验证其他数据库的 JSON 与锁语义，不能把查询失败当作无占用。
        raise RuntimeError("外部执行保护不支持当前数据库")

    # 正常终态不保留内部 claim；状态、载荷或内部标记损坏时不能推断远端已经退出。
    uncertain = or_(
        AgentChange.external_worker_key.is_not(None),
        AgentChange.status.not_in(("proposed", "rejected", "executed", "failed", "undone")),
        claim_type.is_not(None), task_key_type.is_not(None), execution_type.is_not(None), outcome_type.is_not(None),
        snapshot_mac_type.is_not(None),
        and_(AgentChange.kind == "external", AgentChange.status.in_(("executed", "failed")),
             func.coalesce(value_type, "") != "object"),
        and_(AgentChange.kind == "external", AgentChange.status == "failed",
             func.coalesce(error_type, "") != string_type),
        and_(AgentChange.kind == "external", AgentChange.status == "executed",
             func.coalesce(text_type, "") != string_type),
    )
    task_link = or_(
        Task.id == AgentChange.turn_task_id,
        and_(Task.statistics_key.is_not(None),
             or_(Task.statistics_key == AgentChange.turn_task_key,
                 Task.statistics_key == AgentChange.result["_external_task_key"].as_string())),
    )
    if project_id is not None:
        conversation_link = exists(select(AgentConversation.id).where(
            AgentConversation.id == AgentChange.conversation_id, AgentConversation.project_id == project_id,
        ).correlate(AgentChange))
        task_scope = or_(Task.project_id == project_id, Chapter.project_id == project_id)
        task_exists = exists(select(Task.id).outerjoin(Chapter, Chapter.id == Task.chapter_id).where(
            task_link, task_scope,
        ).correlate(AgentChange))
        affected = or_(AgentChange.project_id == project_id, conversation_link, task_exists)
    else:
        conversation_link = exists(select(AgentConversation.id).where(
            AgentConversation.id == AgentChange.conversation_id, AgentConversation.user_id == user_id,
        ).correlate(AgentChange))
        task_exists = exists(select(Task.id).where(task_link, Task.user_id == user_id).correlate(AgentChange))
        affected = or_(conversation_link, task_exists)
    with db.no_autoflush:
        return await db.scalar(select(AgentChange.id).where(uncertain, affected).limit(1)) is not None


async def has_unsettled_user_task_receipts(db: AsyncSession, user_id: int) -> bool:
    """删号调用方已锁用户；按作品再任务锁定并重验所有可能失去的收据。"""
    from app.models import Task
    from app.services import task_service

    with db.no_autoflush:
        statement = select(Task.id, Task.project_id, Chapter.project_id.label("chapter_project_id")).outerjoin(
            Chapter, Chapter.id == Task.chapter_id,
        ).where(Task.user_id == user_id)
        initial = (await db.execute(statement)).all()
        project_ids = {
            value for row in initial for value in (row.project_id, row.chapter_project_id) if value is not None
        }
        for project_id in sorted(project_ids):
            try:
                await project_lifecycle.lock_record(db, project_id)
            except NotFoundError:
                # 关联项目在取锁前消失时不能据旧快照继续级联。
                return True
        tasks = list(await db.scalars(select(Task).where(Task.user_id == user_id).order_by(Task.id).with_for_update()
                                      .execution_options(populate_existing=True)))
        current = (await db.execute(statement)).all()
        if any(value is not None and value not in project_ids for row in current
               for value in (row.project_id, row.chapter_project_id)):
            # 等待锁期间任务关联了新的作品；让调用方重试，不能按未持锁的快照放行。
            return True
        for task in tasks:
            if await task_service.has_unresolved_receipt(db, task):
                return True
    return False


async def project_info(db: AsyncSession, project: Project, *, actor_user_id: int) -> ProjectInfo:
    """作品信息的默认风格按真实 actor 过滤（projects.style 已不再读取）。"""
    style = await style_service.default_description(db, project, actor_user_id=actor_user_id)
    return ProjectInfo(
        title=project.title,
        genre=project.genre,
        premise=project.premise,
        style=style,
        synopsis=project.synopsis or "",
        theme=project.theme or "",
        tags=tuple(project.tags or ()),
        selling_points=project.selling_points or "",
        audience=project.audience or "",
    )


def chapter_stats_subquery():
    return (
        select(
            Chapter.project_id.label("project_id"),
            func.count(Chapter.id).label("chapter_count"),
            func.coalesce(func.sum(Chapter.word_count), 0).label("total_words"),
        )
        .group_by(Chapter.project_id)
        .subquery()
    )


# 「已写章节」：正文有非空白字符（word_count 由服务端按非空白字符计算）
WRITTEN = Chapter.word_count > 0


def parse_progress(raw: Any) -> tuple[int | None, int | None, float]:
    """projects.reading_progress → (chapter_id, chapter_no, ratio)；结构不对的部分按缺失处理，ratio 限制在 [0, 1]。"""
    if not isinstance(raw, dict):
        return None, None, 0.0

    def _int(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    ratio = raw.get("ratio")
    ok = isinstance(ratio, int | float) and not isinstance(ratio, bool) and math.isfinite(ratio)
    return _int(raw.get("chapter_id")), _int(raw.get("chapter_no")), min(max(float(ratio), 0.0), 1.0) if ok else 0.0


async def _last_edited(db: AsyncSession, project_ids: list[int]) -> dict[int, int]:
    """每个项目正文非空、updated_at 最大（同时间取 id 大）的章节；一次查询。"""
    if not project_ids:
        return {}
    rn = (
        func.row_number()
        .over(partition_by=Chapter.project_id, order_by=(Chapter.updated_at.desc(), Chapter.id.desc()))
        .label("rn")
    )
    sub = select(Chapter.project_id, Chapter.id, rn).where(Chapter.project_id.in_(project_ids), WRITTEN).subquery()
    rows = await db.execute(select(sub.c.project_id, sub.c.id).where(sub.c.rn == 1))
    return {pid: cid for pid, cid in rows.all()}


async def _reading(db: AsyncSession, projects: list[Project]) -> dict[int, ReadingOut]:
    """阅读进度（批量，至多两次查询）。进度指向的章节已删除 / 不属于本项目 / 正文为空时，退回（章号, id）
    排在它前面的最近一个已写章节，ratio 置 0；删除的章节按进度里记录的 chapter_no 定位，没有记录则视为无进度。"""
    refs = {}
    for p in projects:
        cid, no, ratio = parse_progress(p.reading_progress)
        if cid is not None:
            refs[p.id] = (cid, no, ratio)
    if not refs:
        return {}
    rows = await db.execute(
        select(Chapter.id, Chapter.project_id, Chapter.chapter_no, Chapter.word_count).where(
            Chapter.id.in_([r[0] for r in refs.values()])
        )
    )
    found = {row.id: row for row in rows.all()}
    out: dict[int, ReadingOut] = {}
    fallback: dict[int, tuple[int, int]] = {}
    for pid, (cid, no, ratio) in refs.items():
        chap = found.get(cid)
        if chap is not None and chap.project_id != pid:
            chap = None
        if chap is not None and chap.word_count > 0:
            out[pid] = ReadingOut(chapter_id=cid, chapter_no=chap.chapter_no, ratio=ratio)
        elif chap is not None:
            fallback[pid] = (chap.chapter_no, chap.id)
        elif no is not None:
            fallback[pid] = (no, cid)
    if fallback:
        cond = or_(*(and_(Chapter.project_id == pid, Chapter.chapter_no <= key[0]) for pid, key in fallback.items()))
        rows = await db.execute(select(Chapter.project_id, Chapter.id, Chapter.chapter_no).where(WRITTEN, cond))
        best: dict[int, tuple[int, int]] = {}
        for pid, cid, no in rows.all():
            key = (no, cid)
            if key < fallback[pid] and (pid not in best or key > best[pid]):
                best[pid] = key
        for pid, (no, cid) in best.items():
            out[pid] = ReadingOut(chapter_id=cid, chapter_no=no, ratio=0.0)
    return out


def _to_out(
    project: Project,
    access: ProjectAccessOut,
    chapter_count: int | None,
    total_words: int | None,
    style: WritingStyle | None = None,
    notice: str | None = None,
    last_edited: int | None = None,
    reading: ReadingOut | None = None,
    cover_updated_at: datetime | None = None,
) -> ProjectOut:
    values = {name: getattr(project, name) for name in ProjectOut.model_fields if hasattr(project, name)}
    out = ProjectOut.model_validate({**values, "access": access})
    out.chapter_count = int(chapter_count or 0)
    out.total_words = int(total_words or 0)
    out.default_style = style_service.to_brief(style) if style is not None else None
    out.notice = notice
    out.last_edited_chapter_id = last_edited
    out.reading = reading
    out.cover_updated_at = cover_updated_at
    return out


def _hide_project_style(
    project: Project, style: WritingStyle | None, actor_user_id: int | None,
    snapshot: authz.AuthorizationSnapshot | None,
) -> bool:
    """只投影本人／预设；管理员的原代管可见性仅覆盖作品作者的风格。"""
    if project.default_style_id is None:
        return False
    if style is None or style.id != project.default_style_id:
        return True
    if style.user_id is None or (actor_user_id is not None and style.user_id == actor_user_id):
        return False
    return not (snapshot is not None and snapshot.allows("projects.read") and style.user_id == project.user_id)


def _series_brief(
    series: Series, actor_user_id: int, invitation_id: str | None, revision: int | None,
) -> ProjectSeriesBrief:
    # 该值仅区分前端协作轮次；调用方必须先从 accepted 关系或作者身份确认系列可见。
    version = None if series.owner_id == actor_user_id else authorization_version(
        series.id, actor_user_id, invitation_id, revision,
    )
    return ProjectSeriesBrief(id=series.id, name=series.name, revision=series.revision, authorization_version=version)


@dataclass(frozen=True)
class _FinalProjectProjection:
    owner_id: int
    deleted_at: datetime | None
    deletion_started_at: datetime | None
    access: ProjectAccessOut
    series: ProjectSeriesBrief | None
    pen_name: PenNameBrief | None


async def _final_project_projections(
    db: AsyncSession, user_id: int, *, project_id: int | None = None,
) -> tuple[authz.AuthorizationSnapshot, dict[int, _FinalProjectProjection]]:
    """最终一次 SQL 同快照复核身份、作品权限与系列摘要；不刷新或写入 ORM 对象。"""
    with db.no_autoflush:
        snapshot = await authz.require_authenticated(db, user_id)
        if project_id is None:
            accepted = select(ProjectCollaborator.id).where(
                ProjectCollaborator.project_id == Project.id, ProjectCollaborator.user_id == user_id,
                ProjectCollaborator.status == "accepted",
            ).correlate(Project).exists()
            project_scope = or_(Project.user_id == user_id, accepted)
        else:
            project_scope = Project.id == project_id
        rows = (await db.execute(
            select(
                User.token_version, User.is_active, AuthorizationState.revision.label("auth_revision"),
                Project.id.label("project_id"), Project.user_id.label("owner_id"), Project.deleted_at,
                Project.deletion_started_at, ProjectCollaborator.id.label("member_id"),
                ProjectCollaborator.role.label("member_role"), ProjectCollaborator.allow_owner_ai,
                ProjectCollaborator.invitation_id.label("project_invitation"),
                ProjectCollaborator.revision.label("project_revision"),
                Series.id.label("series_id"), Series.owner_id.label("series_owner_id"),
                Series.name.label("series_name"), Series.revision.label("series_revision"),
                SeriesCollaborator.invitation_id.label("series_invitation"),
                SeriesCollaborator.revision.label("series_member_revision"),
                PenName.id.label("pen_name_id"), PenName.name.label("pen_name_text"),
            )
            .select_from(User)
            .outerjoin(AuthorizationState, AuthorizationState.id == 1)
            .outerjoin(Project, project_scope)
            .outerjoin(ProjectCollaborator, and_(
                ProjectCollaborator.project_id == Project.id, ProjectCollaborator.user_id == user_id,
                ProjectCollaborator.status == "accepted",
            ))
            .outerjoin(SeriesBook, SeriesBook.project_id == Project.id)
            .outerjoin(Series, Series.id == SeriesBook.series_id)
            .outerjoin(SeriesCollaborator, and_(
                SeriesCollaborator.series_id == Series.id, SeriesCollaborator.user_id == user_id,
                SeriesCollaborator.status == "accepted", SeriesCollaborator.role == "editor",
            ))
            .outerjoin(ProjectPenName, ProjectPenName.project_id == Project.id)
            .outerjoin(PenName, PenName.id == ProjectPenName.pen_name_id)
            .where(User.id == user_id)
        )).all()
    if not rows or not rows[0].is_active or rows[0].token_version != snapshot.token_version:
        raise UnauthorizedError("未登录或登录已过期")
    if rows[0].auth_revision is None:
        raise authz.AuthorizationStorageError("授权状态不可用，请联系管理员")
    if rows[0].auth_revision != snapshot.revision:
        raise ConflictError("作品权限已变化，请刷新后重试", extra={"code": "stale_project_access"})

    result = {}
    for row in rows:
        if row.project_id is None:
            continue
        # 构造不与 session 关联的身份对象，仅复用统一的能力计算，不触碰 identity map。
        identity = Project(id=row.project_id, user_id=row.owner_id)
        member = None if row.member_id is None else ProjectCollaborator(
            project_id=row.project_id, user_id=user_id, status="accepted", role=row.member_role,
            allow_owner_ai=row.allow_owner_ai, invitation_id=row.project_invitation, revision=row.project_revision,
        )
        access = project_access.access_for(identity, user_id, member, snapshot)
        series = None
        if row.series_id is not None and (row.series_owner_id == user_id or row.series_invitation is not None):
            series_identity = Series(id=row.series_id, owner_id=row.series_owner_id, name=row.series_name,
                                     revision=row.series_revision)
            series = _series_brief(series_identity, user_id, row.series_invitation, row.series_member_revision)
        result[row.project_id] = _FinalProjectProjection(
            owner_id=row.owner_id, deleted_at=row.deleted_at, deletion_started_at=row.deletion_started_at,
            access=access, series=series,
            pen_name=PenNameBrief(id=row.pen_name_id if row.owner_id == user_id else None, name=row.pen_name_text)
            if row.pen_name_id is not None else None,
        )
    return snapshot, result


async def list_projects(db: AsyncSession, user_id: int) -> list[ProjectOut]:
    """系列与作品、统计及风格一起投影，不随项目数增加查询次数。"""
    with db.no_autoflush:
        return await _list_projects(db, user_id)


async def _list_projects(db: AsyncSession, user_id: int) -> list[ProjectOut]:
    snapshot = await authz.require_authenticated(db, user_id)
    stats = chapter_stats_subquery()
    rows = (
        await db.execute(
            select(Project, stats.c.chapter_count, stats.c.total_words, WritingStyle, ProjectCover.updated_at,
                   User, ProjectCollaborator)
            .outerjoin(stats, stats.c.project_id == Project.id)
            .outerjoin(WritingStyle, WritingStyle.id == Project.default_style_id)
            .outerjoin(ProjectCover, ProjectCover.project_id == Project.id)
            .join(User, User.id == Project.user_id)
            .outerjoin(ProjectCollaborator, and_(ProjectCollaborator.project_id == Project.id,
                                                  ProjectCollaborator.user_id == user_id,
                                                  ProjectCollaborator.status == "accepted"))
            .where(or_(Project.user_id == user_id, ProjectCollaborator.id.is_not(None)),
                   *project_lifecycle.active_conditions())
            .order_by(Project.updated_at.desc(), Project.id.desc())
        )
    ).all()
    initial_access = {p.id: project_access.access_for(p, user_id, member, snapshot)
                      for p, _, _, _, _, _, member in rows}
    projects = [r[0] for r in rows]
    last = await _last_edited(db, [p.id for p in projects])
    reading = await _reading(db, [p for p in projects if p.user_id == user_id])
    current_snapshot, current = await _final_project_projections(db, user_id)
    result = []
    for p, cc, tw, st, cover, owner, _ in rows:
        fact = current.get(p.id)
        if fact is None:
            continue
        if (fact.owner_id != p.user_id or fact.deleted_at is not None or fact.deletion_started_at is not None
                or not fact.access.can_read or fact.access != initial_access[p.id]):
            continue
        hidden_style = _hide_project_style(p, st, user_id, current_snapshot)
        out = _to_out(p, fact.access, cc, tw, None if hidden_style else st, None,
                      last.get(p.id), reading.get(p.id), cover)
        if hidden_style:
            out.default_style_id = None
        out.owner = ProjectOwner(id=owner.id, username=owner.username)
        out.series = fact.series
        out.pen_name = fact.pen_name
        result.append(out)
    return result


async def project_out(
    db: AsyncSession, project: Project, notice: str | None = None, *, actor_user_id: int | None = None
) -> ProjectOut:
    with db.no_autoflush:
        return await _project_out(db, project, notice, actor_user_id=actor_user_id)


async def _project_out(
    db: AsyncSession, project: Project, notice: str | None = None, *, actor_user_id: int | None = None
) -> ProjectOut:
    row = (
        await db.execute(
            select(func.count(Chapter.id), func.coalesce(func.sum(Chapter.word_count), 0)).where(
                Chapter.project_id == project.id
            )
        )
    ).one()
    style = await db.get(WritingStyle, project.default_style_id) if project.default_style_id else None
    last = await _last_edited(db, [project.id])
    reading = await _reading(db, [project]) if actor_user_id == project.user_id else {}
    cover = await db.scalar(select(ProjectCover.updated_at).where(ProjectCover.project_id == project.id))
    if actor_user_id is None:
        access = project_access.no_access()
        snapshot = None
    else:
        _, access, _ = await project_access.resolve_access(db, actor_user_id, project.id)
        snapshot = await authz.require_authenticated(db, actor_user_id)
    hidden_style = _hide_project_style(project, style, actor_user_id, snapshot)
    out = _to_out(project, access, row[0], row[1], None if hidden_style else style, notice,
                  last.get(project.id), reading.get(project.id), cover)
    if hidden_style:
        out.default_style_id = None
    owner = await db.get(User, project.user_id)
    if owner is not None:
        out.owner = ProjectOwner(id=owner.id, username=owner.username)
    # 最终重验时一并取得系列摘要，避免项目或系列授权在前面的查询期间收紧。
    if actor_user_id is not None:
        current_snapshot, current = await _final_project_projections(db, actor_user_id, project_id=project.id)
        fact = current.get(project.id)
        if fact is None or not fact.access.can_read:
            raise NotFoundError("项目不存在")
        if fact.deleted_at is not None:
            raise NotFoundError("项目不存在")
        if fact.deletion_started_at is not None:
            raise ConflictError("作品正在删除，请稍后重试", extra={"code": "project_deleting"})
        if fact.owner_id != project.user_id or fact.access != access:
            raise ConflictError("作品权限已变化，请刷新后重试", extra={"code": "stale_project_access"})
        if _hide_project_style(project, style, actor_user_id, current_snapshot):
            out.default_style_id = None
            out.default_style = None
        out.series = fact.series
        out.pen_name = fact.pen_name
    return out


async def create_project(db: AsyncSession, user_id: int, data: ProjectCreate) -> tuple[Project, str | None]:
    """返回 (项目, 兼容提示)。传了已废弃的 style 自由文本时为项目创建「原风格」并设为默认。"""
    from app.services.quota_service import check_project_creation

    await check_project_creation(db, user_id)
    # 已持 User 屏障；只重验身份，不反向取得全局 gate。
    await authz.require_authenticated(db, user_id)
    await style_service.ensure_assignable(db, user_id, data.default_style_id)
    project = Project(
        user_id=user_id,
        title=data.title.strip(),
        genre=data.genre.strip(),
        premise=data.premise.strip(),
        status="planning",
        default_style_id=data.default_style_id,
        synopsis=data.synopsis.strip(),
        theme=data.theme.strip(),
        tags=list(data.tags),
        selling_points=data.selling_points.strip(),
        audience=data.audience.strip(),
        target_words=data.target_words,
    )
    db.add(project)
    notice = None
    if data.style.strip():
        notice = await style_service.apply_legacy_style(db, project, data.style)
    if data.pen_name_id is not None:
        from app.services.pen_name_service import bind_without_commit

        await db.flush()
        await bind_without_commit(db, user_id, project.id, data.pen_name_id)
    await db.commit()
    await db.refresh(project)
    return project, notice


async def update_project(
    db: AsyncSession, user_id: int, project_id: int, data: ProjectUpdate
) -> tuple[Project, str | None]:
    """返回 (项目, 兼容提示)。default_style_id 显式 null = 取消默认风格；style（已废弃）更新「原风格」。"""
    async with AuditOperation(db, user_id, "project.update", "project", project_id, project_id=project_id) as audit:
        project = await project_access.require_project(db, user_id, project_id, "content.edit", for_update=True)
        audit.target(project)
        audit.track(project, data.model_fields_set)
        fields = data.model_fields_set
        if project.user_id != user_id and fields.intersection({"style", "default_style_id", "auto_analyze"}):
            raise ForbiddenError("管理他人项目时不能修改私有风格绑定或 AI 设置")
        if "default_style_id" in fields:
            await style_service.ensure_assignable(db, user_id, data.default_style_id)
            project.default_style_id = data.default_style_id
        if "target_words" in fields:
            project.target_words = data.target_words
        exclude = {"style", "default_style_id", "target_words"}
        for field, value in data.model_dump(exclude_unset=True, exclude=exclude).items():
            if value is None:
                continue
            setattr(project, field, value.strip() if isinstance(value, str) else value)
        notice = None
        if data.style is not None:
            notice = await style_service.apply_legacy_style(db, project, data.style)
        await audit.commit()
        await db.refresh(project)
        return project, notice


async def delete_project(
    db: AsyncSession, user_id: int, project_id: int, *, engine: "TaskEngine"
) -> None:
    from app.services.recycle_bin_service import move_to_recycle_bin

    await move_to_recycle_bin(db, engine, user_id, project_id)


def _clean_steps(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        v = v.strip()[:32]
        if v and v not in out:
            out.append(v)
    return out


async def update_wizard_progress(
    db: AsyncSession, user_id: int, project_id: int, data: WizardProgressUpdate
) -> Project:
    """给出的字段整体替换，未给的保持不变；current 显式 null 清空。

    开书向导的步骤进度随向导开放给 accepted editor（E4-g3c，认证/User 屏障 → 作品锁 → 锁后重读成员）；
    viewer 403，非成员/待接受/已撤权 404。向导属 AI 工作流（管理员不能代作者运行），管理员人工权限
    （projects.edit）不扩展到此处：仅凭管理身份仍为原严格作者语义的 404（含管理员兼 viewer）。
    """
    project, access, _member = await project_access.resolve_access(db, user_id, project_id, for_update=True)
    if project.user_id != user_id:
        if not access.is_collaborator:
            raise NotFoundError("项目不存在")
        if access.role != "editor":
            raise ForbiddenError("当前作品没有执行该操作的权限", extra={"code": "project_readonly"})
    current = WizardProgress.model_validate(project.wizard_progress or {})
    fields = data.model_fields_set
    if data.done is not None:
        current.done = _clean_steps(data.done)
    if data.skipped is not None:
        current.skipped = _clean_steps(data.skipped)
    if "current" in fields:
        current.current = (data.current or "").strip() or None
    # JSON 列需整体赋新值才会被识别为已修改
    project.wizard_progress = current.model_dump()
    await db.commit()
    await db.refresh(project)
    return project
