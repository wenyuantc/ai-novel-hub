"""章节版本的 HTTP 入口：列表、详情、标星/备注、删除、回滚。写入规则见 version_service。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, ChapterVersion
from app.schemas.chapter_version import ChapterVersionUpdate, VersionExtract, VersionRestore
from app.schemas.scene import SceneOut, parse_scene_snapshot
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


async def _legacy_plain_snapshot(db: AsyncSession, chapter: Chapter, content: str) -> dict:
    """用当前唯一场景的真实身份承载旧纯文本，不伪造旧版富文档或规划。"""
    from app.services.scene_service import capture_snapshot

    current = await capture_snapshot(db, chapter)
    if len(current["scenes"]) != 1:
        raise ConflictError("旧纯文本不包含场景结构，请提取到指定场景", extra={"code": "scene_target_required"})
    item = {key: value for key, value in current["scenes"][0].items() if key != "document"}
    prefix = item["separator_before"]
    if prefix and content.startswith(prefix):
        item["content"] = content[len(prefix):]
    else:
        item["content"], item["separator_before"] = content, ""
    return {"schema_version": 1, "scenes": [item]}


async def _legacy_extract_snapshot(db: AsyncSession, chapter: Chapter, scene_id: int, content: str) -> dict:
    """只替换选定景；同章其他场景的真实富文档及本地规划原样保留。"""
    from app.schemas.rich_document import SceneDocument
    from app.services.scene_service import capture_snapshot

    snapshot = await capture_snapshot(db, chapter)
    for item in snapshot["scenes"]:
        if item["id"] == scene_id:
            item["content"] = content
            if snapshot["schema_version"] == 2:
                item["document"] = SceneDocument(content=content).model_dump(mode="json", exclude={"content"})
            return snapshot
    raise NotFoundError("场景不存在")


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
        planning_revision = await scene_service._planning_revision(db, project_id)
        target = await _get_version(db, chapter.id, version_id)
        current = await scene_service.list_active_scenes(db, chapter.id)
        ids = [scene.id for scene in current]
        retiring = []
        blockers = []
        rich_changes = []
        requires_rich_confirmation = False
        candidate_snapshot = target.scene_snapshot
        if candidate_snapshot is None and len(current) == 1:
            if await scene_service._rich_scene_ids(db, ids):
                candidate_snapshot = await _legacy_plain_snapshot(db, chapter, target.content)
        if candidate_snapshot is not None:
            from app.services.rich_document_history import plan_restore

            snapshot = parse_scene_snapshot(candidate_snapshot)
            ids = [item.id for item in snapshot.scenes]
            targets = [await scene_service.require_scene(db, chapter, scene_id, active=False) for scene_id in ids]
            retiring = [scene.id for scene in current if scene.id not in ids]
            blockers = await scene_service.reference_blockers(db, chapter, retiring, include_local=True)
            rich = await scene_service._rich_scene_ids(db, list({scene.id for scene in current} | set(ids)))
            if rich or snapshot.schema_version == 2:
                plan = await plan_restore(db, current, targets, snapshot)
                blockers.extend(plan.blockers)
                rich_changes, requires_rich_confirmation = plan.changes, plan.requires_confirmation
        else:
            rich = await scene_service._rich_scene_ids(db, ids)
            blockers.extend({"kind": "rich_document_history", "id": scene_id, "scene_id": scene_id,
                             "label": "旧纯文本不含富文档结构，需完整恢复"} for scene_id in sorted(rich))
        await scene_service.check_revision(db, chapter, expected_revision)
        if planning_revision != await scene_service._planning_revision(db, project_id):
            raise ConflictError("恢复预览期间场景规划已变化，请重新加载", extra={"code": "scene_context_changed"})
        return {"version_id": target.id, "has_scene_snapshot": target.has_scene_snapshot,
                "content_revision": chapter.content_revision, "scene_ids": ids,
                "retiring_scene_ids": retiring, "blockers": blockers, "rich_changes": rich_changes,
                "requires_rich_confirmation": requires_rich_confirmation}


async def restore_version(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, version_id: int,
    data: VersionRestore | None = None,
) -> Chapter:
    """新历史恢复真实结构；旧纯文本只允许单场景写入，绝不伪造历史划分。"""
    from app.services import planning_lifecycle, scene_service, setting_ai_visibility

    async with AuditOperation(
        db, user_id, "chapter_version.restore", "chapter_version", version_id, project_id=project_id
    ) as audit:
        await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id)
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
        if snapshot is None:
            scenes = await scene_service.list_active_scenes(db, chapter.id)
            rich = await scene_service._rich_scene_ids(db, [scene.id for scene in scenes])
            if rich:
                if expected is None:
                    raise BadRequestError("恢复富文档上的旧纯文本必须提供当前内容版本")
                snapshot = await _legacy_plain_snapshot(db, chapter, content)
        await version_service.ensure_baseline(db, chapter, force=True)
        if snapshot is not None:
            await scene_service.restore_snapshot(
                db, chapter, snapshot, expected_revision=expected, visibility_locked=True,
                confirm_rich_document_loss=data.confirm_rich_document_loss if data is not None else False,
            )
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
        requires_rich_confirmation = False
        rich_changes = []
        if await scene_service._rich_scene_ids(db, [scene.id]):
            from app.services.rich_document_history import plan_restore

            current = await scene_service.list_active_scenes(db, chapter.id)
            synthetic = await _legacy_extract_snapshot(db, chapter, scene.id, target.content)
            plan = await plan_restore(db, current, current, parse_scene_snapshot(synthetic))
            requires_rich_confirmation, rich_changes = plan.requires_confirmation, plan.changes
        await scene_service.check_revision(db, chapter, expected_revision)
        return {"version_id": target.id, "scene_id": scene.id, "content_revision": chapter.content_revision,
                "original": scene.content, "replacement": target.content, "signature": signature,
                "requires_rich_confirmation": requires_rich_confirmation, "rich_changes": rich_changes}


async def extract_version(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, version_id: int, data: VersionExtract,
) -> SceneOut:
    from app.services import planning_lifecycle, scene_service, setting_ai_visibility

    async with AuditOperation(
        db, user_id, "chapter_version.restore", "chapter_version", version_id, project_id=project_id
    ) as audit:
        await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id)
        chapter = await scene_service._write_chapter(db, user_id, project_id, chapter_id)
        target = await _get_version(db, chapter.id, version_id)
        audit.target(target, label=f"v{target.seq}")
        preview = await extract_preview(db, user_id, project_id, chapter_id, version_id,
                                        data.scene_id, data.expected_content_revision)
        if preview["signature"] != data.signature:
            raise ConflictError("旧稿提取预览已变化，请重新核对", extra={"code": "scene_preview_changed"})
        scene = await scene_service.require_scene(db, chapter, data.scene_id)
        await version_service.ensure_baseline(db, chapter, force=True)
        if await scene_service._rich_scene_ids(db, [scene.id]):
            snapshot = await _legacy_extract_snapshot(db, chapter, scene.id, target.content)
            await scene_service.restore_snapshot(
                db, chapter, snapshot, expected_revision=data.expected_content_revision, visibility_locked=True,
                confirm_rich_document_loss=data.confirm_rich_document_loss,
            )
            await planning_lifecycle.bump_revision(db, project_id)
        else:
            await scene_service.replace_scene_content(db, chapter, scene, target.content,
                                                     expected_revision=data.expected_content_revision)
        version = await version_service.record(db, chapter, "restore", note=f"提取 v{target.seq} 整份旧稿到场景")
        audit.version(version, project_id)
        await audit.commit(commit_versioned, flush_fn=version_service.flush_versioned)
        return await scene_service.scene_out(db, chapter, scene)
