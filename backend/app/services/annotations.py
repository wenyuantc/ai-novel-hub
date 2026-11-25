"""作品成员的审稿讨论，所有写入共用作品授权屏障。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import AnnotationMessage, AnnotationThread, Chapter, Scene, SceneSection, User
from app.schemas.annotations import (
    MAX_EXCERPT_LENGTH,
    MessageCreate,
    MessageOut,
    MessagePage,
    ThreadCreate,
    ThreadOut,
    ThreadPage,
)
from app.schemas.rich_document import require_range
from app.services import annotation_anchors, project_access
from app.services.scene_service import SceneParameterError, check_revision, content_fingerprint, require_scene


async def _chapter(db, actor: User, pid: int, cid: int, *, write=False, owner_only=False) -> Chapter:
    _, access, _ = await project_access.resolve_access(db, actor.id, pid, for_update=write)
    if access.role != "owner" and not access.is_collaborator:
        raise NotFoundError("作品不存在")
    if owner_only and access.role != "owner":
        raise ForbiddenError("只有作品作者可以处理批注")
    query = select(Chapter).where(Chapter.id == cid, Chapter.project_id == pid)
    if write:
        query = query.with_for_update()
    chapter = await db.scalar(query.execution_options(populate_existing=True))
    if chapter is None:
        raise NotFoundError("章节不存在")
    return chapter


def _threads(chapter: Chapter):
    # 即使旧数据归属异常，也不能拿到另一章或另一作品的讨论。
    return select(AnnotationThread).join(Scene, Scene.id == AnnotationThread.scene_id).where(
        AnnotationThread.project_id == chapter.project_id, AnnotationThread.chapter_id == chapter.id,
        Scene.chapter_id == chapter.id,
    )


async def _thread(db, chapter: Chapter, tid: int) -> AnnotationThread:
    row = await db.scalar(_threads(chapter).where(AnnotationThread.id == tid))
    if row is None:
        raise NotFoundError("批注不存在")
    return row


async def _present(db, chapter: Chapter, threads: list[AnnotationThread]) -> list[ThreadOut]:
    if not threads:
        return []
    tids = [t.id for t in threads]
    # 只取每条讨论的首条意见，回复单独分页。
    first_ids = select(func.min(AnnotationMessage.id)).where(AnnotationMessage.thread_id.in_(tids)) \
        .group_by(AnnotationMessage.thread_id)
    messages = await db.scalars(select(AnnotationMessage).where(AnnotationMessage.id.in_(first_ids)))
    first = {message.thread_id: message for message in messages}
    sids = {t.scene_id for t in threads}
    rows = await db.scalars(select(Scene).where(Scene.id.in_(sids), Scene.chapter_id == chapter.id))
    scenes = {scene.id: scene for scene in rows}
    hashes = {scene.id: content_fingerprint(scene.content) for scene in scenes.values()}
    crossing_section = select(SceneSection.id).where(
        SceneSection.scene_id == AnnotationThread.scene_id, SceneSection.active,
        SceneSection.anchor > AnnotationThread.start_offset, SceneSection.anchor < AnnotationThread.end_offset,
    ).exists()
    blocked = set(await db.scalars(select(AnnotationThread.id).where(AnnotationThread.id.in_(tids), crossing_section)))
    result = []
    for t in threads:
        scene = scenes.get(t.scene_id)
        if scene is None or t.id not in first:
            # 分页过程中讨论所属场景被移走或删除。
            continue
        valid = t.id not in blocked and annotation_anchors.matches(t, scene, (), scene_hash=hashes[scene.id])
        result.append(ThreadOut(
            id=t.id, scene_id=t.scene_id, chapter_id=chapter.id, content_revision=chapter.content_revision,
            start=t.start_offset if valid else None, end=t.end_offset if valid else None, excerpt=t.excerpt,
            selection_hash=t.selection_hash, anchor_valid=valid, resolved=t.resolved, created_at=t.created_at,
            first_message=MessageOut.model_validate(first[t.id]),
        ))
    return result


async def list_threads(db: AsyncSession, actor: User, pid: int, cid: int, offset: int, limit: int) -> ThreadPage:
    chapter = await _chapter(db, actor, pid, cid)
    query = _threads(chapter)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    threads = list(await db.scalars(query.order_by(AnnotationThread.id.desc()).offset(offset).limit(limit)))
    items = await _present(db, chapter, threads)
    # PostgreSQL 默认每次查询读最新提交，不能用旧章版本返回改稿后的新偏移。
    await check_revision(db, chapter, chapter.content_revision)
    return ThreadPage(items=items, total=total)


async def _prior(db, actor: User, body: MessageCreate) -> AnnotationMessage | None:
    return await db.scalar(select(AnnotationMessage).where(
        AnnotationMessage.author_id == actor.id, AnnotationMessage.operation_id == str(body.operation_id),
    ))


def _reused() -> ConflictError:
    return ConflictError("该操作标识已用于另一条意见", extra={"code": "annotation_operation_conflict"})


def _message(actor: User, tid: int, body: MessageCreate) -> AnnotationMessage:
    return AnnotationMessage(thread_id=tid, author_id=actor.id, author_name=actor.username,
                             body=body.body, operation_id=str(body.operation_id))


async def create_thread(db: AsyncSession, actor: User, pid: int, cid: int, body: ThreadCreate) -> ThreadOut:
    chapter = await _chapter(db, actor, pid, cid, write=True)
    prior = await _prior(db, actor, body)
    if prior is not None:
        thread = await db.scalar(_threads(chapter).where(AnnotationThread.id == prior.thread_id))
        request_hash = body.selection_hash if body.selected_text is None else content_fingerprint(body.selected_text)
        if (thread is None or prior.body != body.body or thread.scene_id != body.scene_id
                or thread.original_start != body.start or thread.original_end != body.end
                or thread.selection_hash != request_hash):
            raise _reused()
        result, = await _present(db, chapter, [thread])
        if result.first_message.id != prior.id:
            raise _reused()
        return result

    await check_revision(db, chapter, body.expected_content_revision)
    scene = await require_scene(db, chapter, body.scene_id)
    try:
        require_range(scene.content, body.start, body.end)
    except ValueError as exc:
        raise SceneParameterError(str(exc)) from exc
    selected = scene.content[body.start:body.end]
    if not selected.strip():
        raise SceneParameterError("请选择非空白的正式正文")
    selection_hash = content_fingerprint(selected)
    if ((body.selected_text is not None and body.selected_text != selected)
            or (body.selection_hash is not None and body.selection_hash != selection_hash)):
        raise ConflictError("所选原句已变化，请重新核对", extra={"code": "annotation_selection_changed"})
    if await db.scalar(select(SceneSection.id).where(
        SceneSection.scene_id == scene.id, SceneSection.active,
        SceneSection.anchor > body.start, SceneSection.anchor < body.end,
    ).limit(1)) is not None:
        raise SceneParameterError("选区不能穿过作者区块")
    thread = AnnotationThread(project_id=pid, chapter_id=cid, scene_id=scene.id,
                              original_start=body.start, original_end=body.end,
                              start_offset=body.start, end_offset=body.end, excerpt=selected[:MAX_EXCERPT_LENGTH],
                              selection_hash=selection_hash, scene_hash=content_fingerprint(scene.content))
    db.add(thread)
    await db.flush()
    db.add(_message(actor, thread.id, body))
    await db.flush()
    result, = await _present(db, chapter, [thread])
    await db.commit()
    return result


async def list_messages(db: AsyncSession, actor: User, pid: int, cid: int, tid: int,
                        offset: int, limit: int) -> MessagePage:
    chapter = await _chapter(db, actor, pid, cid)
    await _thread(db, chapter, tid)
    scope = AnnotationMessage.thread_id == tid
    total = await db.scalar(select(func.count(AnnotationMessage.id)).where(scope))
    messages = await db.scalars(select(AnnotationMessage).where(scope).order_by(AnnotationMessage.id)
                                .offset(offset).limit(limit))
    return MessagePage(items=[MessageOut.model_validate(m) for m in messages], total=total)


async def reply(db: AsyncSession, actor: User, pid: int, cid: int, tid: int, body: MessageCreate) -> MessageOut:
    chapter = await _chapter(db, actor, pid, cid, write=True)
    await _thread(db, chapter, tid)
    prior = await _prior(db, actor, body)
    if prior is not None:
        first_id = await db.scalar(select(func.min(AnnotationMessage.id)).where(AnnotationMessage.thread_id == tid))
        if prior.thread_id != tid or prior.body != body.body or prior.id == first_id:
            raise _reused()
        return MessageOut.model_validate(prior)
    msg = _message(actor, tid, body)
    db.add(msg)
    await db.flush()
    result = MessageOut.model_validate(msg)
    await db.commit()
    return result


async def resolve(db: AsyncSession, actor: User, pid: int, cid: int, tid: int) -> ThreadOut:
    chapter = await _chapter(db, actor, pid, cid, write=True, owner_only=True)
    thread = await _thread(db, chapter, tid)
    thread.resolved = True
    await db.flush()
    result, = await _present(db, chapter, [thread])
    await db.commit()
    return result
