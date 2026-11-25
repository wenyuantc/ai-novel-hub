import re
from copy import deepcopy
from typing import Literal

from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.util import identity_key

from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.models import (
    Chapter,
    ChapterPlan,
    ChapterVersion,
    Outline,
    Project,
    RetrievalChunk,
    StoryMemory,
    Task,
    WritingStyle,
)
from app.schemas.chapter import ChapterBrief, ChapterCreate, ChapterOut, ChaptersFromOutlines, ChapterUpdate
from app.services import (
    ai_dispatch,
    memory_service,
    planning_lifecycle,
    project_access,
    project_lifecycle,
    setting_progression_service,
    style_service,
    version_service,
    writing_stats,
)
from app.services import authorization_service as authz
from app.services.audit_writer import AuditOperation
from app.services.chapter_delete_guard import (
    detach_scene_move_chapters,
    detach_serialization_chapters,
    ensure_chapter_delete_isolation,
)
from app.services.project_service import get_manageable_project, get_owned_project

_WS_RE = re.compile(r"\s+")
_CHAPTER_TASK_KINDS = frozenset({
    "chapter.generate", "chapter.polish", "chapter.revise", "chapter.analyze", "scene.restructure",
})
_UNFINISHED_TASK_STATUSES = ("queued", "running", "paused")
_CHAPTER_WRITE_CONDITIONS = {"expected_content_revision", "expected_scene_id", "expected_title"}


def count_words(text: str) -> int:
    """字数统计：按非空白字符计（中文惯例）。"""
    return len(_WS_RE.sub("", text or ""))


async def commit_versioned(db: AsyncSession) -> None:
    """提交含版本写入的事务；并发写入撞上 (chapter_id, seq) 唯一约束时返回 409。"""
    try:
        await version_service.flush_versioned(db)
        await db.commit()
    except ConflictError:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        if not version_service.is_version_conflict(exc):
            raise
        raise ConflictError("正文同时被其他操作修改，请刷新后重试") from exc


async def get_owned_chapter(db: AsyncSession, user_id: int, chapter_id: int, *, lock: bool = False) -> Chapter:
    chapter = await db.scalar(
        select(Chapter)
        .join(Project, Project.id == Chapter.project_id)
        .where(Chapter.id == chapter_id, Project.user_id == user_id, *project_lifecycle.active_conditions())
    )
    if chapter is None:
        raise NotFoundError("章节不存在")
    if lock:
        await get_owned_project(db, user_id, chapter.project_id, lock=True)
    return chapter


async def get_project_chapter(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, *, lock: bool = False
) -> Chapter:
    await get_owned_project(db, user_id, project_id, lock=lock)
    return await _chapter_in_project(db, project_id, chapter_id)


async def get_manageable_chapter(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, *, lock: bool = False,
    permission: str = "projects.read",
) -> Chapter:
    await get_manageable_project(db, user_id, project_id, lock=lock, permission=permission)
    return await _chapter_in_project(db, project_id, chapter_id)


async def lock_manual_project_visibility(db: AsyncSession, user_id: int, project_id: int) -> Project:
    """人工策略相关写入保留原有 SQLite 认证与发送门禁限时。"""
    sqlite = db.get_bind().dialect.name == "sqlite"
    with db.no_autoflush:
        timeout = int(await db.scalar(text("PRAGMA busy_timeout")) or 0) if sqlite else None
        if sqlite:
            await db.execute(text(f"PRAGMA busy_timeout = {int(ai_dispatch._LOCK_WAIT_SECONDS * 1000)}"))
        try:
            return await project_access.require_project(
                db, user_id, project_id, "content.edit", for_update=True, visibility_write=True,
            )
        except ConflictError as exc:
            if sqlite and (exc.extra or {}).get("code") == "collaboration_busy":
                raise ai_dispatch.VisibilityBusyError() from None
            raise
        finally:
            if timeout is not None:
                await db.execute(text(f"PRAGMA busy_timeout = {timeout}"))


