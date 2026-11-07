"""章节版本的 HTTP 入口：列表、详情、标星/备注、删除、回滚。写入规则见 version_service。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, ChapterVersion
from app.schemas.chapter_version import ChapterVersionUpdate, VersionExtract, VersionRestore
from app.schemas.scene import SceneOut, SceneSnapshot
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
        await get_manageable_chapter(db, user_id, project_id, chapter_id, lock=True, permission="projects.edit")
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
        await get_manageable_chapter(db, user_id, project_id, chapter_id, lock=True, permission="projects.edit")
        version = await _get_version(db, chapter_id, version_id)
        audit.target(version, label=f"v{version.seq}")
        audit.version(version, project_id)
        latest = await version_service.get_latest(db, chapter_id)
        if latest is not None and latest.id == version.id:
            raise BadRequestError("不能删除最新版本")
        await db.delete(version)
        await audit.commit()


async def restore_preview(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, version_id: int, expected_revision: int,
) -> dict:
    from app.services import scene_service

    with db.no_autoflush:
        chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
        await scene_service.check_revision(db, chapter, expected_revision)
        target = await _get_version(db, chapter.id, version_id)
        current = await scene_service.list_active_scenes(db, chapter.id)
        ids = [scene.id for scene in current]
        retiring = []
        blockers = []
        if target.scene_snapshot is not None:
            snapshot = SceneSnapshot.model_validate(target.scene_snapshot)
            ids = [item.id for item in snapshot.scenes]
            for scene_id in ids:
                await scene_service.require_scene(db, chapter, scene_id, active=False)
            retiring = [scene.id for scene in current if scene.id not in ids]
            blockers = await scene_service.reference_blockers(db, chapter, retiring, include_local=True)
        return {"version_id": target.id, "has_scene_snapshot": target.has_scene_snapshot,
                "content_revision": chapter.content_revision, "scene_ids": ids,
                "retiring_scene_ids": retiring, "blockers": blockers}


async def restore_version(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, version_id: int,
    data: VersionRestore | None = None,
) -> Chapter:
    """新历史恢复真实结构；旧纯文本只允许单场景写入，绝不伪造历史划分。"""
    from app.services import planning_lifecycle, scene_service

    async with AuditOperation(
        db, user_id, "chapter_version.restore", "chapter_version", version_id, project_id=project_id
    ) as audit:
        chapter = await scene_service._write_chapter(db, user_id, project_id, chapter_id)
        audit.track(chapter, ["content"])
        audit.values["chapter_id"] = chapter_id
        target = await _get_version(db, chapter_id, version_id)
        audit.target(target, label=f"v{target.seq}")
        expected = data.expected_content_revision if data is not None else None
        if expected is not None:
            await scene_service.check_revision(db, chapter, expected)
        if target.scene_snapshot is not None and expected is None:
            raise BadRequestError("恢复场景结构必须提供当前内容版本")
        if target.scene_snapshot is None and len(await scene_service.list_active_scenes(db, chapter.id)) != 1:
            raise ConflictError("旧纯文本不包含场景结构，请预览后提取到指定场景", extra={
                "code": "scene_target_required",
            })
        seq, content, snapshot = target.seq, target.content, target.scene_snapshot
        await version_service.ensure_baseline(db, chapter, force=True)
        if snapshot is not None:
            await scene_service.restore_snapshot(db, chapter, snapshot, expected_revision=expected)
            await planning_lifecycle.bump_revision(db, project_id)
        else:
            await scene_service.replace_legacy_content(db, chapter, content, expected_revision=expected)
        version = await version_service.record(db, chapter, "restore", note=f"恢复自 v{seq}")
        audit.version(version, project_id)
        await audit.commit(commit_versioned, flush_fn=version_service.flush_versioned)
        await db.refresh(chapter)
        return chapter


async def extract_preview(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, version_id: int,
    scene_id: int, expected_revision: int,
) -> dict:
    from app.services import scene_service

    with db.no_autoflush:
        chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
        await scene_service.check_revision(db, chapter, expected_revision)
        scene = await scene_service.require_scene(db, chapter, scene_id)
        target = await _get_version(db, chapter.id, version_id)
        if target.scene_snapshot is not None:
            raise BadRequestError("该版本包含真实场景结构，请使用结构恢复预览")
        signature = scene_service.content_fingerprint(
            f"{chapter.id}:{scene.id}:{target.id}:{expected_revision}:" +
            scene_service.content_fingerprint(scene.content) + ":" + scene_service.content_fingerprint(target.content)
        )
        return {"version_id": target.id, "scene_id": scene.id, "content_revision": chapter.content_revision,
                "original": scene.content, "replacement": target.content, "signature": signature}


async def extract_version(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, version_id: int, data: VersionExtract,
) -> SceneOut:
    from app.services import scene_service

    async with AuditOperation(
        db, user_id, "chapter_version.restore", "chapter_version", version_id, project_id=project_id
    ) as audit:
        chapter = await scene_service._write_chapter(db, user_id, project_id, chapter_id)
        target = await _get_version(db, chapter.id, version_id)
        audit.target(target, label=f"v{target.seq}")
        preview = await extract_preview(db, user_id, project_id, chapter_id, version_id,
                                        data.scene_id, data.expected_content_revision)
        if preview["signature"] != data.signature:
            raise ConflictError("旧稿提取预览已变化，请重新核对", extra={"code": "scene_preview_changed"})
        scene = await scene_service.require_scene(db, chapter, data.scene_id)
        await version_service.ensure_baseline(db, chapter, force=True)
        await scene_service.replace_scene_content(db, chapter, scene, target.content,
                                                 expected_revision=data.expected_content_revision)
        version = await version_service.record(db, chapter, "restore", note=f"提取 v{target.seq} 整份旧稿到场景")
        audit.version(version, project_id)
        await audit.commit(commit_versioned, flush_fn=version_service.flush_versioned)
        return await scene_service.scene_out(db, chapter, scene)
