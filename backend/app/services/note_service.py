"""作品笔记的人工读写：父项目屏障、稳定身份与修订 CAS。"""

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models.notes import ProjectNote
from app.schemas.notes import NoteCreate, NotePage, NoteSummary, NoteUpdate
from app.services.admin_service import _escape_like
from app.services.project_service import get_manageable_project


async def _note(db: AsyncSession, project_id: int, note_id: int) -> ProjectNote:
    note = await db.scalar(select(ProjectNote).where(ProjectNote.project_id == project_id, ProjectNote.id == note_id))
    if note is None:
        raise NotFoundError("笔记不存在")
    return note


async def list_notes(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    *,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> NotePage:
    await get_manageable_project(db, user_id, project_id)
    conditions = [ProjectNote.project_id == project_id]
    if q and q.strip():
        pattern = f"%{_escape_like(q.strip())}%"
        conditions.append(or_(
            ProjectNote.title.ilike(pattern, escape="\\"), ProjectNote.content.ilike(pattern, escape="\\")
        ))
    total = await db.scalar(select(func.count()).select_from(ProjectNote).where(*conditions)) or 0
    rows = await db.execute(
        select(
            ProjectNote.id, ProjectNote.project_id, ProjectNote.title,
            func.substr(ProjectNote.content, 1, 180).label("excerpt"),
            ProjectNote.revision, ProjectNote.created_at, ProjectNote.updated_at,
        )
        .where(*conditions)
        .order_by(ProjectNote.updated_at.desc(), ProjectNote.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return NotePage(
        items=[NoteSummary.model_validate(row) for row in rows], total=total, page=page, page_size=page_size
    )


async def get_note(db: AsyncSession, user_id: int, project_id: int, note_id: int) -> ProjectNote:
    await get_manageable_project(db, user_id, project_id)
    return await _note(db, project_id, note_id)


async def create_note(db: AsyncSession, user_id: int, project_id: int, body: NoteCreate) -> ProjectNote:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    note = ProjectNote(project_id=project_id, title=body.title, content=body.content)
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


def _stale() -> ConflictError:
    return ConflictError("笔记已被修改，请加载最新版本后再保存；当前草稿可继续保留", extra={"code": "stale_revision"})


async def update_note(
    db: AsyncSession, user_id: int, project_id: int, note_id: int, body: NoteUpdate
) -> ProjectNote:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    note = await _note(db, project_id, note_id)
    values = body.model_dump(exclude_unset=True, exclude={"expected_revision"})
    result = await db.execute(
        update(ProjectNote)
        .where(ProjectNote.project_id == project_id, ProjectNote.id == note_id,
               ProjectNote.revision == body.expected_revision)
        .values(**values, revision=ProjectNote.revision + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _stale()
    await db.commit()
    await db.refresh(note)
    return note


async def delete_note(db: AsyncSession, user_id: int, project_id: int, note_id: int, expected_revision: int) -> None:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    await _note(db, project_id, note_id)
    result = await db.execute(
        delete(ProjectNote)
        .where(ProjectNote.project_id == project_id, ProjectNote.id == note_id,
               ProjectNote.revision == expected_revision)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _stale()
    await db.commit()