async def get_manual_chapter(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int,
    capability: Literal["content.read", "content.edit"], *, for_update: bool = False, visibility_write: bool = False,
) -> Chapter:
    """仅人工内容使用；写动作必须先锁项目并在锁后重验协作授权。"""
    if capability not in {"content.read", "content.edit"}:
        raise ValueError("未知章节人工能力")
    if (capability == "content.edit" or visibility_write) and not for_update:
        raise ValueError("章节人工写入必须持有项目锁")
    with db.no_autoflush:
        cached = db.identity_map.get(identity_key(Chapter, chapter_id))
        pending = {}
        content_revision = None
        if for_update and cached is not None:
            state = inspect(cached)
            protected = ("id", "project_id", "status", "content_revision", "generation_claim_key",
                         "generation_claim_task_key", "generation_claim_prev_status")
            if any(state.attrs[key].history.has_changes() for key in protected):
                raise ConflictError("章节状态已有未提交修改，请先完成当前操作")
            content_revision = cached.content_revision
            pending = {
                column.key: deepcopy(getattr(cached, column.key)) for column in state.mapper.columns
                if column.key not in protected
                and state.attrs[column.key].history.has_changes()
            }
        if visibility_write:
            if capability != "content.edit":
                raise ValueError("发送写门禁仅适用于人工写入")
            await lock_manual_project_visibility(db, user_id, project_id)
        else:
            await project_access.require_project(db, user_id, project_id, capability, for_update=for_update)
        statement = select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        chapter = await db.scalar(statement)
        if chapter is not None:
            if pending.keys() & {"content", "word_count"} and chapter.content_revision != content_revision:
                raise ConflictError("场景已被其他操作修改，请核对最新内容后重试", extra={
                    "code": "stale_scene_revision", "current_revision": chapter.content_revision,
                })
            for field, value in pending.items():
                setattr(chapter, field, value)
    if chapter is None:
        raise NotFoundError("章节不存在")
    return chapter


async def manual_chapter_views(
    db: AsyncSession, user_id: int, project_id: int, chapters: list[Chapter], *, full: bool,
) -> list[ChapterBrief] | list[ChapterOut]:
    """在独立响应模型中过滤不可见风格与失效的跨作品关联。"""
    project = await project_access.require_project(db, user_id, project_id, "content.read")
    if any(chapter.project_id != project_id for chapter in chapters):
        raise NotFoundError("章节不存在")
    hidden: set[int] = set()
    style_ids = {chapter.style_id for chapter in chapters if chapter.style_id is not None}
    outline_ids = {chapter.outline_id for chapter in chapters if chapter.outline_id is not None}
    plan_ids = {chapter.plan_id for chapter in chapters if chapter.plan_id is not None}
    valid_outlines: set[int] = set()
    valid_plans: dict[int, tuple[int | None, int]] = {}

    def chunks(ids: set[int]):
        values = list(ids)
        for start in range(0, len(values), 500):
            yield values[start:start + 500]

    with db.no_autoflush:
        for ids in chunks(outline_ids):
            valid_outlines.update(await db.scalars(select(Outline.id).where(
                Outline.project_id == project_id, Outline.id.in_(ids),
            )))
        for ids in chunks(plan_ids):
            rows = await db.execute(select(ChapterPlan.id, ChapterPlan.chapter_id, ChapterPlan.outline_id)
                                    .join(Outline, Outline.id == ChapterPlan.outline_id)
                                    .where(ChapterPlan.project_id == project_id, Outline.project_id == project_id,
                                           ChapterPlan.id.in_(ids)))
            valid_plans.update({row.id: (row.chapter_id, row.outline_id) for row in rows})
        if style_ids:
            visible_owners = {user_id}
            if project.user_id != user_id:
                snapshot = await authz.require_authenticated(db, user_id)
                if snapshot.allows("projects.read"):
                    # 原管理权限可查看作品作者的风格，但不放行异常绑定的第三方风格。
                    visible_owners.add(project.user_id)
            visible: set[int] = set()
            for ids in chunks(style_ids):
                visible.update(await db.scalars(select(WritingStyle.id).where(
                    WritingStyle.id.in_(ids),
                    (WritingStyle.user_id.is_(None) | WritingStyle.user_id.in_(visible_owners)),
                )))
            hidden = style_ids - visible
    schema = ChapterOut if full else ChapterBrief
    return [schema.model_validate(chapter).model_copy(update={
        "style_id": None if chapter.style_id in hidden else chapter.style_id,
        "outline_id": chapter.outline_id if chapter.outline_id in valid_outlines else None,
        "plan_id": chapter.plan_id if (
            chapter.plan_id is not None and chapter.outline_id in valid_outlines
            and valid_plans.get(chapter.plan_id) == (chapter.id, chapter.outline_id)
        ) else None,
    }) for chapter in chapters]


