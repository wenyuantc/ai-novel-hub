from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import ProjectInfo
from app.core.errors import NotFoundError
from app.models import Chapter, Project
from app.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate, WizardProgress, WizardProgressUpdate


async def get_owned_project(db: AsyncSession, user_id: int, project_id: int) -> Project:
    """按 user_id 校验归属；他人项目与不存在统一返回 404，避免泄露存在性。"""
    project = await db.scalar(select(Project).where(Project.id == project_id, Project.user_id == user_id))
    if project is None:
        raise NotFoundError("项目不存在")
    return project


def project_info(project: Project) -> ProjectInfo:
    return ProjectInfo(title=project.title, genre=project.genre, premise=project.premise, style=project.style)


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


def _to_out(project: Project, chapter_count: int | None, total_words: int | None) -> ProjectOut:
    out = ProjectOut.model_validate(project)
    out.chapter_count = int(chapter_count or 0)
    out.total_words = int(total_words or 0)
    return out


async def list_projects(db: AsyncSession, user_id: int) -> list[ProjectOut]:
    stats = _stats_subquery()
    rows = await db.execute(
        select(Project, stats.c.chapter_count, stats.c.total_words)
        .outerjoin(stats, stats.c.project_id == Project.id)
        .where(Project.user_id == user_id)
        .order_by(Project.updated_at.desc(), Project.id.desc())
    )
    return [_to_out(p, cc, tw) for p, cc, tw in rows.all()]


async def project_out(db: AsyncSession, project: Project) -> ProjectOut:
    row = (
        await db.execute(
            select(func.count(Chapter.id), func.coalesce(func.sum(Chapter.word_count), 0)).where(
                Chapter.project_id == project.id
            )
        )
    ).one()
    return _to_out(project, row[0], row[1])


async def create_project(db: AsyncSession, user_id: int, data: ProjectCreate) -> Project:
    project = Project(
        user_id=user_id,
        title=data.title.strip(),
        genre=data.genre.strip(),
        premise=data.premise.strip(),
        style=data.style.strip(),
        status="planning",
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def update_project(db: AsyncSession, user_id: int, project_id: int, data: ProjectUpdate) -> Project:
    project = await get_owned_project(db, user_id, project_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        setattr(project, field, value.strip() if isinstance(value, str) else value)
    await db.commit()
    await db.refresh(project)
    return project


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
