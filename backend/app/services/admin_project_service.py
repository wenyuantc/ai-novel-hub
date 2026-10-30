"""实例级项目列表；只返回管理所需的归属、统计和项目元数据。"""

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, User
from app.schemas.admin import AdminProjectList, AdminProjectOut
from app.schemas.project import ProjectStatus
from app.services.admin_service import _escape_like
from app.services.project_service import chapter_stats_subquery


async def list_projects(
    db: AsyncSession,
    *,
    q: str | None,
    owner_user_id: int | None,
    status: ProjectStatus | None,
    page: int,
    page_size: int,
) -> AdminProjectList:
    conditions = []
    keyword = (q or "").strip()
    if keyword:
        pattern = f"%{_escape_like(keyword)}%"
        conditions.append(or_(Project.title.ilike(pattern, escape="\\"), User.username.ilike(pattern, escape="\\")))
    if owner_user_id is not None:
        conditions.append(Project.user_id == owner_user_id)
    if status is not None:
        conditions.append(Project.status == status)
    total = await db.scalar(select(func.count(Project.id)).join(User, User.id == Project.user_id).where(*conditions))
    stats = chapter_stats_subquery()
    rows = await db.execute(
        select(Project, User.username, stats.c.chapter_count, stats.c.total_words)
        .join(User, User.id == Project.user_id)
        .outerjoin(stats, stats.c.project_id == Project.id)
        .where(*conditions)
        .order_by(Project.updated_at.desc(), Project.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return AdminProjectList(
        items=[
            AdminProjectOut(
                id=project.id,
                title=project.title,
                genre=project.genre,
                status=project.status,
                owner_user_id=project.user_id,
                owner_username=username,
                chapter_count=int(count or 0),
                total_words=int(words or 0),
                created_at=project.created_at,
                updated_at=project.updated_at,
            )
            for project, username, count, words in rows.all()
        ],
        total=int(total or 0),
    )