async def _chapter_in_project(db: AsyncSession, project_id: int, chapter_id: int) -> Chapter:
    chapter = await db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id))
    if chapter is None:
        raise NotFoundError("章节不存在")
    return chapter


async def list_chapters(db: AsyncSession, user_id: int, project_id: int) -> list[Chapter]:
    await get_manageable_project(db, user_id, project_id)
    rows = await db.scalars(
        select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_no, Chapter.id)
    )
    return list(rows)


async def list_manual_chapters(db: AsyncSession, user_id: int, project_id: int) -> list[Chapter]:
    await project_access.require_project(db, user_id, project_id, "content.read")
    rows = await db.scalars(
        select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_no, Chapter.id)
    )
    return list(rows)


async def _get_outline(db: AsyncSession, project_id: int, outline_id: int) -> Outline:
    outline = await db.scalar(select(Outline).where(Outline.id == outline_id, Outline.project_id == project_id))
    if outline is None:
        raise BadRequestError("大纲条目不存在或不属于该项目")
    return outline


async def _next_chapter_no(db: AsyncSession, project_id: int) -> int:
    current = await db.scalar(select(func.max(Chapter.chapter_no)).where(Chapter.project_id == project_id))
    return (current or 0) + 1


async def create_chapter(db: AsyncSession, user_id: int, project_id: int, data: ChapterCreate) -> Chapter:
    async with AuditOperation(db, user_id, "chapter.create", "chapter", None, project_id=project_id) as audit:
        await project_access.require_project(db, user_id, project_id, "content.edit", for_update=True)
        title = (data.title or "").strip()
        chapter_no = data.chapter_no
        if data.outline_id is not None:
            outline = await _get_outline(db, project_id, data.outline_id)
            title = title or outline.title
            chapter_no = chapter_no or outline.chapter_no
        if chapter_no is None:
            chapter_no = await _next_chapter_no(db, project_id)
        chapter = Chapter(
            project_id=project_id,
            outline_id=data.outline_id,
            chapter_no=chapter_no,
            title=title or f"第{chapter_no}章",
            content=data.content,
            status="draft",
            word_count=count_words(data.content),
        )
        db.add(chapter)
        from app.services.scene_service import initialize_chapter

        await db.flush()
        await initialize_chapter(db, chapter)
        if chapter.word_count:
            await writing_stats.record_words(
                db, user_id=user_id, project_id=project_id, source="manual", words=chapter.word_count,
            )
            await db.flush()
            version = await version_service.record_manual(db, chapter)
            audit.version(version, project_id)
        await db.flush()
        audit.target(chapter)
        audit.summary.changed_fields = ["title", "chapter_no", "content"]
        audit.summary.word_count_after = chapter.word_count
        await planning_lifecycle.bump_revision(db, project_id)
        await audit.commit()
        await db.refresh(chapter)
        return chapter


