from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import ProjectInfo
from app.core.errors import NotFoundError
from app.models import Chapter, Project, WritingStyle
from app.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate, WizardProgress, WizardProgressUpdate
from app.services import style_service


async def get_owned_project(db: AsyncSession, user_id: int, project_id: int) -> Project:
    """按 user_id 校验归属；他人项目与不存在统一返回 404，避免泄露存在性。"""
    project = await db.scalar(select(Project).where(Project.id == project_id, Project.user_id == user_id))
    if project is None:
        raise NotFoundError("项目不存在")
    return project


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


def _stats_subquery():
    return (
        select(
            Chapter.project_id.label("project_id"),
            func.count(Chapter.id).label("chapter_count"),
            func.coalesce(func.sum(Chapter.word_count), 0).label("total_words"),
        )
        .group_by(Chapter.project_id)
        .subquery()
    )


def _to_out(
    project: Project,
    chapter_count: int | None,
    total_words: int | None,
    style: WritingStyle | None = None,
    notice: str | None = None,
) -> ProjectOut:
    out = ProjectOut.model_validate(project)
    out.chapter_count = int(chapter_count or 0)
    out.total_words = int(total_words or 0)
    out.default_style = style_service.to_brief(style) if style is not None else None
    out.notice = notice
    return out


async def list_projects(db: AsyncSession, user_id: int) -> list[ProjectOut]:
    stats = _stats_subquery()
    rows = await db.execute(
        select(Project, stats.c.chapter_count, stats.c.total_words, WritingStyle)
        .outerjoin(stats, stats.c.project_id == Project.id)
        .outerjoin(WritingStyle, WritingStyle.id == Project.default_style_id)
        .where(Project.user_id == user_id)
        .order_by(Project.updated_at.desc(), Project.id.desc())
    )
    return [_to_out(p, cc, tw, st) for p, cc, tw, st in rows.all()]


async def project_out(db: AsyncSession, project: Project, notice: str | None = None) -> ProjectOut:
    row = (
        await db.execute(
            select(func.count(Chapter.id), func.coalesce(func.sum(Chapter.word_count), 0)).where(
                Chapter.project_id == project.id
            )
        )
    ).one()
    style = await db.get(WritingStyle, project.default_style_id) if project.default_style_id else None
    return _to_out(project, row[0], row[1], style, notice)


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
    project = await get_owned_project(db, user_id, project_id)
    fields = data.model_fields_set
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
    await db.commit()
    await db.refresh(project)
    return project, notice


async def delete_project(db: AsyncSession, user_id: int, project_id: int) -> None:
    project = await get_owned_project(db, user_id, project_id)
    await db.delete(project)
    await db.commit()


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
