"""作者私有笔名和作品署名；无提交原语供建书与恢复共用。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import PenName, ProjectPenName
from app.schemas.pen_names import PenNameBrief, ProjectPenNameOut
from app.services import authorization_service as authz


async def _owned(db: AsyncSession, owner_id: int, pen_name_id: int, *, lock: bool = False) -> PenName:
    stmt = select(PenName).where(PenName.id == pen_name_id, PenName.owner_id == owner_id)
    if lock:
        stmt = stmt.with_for_update()
    with db.no_autoflush:
        row = await db.scalar(stmt.execution_options(populate_existing=True))
    if row is None:
        raise NotFoundError("笔名不存在")
    return row


async def _ensure_unique(db: AsyncSession, owner_id: int, name: str, *, exclude_id: int | None = None) -> None:
    stmt = select(PenName.id).where(PenName.owner_id == owner_id, PenName.name == name)
    if exclude_id is not None:
        stmt = stmt.where(PenName.id != exclude_id)
    if await db.scalar(stmt) is not None:
        raise ConflictError("该笔名已存在", extra={"code": "pen_name_exists"})


def _check_revision(row: PenName, expected: int) -> None:
    if row.revision != expected:
        raise ConflictError("笔名已被修改，请刷新后重试", extra={"code": "stale_revision", "revision": row.revision})


async def list_pen_names(db: AsyncSession, actor_id: int) -> list[PenName]:
    await authz.require_authenticated(db, actor_id)
    rows = list(await db.scalars(select(PenName).where(PenName.owner_id == actor_id).order_by(PenName.id)))
    await authz.require_authenticated(db, actor_id)
    return rows


async def create_pen_name(db: AsyncSession, actor_id: int, name: str) -> PenName:
    await authz.require_authenticated(db, actor_id, write=True)
    await _ensure_unique(db, actor_id, name)
    row = PenName(owner_id=actor_id, name=name)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_pen_name(db: AsyncSession, actor_id: int, pen_name_id: int, name: str, revision: int) -> PenName:
    await authz.require_authenticated(db, actor_id, write=True)
    row = await _owned(db, actor_id, pen_name_id, lock=True)
    _check_revision(row, revision)
    await _ensure_unique(db, actor_id, name, exclude_id=row.id)
    row.name = name
    row.revision += 1
    await db.commit()
    await db.refresh(row)
    return row


async def delete_pen_name(db: AsyncSession, actor_id: int, pen_name_id: int, revision: int) -> None:
    await authz.require_authenticated(db, actor_id, write=True)
    row = await _owned(db, actor_id, pen_name_id, lock=True)
    _check_revision(row, revision)
    await db.delete(row)
    await db.commit()


async def bind_without_commit(db: AsyncSession, owner_id: int, project_id: int, pen_name_id: int | None) -> None:
    """调用方持有 User 写屏障及活跃项目锁或拥有尚未提交的新项目。"""
    if pen_name_id is not None:
        await _owned(db, owner_id, pen_name_id)
    row = await db.get(ProjectPenName, project_id)
    if pen_name_id is None:
        if row is not None:
            await db.delete(row)
    elif row is None:
        db.add(ProjectPenName(project_id=project_id, owner_id=owner_id, pen_name_id=pen_name_id))
    else:
        row.pen_name_id = pen_name_id


async def find_or_create_without_commit(db: AsyncSession, owner_id: int, name: str) -> PenName:
    """备份恢复持有 owner User 屏障；按本用户精确名称复用。"""
    row = await db.scalar(select(PenName).where(PenName.owner_id == owner_id, PenName.name == name))
    if row is None:
        row = PenName(owner_id=owner_id, name=name)
        db.add(row)
        await db.flush()
    return row


async def set_project_pen_name(
    db: AsyncSession, actor_id: int, project_id: int, pen_name_id: int | None,
) -> ProjectPenNameOut:
    from app.services.project_service import get_owned_project

    # 完成所有归属校验前，不得让调用方暂存的其他 ORM 修改自动写入。
    with db.no_autoflush:
        await authz.require_authenticated(db, actor_id, write=True)
        await get_owned_project(db, actor_id, project_id, lock=True)
        await bind_without_commit(db, actor_id, project_id, pen_name_id)
    await db.flush()
    brief = None
    if pen_name_id is not None:
        pen = await _owned(db, actor_id, pen_name_id)
        brief = PenNameBrief(id=pen.id, name=pen.name)
    await db.commit()
    return ProjectPenNameOut(project_id=project_id, pen_name=brief)


async def project_pen_name(db: AsyncSession, project_id: int) -> str | None:
    return await db.scalar(
        select(PenName.name).join(ProjectPenName, ProjectPenName.pen_name_id == PenName.id)
        .where(ProjectPenName.project_id == project_id)
    )