async def create_from_outlines(
    db: AsyncSession, user_id: int, project_id: int, data: ChaptersFromOutlines
) -> list[Chapter]:
    """为大纲条目批量创建章节占位（1 纲 1 章）；已关联章节的、已展开为细纲的大纲条目跳过（后者按细纲建章）。"""
    async with AuditOperation(
        db, user_id, "chapter.create_from_outlines", "project", None, project_id=project_id
    ) as audit:
        project = await project_access.require_project(db, user_id, project_id, "content.edit", for_update=True)
        audit.target(project)
        stmt = select(Outline).where(Outline.project_id == project_id)
        if data.outline_ids is not None:
            stmt = stmt.where(Outline.id.in_(data.outline_ids))
        outlines = list(await db.scalars(stmt.order_by(Outline.volume_no, Outline.chapter_no, Outline.sort)))
        if data.outline_ids is not None and len(outlines) != len(set(data.outline_ids)):
            raise BadRequestError("部分大纲条目不存在或不属于该项目")
        linked = set(
            await db.scalars(
                select(Chapter.outline_id).where(Chapter.project_id == project_id, Chapter.outline_id.is_not(None))
            )
        )
        expanded = set(
            await db.scalars(select(ChapterPlan.outline_id).where(ChapterPlan.project_id == project_id).distinct())
        )
        created = []
        for o in outlines:
            if o.id in linked or o.id in expanded:
                continue
            chapter = Chapter(
                project_id=project_id,
                outline_id=o.id,
                chapter_no=o.chapter_no,
                title=o.title,
                content="",
                status="draft",
                word_count=0,
            )
            db.add(chapter)
            created.append(chapter)
        await db.flush()
        from app.services.scene_service import initialize_chapter

        for chapter in created:
            await initialize_chapter(db, chapter)
        audit.affected([c.id for c in created])
        if created:
            await planning_lifecycle.bump_revision(db, project_id)
        await audit.commit()
        for c in created:
            await db.refresh(c)
        return created


async def _check_title_baseline(db: AsyncSession, chapter: Chapter, data: ChapterUpdate) -> None:
    from app.services import scene_service

    if data.expected_scene_id is None or data.expected_content_revision is None or data.expected_title is None:
        raise BadRequestError("保存章节标题需要原始标题、场景和内容版本")
    try:
        await scene_service.require_scene(db, chapter, data.expected_scene_id)
    except NotFoundError:
        raise ConflictError("标题草稿对应的场景已变化，请核对后重试", extra={
            "code": "editor_target_changed",
        }) from None
    await scene_service.check_revision(db, chapter, data.expected_content_revision)
    if chapter.title != data.expected_title:
        raise ConflictError("章节标题已被其他操作修改，请核对后重试", extra={"code": "stale_chapter_title"})


