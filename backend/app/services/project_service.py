import math
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import ProjectInfo
from app.core.errors import ForbiddenError, NotFoundError
from app.models import Chapter, ChapterVersion, Project, ProjectCover, User, WritingStyle
from app.schemas.project import (
    ProjectCreate,
    ProjectOut,
    ProjectOwner,
    ProjectUpdate,
    ReadingOut,
    WizardProgress,
    WizardProgressUpdate,
)
from app.services import style_service
from app.services.audit_writer import AuditOperation


async def get_owned_project(db: AsyncSession, user_id: int, project_id: int) -> Project:
    """按 user_id 校验归属；他人项目与不存在统一返回 404，避免泄露存在性。"""
    project = await db.scalar(select(Project).where(Project.id == project_id, Project.user_id == user_id))
    if project is None:
        raise NotFoundError("项目不存在")
    return project


async def get_manageable_project(db: AsyncSession, actor_user_id: int, project_id: int) -> Project:
    """人工内容权限：所有者或当前启用管理员；不用于 AI、任务或账户私有资源。"""
    project = await db.get(Project, project_id)
    if project is not None:
        if project.user_id == actor_user_id:
            return project
        admin = await db.scalar(
            select(User.id).where(User.id == actor_user_id, User.is_admin.is_(True), User.is_active.is_(True))
        )
        if admin is not None:
            return project
    raise NotFoundError("项目不存在")


async def project_info(db: AsyncSession, project: Project) -> ProjectInfo:
    """作品信息；「写作风格」取项目默认风格的一句话描述（projects.style 已不再读取）。"""
    style = await style_service.default_description(db, project)
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
    chapter_count: int | None,
    total_words: int | None,
    style: WritingStyle | None = None,
    notice: str | None = None,
    last_edited: int | None = None,
    reading: ReadingOut | None = None,
    cover_updated_at: datetime | None = None,
) -> ProjectOut:
    out = ProjectOut.model_validate(project)
    out.chapter_count = int(chapter_count or 0)
    out.total_words = int(total_words or 0)
    out.default_style = style_service.to_brief(style) if style is not None else None
    out.notice = notice
    out.last_edited_chapter_id = last_edited
    out.reading = reading
    out.cover_updated_at = cover_updated_at
    return out


async def list_projects(db: AsyncSession, user_id: int) -> list[ProjectOut]:
    """固定次数的查询（项目 + 统计 + 风格一次，最近编辑一次，阅读进度至多两次），不随项目数增长。"""
    stats = chapter_stats_subquery()
    rows = (
        await db.execute(
            select(Project, stats.c.chapter_count, stats.c.total_words, WritingStyle, ProjectCover.updated_at)
            .outerjoin(stats, stats.c.project_id == Project.id)
            .outerjoin(WritingStyle, WritingStyle.id == Project.default_style_id)
            .outerjoin(ProjectCover, ProjectCover.project_id == Project.id)
            .where(Project.user_id == user_id)
            .order_by(Project.updated_at.desc(), Project.id.desc())
        )
    ).all()
    projects = [r[0] for r in rows]
    last = await _last_edited(db, [p.id for p in projects])
    reading = await _reading(db, projects)
    return [_to_out(p, cc, tw, st, None, last.get(p.id), reading.get(p.id), cover) for p, cc, tw, st, cover in rows]


async def project_out(
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
    reading = await _reading(db, [project]) if actor_user_id in (None, project.user_id) else {}
    cover = await db.scalar(select(ProjectCover.updated_at).where(ProjectCover.project_id == project.id))
    out = _to_out(project, row[0], row[1], style, notice, last.get(project.id), reading.get(project.id), cover)
    owner = await db.get(User, project.user_id)
    if owner is not None:
        out.owner = ProjectOwner(id=owner.id, username=owner.username)
    return out


async def create_project(db: AsyncSession, user_id: int, data: ProjectCreate) -> tuple[Project, str | None]:
    """返回 (项目, 兼容提示)。传了已废弃的 style 自由文本时为项目创建「原风格」并设为默认。"""
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
    await db.commit()
    await db.refresh(project)
    return project, notice


async def update_project(
    db: AsyncSession, user_id: int, project_id: int, data: ProjectUpdate
) -> tuple[Project, str | None]:
    """返回 (项目, 兼容提示)。default_style_id 显式 null = 取消默认风格；style（已废弃）更新「原风格」。"""
    async with AuditOperation(db, user_id, "project.update", "project", project_id, project_id=project_id) as audit:
        project = await get_manageable_project(db, user_id, project_id)
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


async def delete_project(db: AsyncSession, user_id: int, project_id: int) -> None:
    async with AuditOperation(db, user_id, "project.delete", "project", project_id, project_id=project_id) as audit:
        project = await get_manageable_project(db, user_id, project_id)
        audit.target(project)
        if audit.enabled:
            audit.summary.chapter_count = int(
                await db.scalar(select(func.count(Chapter.id)).where(Chapter.project_id == project_id)) or 0
            )
            audit.summary.version_count = int(
                await db.scalar(
                    select(func.count(ChapterVersion.id))
                    .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
                    .where(Chapter.project_id == project_id)
                )
                or 0
            )
        await db.delete(project)
        await audit.commit()


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
    """给出的字段整体替换，未给的保持不变；current 显式 null 清空。"""
    project = await get_owned_project(db, user_id, project_id)
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
