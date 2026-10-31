"""章节版本的 HTTP 入口：列表、详情、标星/备注、删除、回滚。写入规则见 version_service。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, ChapterVersion
from app.schemas.chapter_version import ChapterVersionUpdate
from app.services import version_service
from app.services.audit_writer import AuditOperation
from app.services.chapter_service import commit_versioned, get_manageable_chapter


async def list_versions(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> list[ChapterVersion]:
    await get_manageable_chapter(db, user_id, project_id, chapter_id)
    rows = await db.scalars(
        select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id).order_by(ChapterVersion.seq.desc())
    )
    return list(rows)


async def _get_version(db: AsyncSession, chapter_id: int, version_id: int) -> ChapterVersion:
    version = await db.scalar(
        select(ChapterVersion).where(ChapterVersion.id == version_id, ChapterVersion.chapter_id == chapter_id)
    )
    if version is None:
        raise NotFoundError("版本不存在")
    return version


async def get_version(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, version_id: int
) -> ChapterVersion:
    await get_manageable_chapter(db, user_id, project_id, chapter_id)
    return await _get_version(db, chapter_id, version_id)


async def update_version(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, version_id: int, data: ChapterVersionUpdate
) -> ChapterVersion:
    async with AuditOperation(
        db, user_id, "chapter_version.update", "chapter_version", version_id, project_id=project_id
    ) as audit:
        await get_manageable_chapter(db, user_id, project_id, chapter_id)
        version = await _get_version(db, chapter_id, version_id)
        audit.target(version, label=f"v{version.seq}")
        audit.version(version, project_id)
        audit.track(version, ["starred", "note"])
        if data.starred is not None:
            version.starred = data.starred
        if data.note is not None:
            version.note = data.note.strip()
        await audit.commit()
        await db.refresh(version)
        return version


async def delete_version(db: AsyncSession, user_id: int, project_id: int, chapter_id: int, version_id: int) -> None:
    async with AuditOperation(
        db, user_id, "chapter_version.delete", "chapter_version", version_id, project_id=project_id
    ) as audit:
        await get_manageable_chapter(db, user_id, project_id, chapter_id)
        version = await _get_version(db, chapter_id, version_id)
        audit.target(version, label=f"v{version.seq}")
        audit.version(version, project_id)
        latest = await version_service.get_latest(db, chapter_id)
        if latest is not None and latest.id == version.id:
            raise BadRequestError("不能删除最新版本")
        await db.delete(version)
        await audit.commit()


async def restore_version(db: AsyncSession, user_id: int, project_id: int, chapter_id: int, version_id: int) -> Chapter:
    """回滚：先补 baseline（使回滚可撤销），再把正文改为目标版本，并写 restore 版本。标题不变。"""
    async with AuditOperation(
        db, user_id, "chapter_version.restore", "chapter_version", version_id, project_id=project_id
    ) as audit:
        chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
        audit.track(chapter, ["content"])
        audit.values["chapter_id"] = chapter_id
        if chapter.status == "generating":
            raise ConflictError("该章节正在生成中，请稍候再试")
        target = await _get_version(db, chapter_id, version_id)
        audit.target(target, label=f"v{target.seq}")
        seq, content, word_count = target.seq, target.content, target.word_count
        await version_service.ensure_baseline(db, chapter)
        chapter.content = content
        chapter.word_count = word_count
        version = await version_service.record(db, chapter, "restore", note=f"恢复自 v{seq}")
        audit.version(version, project_id)
        await audit.commit(commit_versioned, flush_fn=version_service.flush_versioned)
        await db.refresh(chapter)
        return chapter