async def update_chapter(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, data: ChapterUpdate, *, manual: bool = False,
) -> Chapter:
    from app.services import scene_service

    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        if manual:
            chapter = await get_manual_chapter(db, user_id, project_id, chapter_id, "content.edit", for_update=True,
                                               visibility_write=data.status is not None)
            project = await db.get(Project, project_id)
            assert project is not None
        else:
            if data.status is not None:
                # 先取认证与发送门禁，再进入原管理员/作者检查和项目锁。
                await lock_manual_project_visibility(db, user_id, project_id)
            chapter = await get_manageable_chapter(
                db, user_id, project_id, chapter_id, lock=True, permission="projects.edit"
            )
            project = await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
        audit.target(chapter)
        fields = data.model_fields_set - _CHAPTER_WRITE_CONDITIONS
        audit.track(chapter, fields)
        if project.user_id != user_id:
            if "style_id" in fields:
                raise ForbiddenError("管理他人项目时不能修改私有风格绑定")
            if chapter.status == "generating":
                raise ConflictError("该章节正在生成中，请等待完成后再编辑")
        # 生成中修改正文/标题会在生成完成时被静默覆盖；只允许单独复位状态
        if (
            chapter.status == "generating"
            and data.status is None
            and (data.content is not None or data.title is not None)
        ):
            raise ConflictError("该章节正在生成中，请等待完成或先复位状态后再编辑")
        if data.title is not None and (data.expected_title is not None or data.expected_scene_id is not None):
            await _check_title_baseline(db, chapter, data)
        original_revision = chapter.content_revision
        title = data.title.strip() if data.title is not None else chapter.title
        title_changed = title != chapter.title
        if fields == {"title"} and not title_changed:
            return chapter
        if chapter.status == "generating" and data.status is not None:
            from app.services import chapter_claim

            if not await chapter_claim.reset_generating_chapter(db, user_id, project_id, chapter_id, data.status):
                raise ConflictError("章节生成状态已变化，请刷新后重试")
        if "outline_id" in fields:
            if data.outline_id is not None:
                await _get_outline(db, project_id, data.outline_id)
            if data.outline_id != chapter.outline_id and chapter.plan_id is not None:
                plan = await db.scalar(select(ChapterPlan).join(Outline, Outline.id == ChapterPlan.outline_id).where(
                    ChapterPlan.id == chapter.plan_id, ChapterPlan.project_id == project_id,
                    ChapterPlan.chapter_id == chapter.id, ChapterPlan.outline_id == chapter.outline_id,
                    Outline.project_id == project_id,
                ))
                if plan is None:
                    raise ConflictError("章节规划关联无效，请刷新后联系作者处理", extra={
                        "code": "chapter_plan_invalid",
                    })
                # 改挂到别的大纲：解除与原细纲的绑定（细纲回到未建章状态），保持双向一致
                await planning_lifecycle.detach_chapter_plan(db, chapter)
                plan.chapter_id = None
                chapter.plan_id = None
            chapter.outline_id = data.outline_id
        if "style_id" in fields:
            # 只能指向可见的风格（预设或本人所有）；null = 继承项目默认
            await style_service.ensure_assignable(db, user_id, data.style_id)
            chapter.style_id = data.style_id
        if data.chapter_no is not None:
            if data.chapter_no != chapter.chapter_no:
                foreign_memory = await db.scalar(select(StoryMemory.id).where(
                    StoryMemory.chapter_id == chapter.id, StoryMemory.project_id.is_distinct_from(project_id),
                ).limit(1))
                foreign_index = await db.scalar(select(RetrievalChunk.id).join(
                    StoryMemory, StoryMemory.id == RetrievalChunk.ref_id,
                ).where(RetrievalChunk.source == "memory", StoryMemory.chapter_id == chapter.id,
                        RetrievalChunk.project_id.is_distinct_from(project_id)).limit(1))
                if foreign_memory is not None or foreign_index is not None:
                    raise ConflictError("章节存在无法安全处理的关联，请联系作者检查", extra={
                        "code": "chapter_reference_invalid",
                    })
                # 剧情记忆按章号召回：该章的记忆跟随
                await memory_service.on_chapter_renumbered(db, chapter.id, data.chapter_no)
                chapter.chapter_no = data.chapter_no
                # 先 flush 让时间线按新的章节号排序；重算与原修改同一事务提交。
                await db.flush()
                await setting_progression_service.on_story_positions_changed(db, project_id)
        if title_changed:
            chapter.title = title
        content_changed = data.content is not None and data.content != chapter.content
        if data.content is not None:
            if content_changed and not data.content.strip():
                await version_service.ensure_baseline(db, chapter, force=True)
            before, after = await scene_service.replace_legacy_content(
                db, chapter, data.content, expected_revision=data.expected_content_revision,
            )
            if manual and content_changed:
                await writing_stats.record_words(
                    db, user_id=user_id, project_id=project_id, source="manual",
                    words=writing_stats.count_added_words(before, after),
                )
        if title_changed and chapter.content_revision == original_revision:
            # 改回原名也必须消耗版本，迟到的旧标题请求不能重新满足写入条件。
            await scene_service.touch_revision(db, chapter, original_revision)
        if data.status is not None:
            chapter.status = data.status
        if content_changed:
            # 手动保存：按 10 分钟窗口合并，与正文修改同一事务提交
            version = await version_service.record_manual(db, chapter)
            audit.version(version, project_id)
        if title_changed or fields.intersection({"outline_id", "chapter_no"}):
            await planning_lifecycle.bump_revision(db, project_id)
        await audit.commit(commit_versioned, flush_fn=version_service.flush_versioned)
        await db.refresh(chapter)
        return chapter


async def _ensure_other_tasks_do_not_use_chapter(db: AsyncSession, actor_id: int, chapter: Chapter) -> None:
    """不披露任务详情；不能证明其他用户的未结束任务与章节无关时拒绝删除。"""
    from app.services.generation_service import BATCH_KIND

    def busy() -> ConflictError:
        return ConflictError("该章节正在使用，请稍后再删除", extra={"code": "chapter_in_use"})

    if chapter.status == "generating":
        raise busy()
    tasks = await db.execute(select(Task.kind, Task.chapter_id, Task.input, Task.project_id,
                                    Chapter.project_id.label("chapter_project_id"))
                             .outerjoin(Chapter, Chapter.id == Task.chapter_id)
                             .where(or_(Task.project_id == chapter.project_id, Task.chapter_id == chapter.id),
                                    Task.user_id != actor_id, Task.status.in_(_UNFINISHED_TASK_STATUSES)))
    for kind, task_chapter_id, payload, task_project_id, task_chapter_project_id in tasks:
        if task_project_id != chapter.project_id:
            raise busy()
        if not isinstance(payload, dict):
            raise busy()
        if kind == BATCH_KIND:
            ids = payload.get("chapter_ids")
            if (task_chapter_id is not None or not isinstance(ids, list) or not ids
                    or any(type(value) is not int or value <= 0 for value in ids)):
                raise busy()
            if chapter.id in ids:
                raise busy()
        elif kind in _CHAPTER_TASK_KINDS:
            input_id = payload.get("chapter_id")
            if (type(task_chapter_id) is not int or type(input_id) is not int or task_chapter_id != input_id
                    or task_chapter_project_id != chapter.project_id or "chapter_ids" in payload):
                raise busy()
            request = payload.get("request")
            if request is not None and (not isinstance(request, dict) or ("chapter_id" in request and (
                type(request["chapter_id"]) is not int or request["chapter_id"] != task_chapter_id
            ))):
                raise busy()
            if task_chapter_id == chapter.id:
                raise busy()
        else:
            raise busy()


async def _lock_chapter_for_delete(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> Chapter:
    """纯协作删除先取发送写门禁；作者或原管理员保留原删除行为。"""
    with db.no_autoflush:
        project = await project_access.require_project(db, user_id, project_id, "content.edit")
        initial = await authz.require_authenticated(db, user_id)
        prior_bypass = project.user_id == user_id or (
            initial.allows("projects.read") and initial.allows("projects.edit")
        )
        chapter = await get_manual_chapter(
            db, user_id, project_id, chapter_id, "content.edit", for_update=True, visibility_write=not prior_bypass,
        )
        # 管理权限可能在预判和持锁之间变化；不得逆序补门禁或沿用旧审计来源。
        current = await authz.require_authenticated(db, user_id)
        current_bypass = project.user_id == user_id or (
            current.allows("projects.read") and current.allows("projects.edit")
        )
        if prior_bypass != current_bypass:
            raise ConflictError("作品权限已变化，请刷新后重试", extra={"code": "collaboration_busy"})
        if not current_bypass:
            await _ensure_other_tasks_do_not_use_chapter(db, user_id, chapter)
        return chapter


async def delete_chapter(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> None:
    async with AuditOperation(db, user_id, "chapter.delete", "chapter", chapter_id, project_id=project_id) as audit:
        chapter = await _lock_chapter_for_delete(db, user_id, project_id, chapter_id)
        await ensure_chapter_delete_isolation(db, project_id, [chapter.id])
        await detach_scene_move_chapters(db, project_id, [chapter.id])
        audit.target(chapter)
        audit.summary.word_count_before = chapter.word_count
        if audit.enabled:
            audit.summary.version_count = int(
                await db.scalar(select(func.count(ChapterVersion.id)).where(ChapterVersion.chapter_id == chapter.id))
                or 0
            )
        # 先删该章的自动记忆与检索单元（手动记忆保留，chapter_id 由外键置空）
        await planning_lifecycle.detach_sources(db, project_id, chapter_ids=[chapter.id])
        await memory_service.on_chapters_deleted(db, [chapter.id])
        await detach_serialization_chapters(db, project_id, [chapter.id])
        await db.delete(chapter)
        # FK SET NULL 已生效后再重算，避免把已删除章节的变化继续作为最新状态。
        await db.flush()
        await setting_progression_service.on_story_positions_changed(db, project_id)
        await planning_lifecycle.bump_revision(db, project_id)
        await audit.commit()
