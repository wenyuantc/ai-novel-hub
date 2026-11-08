"""场景正文、无损结构操作与兼容章投影的唯一写入点。

低层原语只 flush；HTTP 入口取得共同授权／项目／章屏障，并拥有审计及提交。
读取不补数据，不依靠 ORM 事件修复未接入的写入口。
"""

import hashlib
import json
from dataclasses import dataclass
from itertools import batched, chain
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, Project, Scene, SceneDocumentMeta, SceneMarker, SceneSection
from app.schemas.scene import (
    SceneBrief,
    SceneCreate,
    SceneMerge,
    SceneMergePreview,
    SceneMergePreviewOut,
    SceneMutation,
    SceneOut,
    ScenePage,
    ScenePositionOut,
    SceneReorder,
    SceneSplit,
    SceneUpdate,
    parse_scene_snapshot,
)
from app.services import setting_progression_service, version_service
from app.services.audit_writer import AuditOperation
from app.services.chapter_service import count_words, get_manageable_chapter
from app.services.project_service import get_manageable_project

_MAX_SORT = 2**31 - 1


class SceneParameterError(BadRequestError):
    status_code = 422


@dataclass(frozen=True)
class SceneRange:
    scene_id: int
    start: int
    end: int
    separator_start: int
    separator_end: int


def content_fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def project_scenes(scenes: list[Scene]) -> tuple[str, list[SceneRange]]:
    """纯投影；保留全部空白、零字场景与首项分隔，不按 trim 或字数猜边界。"""
    parts, ranges = [], []
    cursor = 0
    for scene in sorted(scenes, key=lambda item: (item.sort, item.id)):
        if not scene.active:
            continue
        separator_start = cursor
        parts.append(scene.separator_before)
        cursor += len(scene.separator_before)
        start = cursor
        parts.append(scene.content)
        cursor += len(scene.content)
        ranges.append(SceneRange(scene.id, start, cursor, separator_start, start))
    return "".join(parts), ranges


async def list_active_scenes(db: AsyncSession, chapter_id: int) -> list[Scene]:
    with db.no_autoflush:
        return list(await db.scalars(
            select(Scene).where(Scene.chapter_id == chapter_id, Scene.active.is_(True))
            .order_by(Scene.sort, Scene.id)
        ))


async def initialize_chapter(db: AsyncSession, chapter: Chapter) -> Scene:
    """仅供明确的新建／迁移入口。初始化不改旧章正文、时间与内容代次。"""
    if chapter.id is None:
        await db.flush()
    with db.no_autoflush:
        existing = list(await db.scalars(select(Scene).where(Scene.chapter_id == chapter.id).order_by(Scene.id)))
    if existing:
        active = [scene for scene in existing if scene.active]
        if not active:
            raise ConflictError("章节缺少活动场景，请检查创建或导入入口", extra={"code": "scene_invariant_error"})
        return min(active, key=lambda scene: (scene.sort, scene.id))
    scene = Scene(
        chapter_id=chapter.id, title="", content=chapter.content, word_count=count_words(chapter.content),
        summary="", sort=0, separator_before="", active=True,
    )
    db.add(scene)
    await db.flush()
    return scene


async def require_scene(db: AsyncSession, chapter: Chapter, scene_id: int, *, active: bool = True) -> Scene:
    with db.no_autoflush:
        statement = select(Scene).where(Scene.id == scene_id, Scene.chapter_id == chapter.id)
        if active:
            statement = statement.where(Scene.active.is_(True))
        scene = await db.scalar(statement)
    if scene is None:
        raise NotFoundError("场景不存在")
    return scene


async def check_revision(db: AsyncSession, chapter: Chapter, expected_revision: int) -> None:
    with db.no_autoflush:
        current = await db.scalar(select(Chapter.content_revision).where(Chapter.id == chapter.id))
    if current != expected_revision:
        raise ConflictError("场景已被其他操作修改，请核对最新内容后重试", extra={
            "code": "stale_scene_revision", "current_revision": current,
        })


async def touch_revision(db: AsyncSession, chapter: Chapter, expected_revision: int | None = None) -> int:
    expected = chapter.content_revision if expected_revision is None else expected_revision
    with db.no_autoflush:
        result = await db.execute(update(Chapter).where(
            Chapter.id == chapter.id, Chapter.content_revision == expected,
        ).values(content_revision=Chapter.content_revision + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        await check_revision(db, chapter, expected)
        raise NotFoundError("章节不存在")
    set_committed_value(chapter, "content_revision", expected + 1)
    return expected + 1


async def refresh_projection(
    db: AsyncSession, chapter: Chapter, *, expected_revision: int | None = None,
) -> None:
    scenes = await list_active_scenes(db, chapter.id)
    if not scenes:
        raise ConflictError("章节缺少活动场景，请检查创建或导入入口", extra={"code": "scene_invariant_error"})
    projection, ranges = project_scenes(scenes)
    # 旧版允许在 CRLF 中间拆景。仅当本次章投影确有跨景接缝时，
    # 才读取完整元数据验证；无关位置的编辑不受影响。
    from app.schemas.rich_document import is_text_boundary, validate_chapter_document_boundaries

    if any(not is_text_boundary(projection, position) for item in ranges for position in (item.start, item.end)):
        from app.services.rich_document_store import capture_documents

        documents, _ = await capture_documents(db, scenes)
        try:
            validate_chapter_document_boundaries([(scene.separator_before, documents[scene.id]) for scene in scenes])
        except ValueError as exc:
            raise SceneParameterError(str(exc)) from exc
    await touch_revision(db, chapter, expected_revision)
    for scene in scenes:
        scene.word_count = count_words(scene.content)
    chapter.content = projection
    chapter.word_count = count_words(chapter.content)
    await db.flush()
    # 原子 UPDATE 的服务端 onupdate 会使属性过期；响应前显式加载，避免异步隐式 IO。
    await db.refresh(chapter, ["updated_at"])


async def _rich_scene_ids(db: AsyncSession, scene_ids: list[int]) -> set[int]:
    if not scene_ids:
        return set()
    scene_id_set = set(scene_ids)
    pending = {item.scene_id for item in chain(db.new, db.dirty, db.deleted)
               if isinstance(item, SceneDocumentMeta | SceneSection | SceneMarker) and item.scene_id in scene_id_set}
    with db.no_autoflush:
        for batch in batched(sorted(scene_id_set), 200):
            rows = select(SceneDocumentMeta.scene_id).where(SceneDocumentMeta.scene_id.in_(batch)).union_all(
                select(SceneSection.scene_id).where(SceneSection.scene_id.in_(batch)),
                select(SceneMarker.scene_id).where(SceneMarker.scene_id.in_(batch)),
            )
            pending.update(await db.scalars(rows))
    return pending


async def _require_plain_scenes(db: AsyncSession, scene_ids: list[int]) -> None:
    """旧路径无法映射富文档，暂以明确冲突保护资料，而不是猜测位置或丢弃。"""
    if await _rich_scene_ids(db, scene_ids):
        raise ConflictError("该操作无法安全映射富格式、标记或区块，请使用支持完整文档的操作", extra={
            "code": "rich_document_unmapped",
        })


async def _require_safe_legacy_edit(db: AsyncSession, scene: Scene, content: str) -> None:
    """只允许能够证明完全不触及现有结构的尾部编辑；其余旧请求仍需完整文档。"""
    if scene.content == content or not await _rich_scene_ids(db, [scene.id]):
        return
    from app.schemas.rich_document import SceneDocument
    from app.services.rich_document_store import capture_documents

    # 共同前缀之后可能发生任意替换；相等锚点的插入偏向未知，也不能猜测。
    common = 0
    for left, right in zip(scene.content, content, strict=False):
        if left != right:
            break
        common += 1
    if common:
        try:
            document = (await capture_documents(db, [scene]))[0][scene.id]
            positions = [*document.formatting.boundaries]
            positions.extend(item.end for item in (*document.formatting.inline, *document.formatting.blocks))
            positions.extend(section.anchor for section in document.sections)
            positions.extend(marker.end for marker in document.markers
                             if marker.section_key is None and marker.status == "active" and marker.end is not None)
            if all(position < common for position in positions):
                SceneDocument.model_validate({**document.model_dump(mode="json"), "content": content})
                return
        except (ConflictError, ValidationError):
            pass
    raise ConflictError("该操作无法安全映射富格式、标记或区块，请使用支持完整文档的操作", extra={
        "code": "rich_document_unmapped",
    })


async def replace_scene_content(
    db: AsyncSession, chapter: Chapter, scene: Scene, content: str, *, expected_revision: int | None = None,
) -> None:
    target = await require_scene(db, chapter, scene.id)
    await _require_safe_legacy_edit(db, target, content)
    target.content = content
    await refresh_projection(db, chapter, expected_revision=expected_revision)


async def replace_chapter_contents(
    db: AsyncSession, chapter: Chapter, contents: dict[int, str], *, expected_revision: int | None = None,
) -> None:
    scenes = await list_active_scenes(db, chapter.id)
    if not scenes or set(contents) != {scene.id for scene in scenes}:
        raise ConflictError("场景结构已变化，请重新发起操作", extra={"code": "scene_context_changed"})
    for scene in scenes:
        await _require_safe_legacy_edit(db, scene, contents[scene.id])
    for scene in scenes:
        scene.content = contents[scene.id]
    await refresh_projection(db, chapter, expected_revision=expected_revision)


async def replace_legacy_content(
    db: AsyncSession, chapter: Chapter, content: str, *, expected_revision: int | None = None,
) -> None:
    scenes = await list_active_scenes(db, chapter.id)
    if len(scenes) != 1:
        raise ConflictError("多场景章节需要明确选择目标场景", extra={"code": "scene_target_required"})
    scene = scenes[0]
    # 旧入口传入的是整章稿，不是场景局部正文。首项可保留有序结构产生的分隔，
    # 必须恰好消费一次；旧纯文本恢复不带该前缀时明确用整份正文替换，不能额外加字。
    prefix = scene.separator_before
    if prefix and content.startswith(prefix):
        content = content[len(prefix):]
    elif prefix:
        # 在改变分隔符之前完成预检，失败时调用方的会话内对象也保持原样。
        await _require_safe_legacy_edit(db, scene, content)
        scene.separator_before = ""
    await replace_scene_content(db, chapter, scene, content, expected_revision=expected_revision)


def _range_target(ranges: list[SceneRange], start: int, end: int) -> tuple[SceneRange | None, str | None]:
    targets = [item for item in ranges if item.start <= start <= end <= item.end]
    # 空光标恰在多个场景共享的切点时没有唯一归属。
    if len(targets) == 1:
        return targets[0], None
    if any(item.separator_start <= start < item.separator_end or
           item.separator_start < end <= item.separator_end for item in ranges):
        return None, "separator"
    return None, "cross_scene"


async def resolve_chapter_range(
    db: AsyncSession, chapter: Chapter, start: int, end: int,
) -> tuple[Scene, int, int]:
    scenes = await list_active_scenes(db, chapter.id)
    content, ranges = project_scenes(scenes)
    if not 0 <= start <= end <= len(content):
        raise BadRequestError("正文范围无效")
    target, _ = _range_target(ranges, start, end)
    if target is None:
        raise ConflictError("该范围跨越场景或分隔，请明确选择一个场景", extra={"code": "scene_target_required"})
    return next(scene for scene in scenes if scene.id == target.scene_id), start - target.start, end - target.start


async def capture_snapshot(db: AsyncSession, chapter: Chapter) -> dict[str, Any]:
    from app.services.planning_scene_service import capture_scene_planning
    from app.services.rich_document_store import capture_documents

    scenes = await list_active_scenes(db, chapter.id)
    if not scenes:
        raise ConflictError("章节缺少活动场景，请检查创建或导入入口", extra={"code": "scene_invariant_error"})
    if project_scenes(scenes)[0] != chapter.content:
        raise ConflictError("章节投影与场景正文不一致，请检查正文写入入口", extra={"code": "scene_invariant_error"})
    with db.no_autoflush:
        planning = await capture_scene_planning(db, chapter.project_id, [scene.id for scene in scenes])
        documents, has_documents = await capture_documents(db, scenes)
    items = [{
        "id": scene.id, "title": scene.title, "content": scene.content, "summary": scene.summary,
        "sort": scene.sort, "separator_before": scene.separator_before, "planning": planning.get(scene.id),
        **({"document": documents[scene.id].model_dump(mode="json", exclude={"content"})} if has_documents else {}),
    } for scene in scenes]
    return {"schema_version": 2 if has_documents else 1, "scenes": items}


async def reference_blockers(
    db: AsyncSession, chapter: Chapter, scene_ids: list[int], *, include_local: bool = False,
) -> list[dict]:
    from app.services.planning_scene_service import capture_scene_planning, scene_reference_blockers

    blockers = await scene_reference_blockers(db, chapter.project_id, scene_ids)
    if include_local and scene_ids:
        snapshots = await capture_scene_planning(db, chapter.project_id, scene_ids)
        for scene_id, snapshot in snapshots.items():
            if snapshot is not None and "override" in snapshot["raw"]["modes"].values():
                blockers.append({"kind": "planning", "id": snapshot["unit_id"], "scene_id": scene_id,
                                 "label": "场景本地覆盖"})
    return blockers


async def _protect_references(
    db: AsyncSession, chapter: Chapter, scene_ids: list[int], *, include_local: bool = False,
) -> None:
    blockers = await reference_blockers(db, chapter, scene_ids, include_local=include_local)
    if blockers:
        raise ConflictError("场景仍有需要保留的演变或独立规划，请先处理引用", extra={
            "code": "scene_reference_conflict", "blockers": blockers,
        })


async def _set_order(db: AsyncSession, scenes: list[Scene]) -> None:
    """先释放所有活动唯一 rank，再设置最终顺序；不依赖 ORM UPDATE 的碰巧次序。"""
    if not scenes:
        return
    highest = max(scene.sort for scene in scenes)
    if highest + len(scenes) > _MAX_SORT:
        raise BadRequestError("场景顺序超过可安全调整的范围")
    for i, scene in enumerate(scenes, start=highest + 1):
        scene.sort = i
    await db.flush()
    for i, scene in enumerate(scenes):
        scene.sort = i
    await db.flush()


async def restore_snapshot(
    db: AsyncSession, chapter: Chapter, snapshot: dict, *, expected_revision: int | None = None,
    visibility_locked: bool = False, confirm_rich_document_loss: bool = False,
) -> None:
    from app.services.planning_scene_service import restore_scene_planning, retire_scene_planning
    from app.services.rich_document_history import apply_restored_documents, plan_restore

    try:
        parsed = parse_scene_snapshot(snapshot)
    except ValidationError as exc:
        raise ConflictError("场景历史格式无效，不能恢复", extra={"code": "scene_reference_conflict"}) from exc
    current = await list_active_scenes(db, chapter.id)
    ids = [item.id for item in parsed.scenes]
    targets = [await require_scene(db, chapter, sid, active=False) for sid in ids]
    rich_ids = await _rich_scene_ids(db, list({item.id for item in current} | set(ids)))
    rich_plan = None
    if rich_ids or parsed.schema_version == 2:
        if not visibility_locked:
            raise ConflictError("该操作无法安全恢复富文档", extra={"code": "rich_document_unmapped"})
        rich_plan = await plan_restore(db, current, targets, parsed)
        if rich_plan.blockers:
            raise ConflictError("结构历史存在身份或资料冲突，请先核对", extra={
                "code": "rich_document_unmapped", "blockers": rich_plan.blockers,
            })
        if rich_plan.requires_confirmation and not confirm_rich_document_loss:
            raise ConflictError("恢复会改变当前富格式、区块或标记，请先预览并确认", extra={
                "code": "rich_document_confirmation_required",
            })
        if rich_plan.visibility_changed:
            from app.services.setting_ai_visibility import bump_visibility_revision

            await bump_visibility_revision(db, chapter.project_id, tighten=rich_plan.visibility_tightened)
    removed = [scene.id for scene in current if scene.id not in ids]
    await _protect_references(db, chapter, removed, include_local=True)
    # 首先释放活动唯一顺序；所有身份始终留存，不以快照裸 ID 新建 Scene。
    for scene in current:
        scene.active = False
    await db.flush()
    await retire_scene_planning(db, chapter.project_id, removed)
    if rich_plan is not None:
        await apply_restored_documents(db, targets, rich_plan.documents)
    for scene, item in zip(targets, parsed.scenes, strict=True):
        scene.title, scene.content, scene.summary = item.title, item.content, item.summary
        scene.separator_before, scene.sort = item.separator_before, item.sort
        scene.active = True
    await db.flush()
    for item in parsed.scenes:
        await restore_scene_planning(db, chapter.project_id, item.id, item.planning)
    await refresh_projection(db, chapter, expected_revision=expected_revision)
    await setting_progression_service.on_story_positions_changed(db, chapter.project_id)


async def _write_chapter(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> Chapter:
    from app.services.generation_service import ensure_outside_batch

    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    chapter = await db.scalar(select(Chapter).where(
        Chapter.id == chapter_id, Chapter.project_id == project_id,
    ).with_for_update().execution_options(populate_existing=True))
    if chapter is None:
        raise NotFoundError("章节不存在")
    if chapter.status == "generating":
        raise ConflictError("该章节正在生成中，请等待完成后再编辑")
    await ensure_outside_batch(db, chapter)
    if not await list_active_scenes(db, chapter.id):
        raise ConflictError("章节缺少活动场景，请检查创建或导入入口", extra={"code": "scene_invariant_error"})
    return chapter


async def _planning_revision(db: AsyncSession, project_id: int) -> int:
    with db.no_autoflush:
        return int(await db.scalar(select(Project.planning_revision).where(Project.id == project_id)) or 0)


def _brief(scene: Scene, chapter: Chapter, planning_revision: int) -> dict[str, Any]:
    return {
        "id": scene.id, "chapter_id": chapter.id, "chapter_no": chapter.chapter_no,
        "chapter_title": chapter.title, "title": scene.title, "summary": scene.summary, "sort": scene.sort,
        "word_count": count_words(scene.content), "content_revision": chapter.content_revision,
        "planning_revision": planning_revision, "created_at": scene.created_at, "updated_at": scene.updated_at,
    }


async def scene_out(db: AsyncSession, chapter: Chapter, scene: Scene, revision: int | None = None) -> SceneOut:
    from app.services.planning_scene_service import resolve_scene_planning

    with db.no_autoflush:
        revision = await _planning_revision(db, chapter.project_id) if revision is None else revision
        planning = await resolve_scene_planning(db, chapter.project_id, scene.id)
        if planning.revision != revision:
            raise ConflictError("场景规划读取期间已变化，请重新加载", extra={"code": "scene_context_changed"})
        await db.refresh(scene, ["created_at", "updated_at"])
    return SceneOut(**_brief(scene, chapter, revision), content=scene.content,
                    separator_before=scene.separator_before, active=scene.active,
                    planning=planning.model_dump(mode="json"))


async def list_scenes(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int | None = None, *, offset: int = 0, limit: int = 50,
) -> ScenePage:
    from app.services.planning_scene_service import resolve_scene_plannings

    with db.no_autoflush:
        await get_manageable_project(db, user_id, project_id)
        revision = await _planning_revision(db, project_id)
        chapter = None
        if chapter_id is not None:
            chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
        conditions = [Chapter.project_id == project_id, Scene.active.is_(True)]
        if chapter_id is not None:
            conditions.append(Scene.chapter_id == chapter_id)
        fields = [Scene.id, Scene.chapter_id, Chapter.chapter_no, Chapter.title.label("chapter_title"),
                  Scene.title, Scene.summary, Scene.sort, Scene.word_count, Chapter.content_revision,
                  Scene.created_at, Scene.updated_at]
        rows = (await db.execute(select(*fields).join(Chapter, Scene.chapter_id == Chapter.id)
            .where(*conditions).order_by(Chapter.chapter_no, Chapter.id, Scene.sort, Scene.id)
            .offset(offset).limit(limit))).mappings().all()
        total = int(await db.scalar(select(func.count()).select_from(Scene)
                                    .join(Chapter, Scene.chapter_id == Chapter.id).where(*conditions)) or 0)
        if chapter is not None and not total:
            raise ConflictError("章节缺少活动场景，请检查创建或导入入口", extra={"code": "scene_invariant_error"})
        planning = await resolve_scene_plannings(db, project_id, [row["id"] for row in rows])
        expected = {row["chapter_id"]: row["content_revision"] for row in rows}
        if chapter is not None:
            # 页顶版本来自较早读取的父章，也必须参加快照核验。否则 rows 已读到
            # 新版而 identity map 中的父章仍为旧版时，会返回相互矛盾的两个版本。
            # 也覆盖 offset 超出末页、没有行内版本可供比较的情况。
            expected[chapter.id] = chapter.content_revision
        actual = dict((await db.execute(select(Chapter.id, Chapter.content_revision).where(
            Chapter.project_id == project_id, Chapter.id.in_(expected),
        ))).all())
        if expected != actual or revision != await _planning_revision(db, project_id):
            raise ConflictError("场景目录读取期间已变化，请重新加载", extra={"code": "scene_context_changed"})
        items = [SceneBrief(**row, planning_revision=revision, planning=planning[row["id"]].model_dump(mode="json"))
                 for row in rows]
        return ScenePage(items=items, total=total, offset=offset, limit=limit,
                         content_revision=chapter.content_revision if chapter else None, planning_revision=revision)


async def get_scene(db: AsyncSession, user_id: int, project_id: int, chapter_id: int, scene_id: int) -> SceneOut:
    with db.no_autoflush:
        chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
        revision = chapter.content_revision
        output = await scene_out(db, chapter, await require_scene(db, chapter, scene_id))
        await check_revision(db, chapter, revision)
        return output


async def _finish_structure(db: AsyncSession, chapter: Chapter, audit: AuditOperation, note: str) -> None:
    from app.services.planning_lifecycle import bump_revision

    await setting_progression_service.on_story_positions_changed(db, chapter.project_id)
    await bump_revision(db, chapter.project_id)
    version = await version_service.record(db, chapter, "manual", note=note)
    audit.version(version, chapter.project_id)
    audit.summary.changed_fields = ["content"]
    await audit.commit(flush_fn=version_service.flush_versioned)


async def _mutation(db: AsyncSession, chapter: Chapter, scenes: list[Scene]) -> SceneMutation:
    revision = await _planning_revision(db, chapter.project_id)
    return SceneMutation(items=[await scene_out(db, chapter, scene, revision) for scene in scenes],
                         content_revision=chapter.content_revision, planning_revision=revision)


async def create_scene(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, data: SceneCreate,
) -> SceneOut:
    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        chapter = await _write_chapter(db, user_id, project_id, chapter_id)
        audit.target(chapter)
        audit.track(chapter, ["content"])
        await check_revision(db, chapter, data.expected_content_revision)
        scenes = await list_active_scenes(db, chapter.id)
        if "after_scene_id" not in data.model_fields_set:
            index = len(scenes)
        elif data.after_scene_id is None:
            index = 0
        else:
            parent = await require_scene(db, chapter, data.after_scene_id)
            index = scenes.index(parent) + 1
        await version_service.ensure_baseline(db, chapter, force=True)
        if max(item.sort for item in scenes) >= _MAX_SORT:
            raise BadRequestError("场景顺序超过可安全调整的范围")
        scene = Scene(chapter_id=chapter.id, title=data.title.strip(), content=data.content, summary=data.summary,
                      sort=max(s.sort for s in scenes) + 1, separator_before="\n\n" if index else "", active=True)
        if index == 0 and not scenes[0].separator_before:
            scenes[0].separator_before = "\n\n"
        db.add(scene)
        await db.flush()
        scenes.insert(index, scene)
        await _set_order(db, scenes)
        await refresh_projection(db, chapter, expected_revision=data.expected_content_revision)
        await _finish_structure(db, chapter, audit, "新增场景")
        return await scene_out(db, chapter, scene)


async def update_scene(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, scene_id: int, data: SceneUpdate,
) -> SceneOut:
    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        chapter = await _write_chapter(db, user_id, project_id, chapter_id)
        audit.target(chapter)
        audit.track(chapter, ["content"])
        scene = await require_scene(db, chapter, scene_id)
        await check_revision(db, chapter, data.expected_content_revision)
        values = {field: (getattr(data, field).strip() if field == "title" else getattr(data, field))
                  for field in data.model_fields_set - {"expected_content_revision"}}
        if "content" in values:
            await _require_safe_legacy_edit(db, scene, values["content"])
        # 编辑器一次提交完整草稿。相同的标题／摘要不应把普通正文保存误判成
        # 结构操作，否则每次手写都绕过固定的十分钟版本合并窗口。
        metadata = any(field in values and values[field] != getattr(scene, field) for field in ("title", "summary"))
        clear = "content" in values and bool(scene.content.strip()) and not values["content"].strip()
        if metadata or clear:
            await version_service.ensure_baseline(db, chapter, force=True)
        for field, value in values.items():
            setattr(scene, field, value)
        await refresh_projection(db, chapter, expected_revision=data.expected_content_revision)
        if metadata or clear:
            version = await version_service.record(
                db, chapter, "manual", note="修改场景资料" if metadata else "清空场景正文",
            )
        else:
            version = await version_service.record_manual(db, chapter)
        audit.version(version, project_id)
        await audit.commit(flush_fn=version_service.flush_versioned)
        return await scene_out(db, chapter, scene)


async def reorder_scenes(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, data: SceneReorder,
) -> SceneMutation:
    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        chapter = await _write_chapter(db, user_id, project_id, chapter_id)
        audit.target(chapter)
        audit.track(chapter, ["content"])
        await check_revision(db, chapter, data.expected_content_revision)
        scenes = await list_active_scenes(db, chapter.id)
        if set(data.scene_ids) != {scene.id for scene in scenes}:
            raise BadRequestError("排序必须包含本章完整的活动场景，不能遗漏或跨章")
        ordered = [{scene.id: scene for scene in scenes}[sid] for sid in data.scene_ids]
        await version_service.ensure_baseline(db, chapter, force=True)
        await _set_order(db, ordered)
        await refresh_projection(db, chapter, expected_revision=data.expected_content_revision)
        await _finish_structure(db, chapter, audit, "调整场景顺序")
        return await _mutation(db, chapter, ordered)


async def split_scene(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, scene_id: int, data: SceneSplit,
) -> SceneMutation:
    from app.services.planning_scene_service import clone_scene_planning
    from app.services.rich_document_store import capture_documents
    from app.services.rich_document_structure import apply_split, split_document
    from app.services.setting_ai_visibility import bump_visibility_revision, lock_author_visibility_write

    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        await lock_author_visibility_write(db, user_id, project_id)
        chapter = await _write_chapter(db, user_id, project_id, chapter_id)
        audit.target(chapter)
        audit.track(chapter, ["content"])
        scene = await require_scene(db, chapter, scene_id)
        await check_revision(db, chapter, data.expected_content_revision)
        if not 0 < data.offset < len(scene.content):
            raise SceneParameterError("拆分光标必须位于正文内部，不能产生无意空片")
        rich = bool(await _rich_scene_ids(db, [scene.id]))
        split_docs = None
        if rich:
            original = (await capture_documents(db, [scene]))[0][scene.id]
            split_docs = split_document(original, data.offset)
        await _protect_references(db, chapter, [scene.id])
        scenes = await list_active_scenes(db, chapter.id)
        before = project_scenes(scenes)[0]
        await version_service.ensure_baseline(db, chapter, force=True)
        if max(item.sort for item in scenes) >= _MAX_SORT:
            raise BadRequestError("场景顺序超过可安全调整的范围")
        right = Scene(chapter_id=chapter.id, title=data.right_title.strip(), content=scene.content[data.offset:],
                      summary="", sort=max(s.sort for s in scenes) + 1, separator_before="", active=True)
        scene.content = scene.content[:data.offset]
        db.add(right)
        await db.flush()
        if split_docs is not None:
            await apply_split(db, scene, right, *split_docs)
            if any(section.include_in_ai for section in split_docs[1].sections):
                await bump_visibility_revision(db, project_id, tighten=True)
        await clone_scene_planning(db, project_id, scene.id, right.id)
        scenes.insert(scenes.index(scene) + 1, right)
        await _set_order(db, scenes)
        await refresh_projection(db, chapter, expected_revision=data.expected_content_revision)
        if chapter.content != before:
            raise ConflictError("场景拆分未能保留原文，操作已拒绝")
        await _finish_structure(db, chapter, audit, "手动拆分场景；原摘要保留在左片")
        return await _mutation(db, chapter, [scene, right])


async def _merge_preview(db: AsyncSession, chapter: Chapter, data: SceneMergePreview) -> SceneMergePreviewOut:
    from app.services.planning_scene_service import capture_scene_planning
    from app.services.rich_document_structure import merge_document

    await check_revision(db, chapter, data.expected_content_revision)
    scenes = await list_active_scenes(db, chapter.id)
    selected = [await require_scene(db, chapter, sid) for sid in data.scene_ids]
    left, right = selected
    if scenes.index(right) != scenes.index(left) + 1:
        raise BadRequestError("只能按原顺序合并本章两个相邻活动场景")
    planning = await capture_scene_planning(db, chapter.project_id, data.scene_ids)
    blockers = await reference_blockers(db, chapter, data.scene_ids)
    rich_ids = await _rich_scene_ids(db, data.scene_ids)
    blocked_rich: set[int] = set()
    if rich_ids:
        try:
            await merge_document(db, left, right)
        except ConflictError:
            blocked_rich.add(right.id if right.id in rich_ids else left.id)
    blockers.extend({"kind": "rich_document", "id": scene_id, "scene_id": scene_id,
                     "label": "富格式、标记或区块需要完整文档映射"}
                    for scene_id in sorted(blocked_rich))
    conflicts = []
    for field in ("title", "summary"):
        if getattr(left, field) != getattr(right, field):
            conflicts.append({"field": field, "left": getattr(left, field), "right": getattr(right, field)})
    raw_left = (planning.get(left.id) or {}).get("raw")
    raw_right = (planning.get(right.id) or {}).get("raw")
    if raw_left != raw_right:
        conflicts.append({"field": "planning", "left": "左场景本地覆盖", "right": "右场景本地覆盖"})
    revision = await _planning_revision(db, chapter.project_id)
    digest = {"chapter": chapter.id, "content_revision": chapter.content_revision, "planning_revision": revision,
              "scenes": [{"id": s.id, "title": s.title, "summary": s.summary, "content": s.content,
                          "sort": s.sort, "separator_before": s.separator_before} for s in selected],
              "planning": planning, "blockers": blockers}
    signature = content_fingerprint(json.dumps(digest, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    return SceneMergePreviewOut(scene_ids=data.scene_ids, content_revision=chapter.content_revision,
                                planning_revision=revision, signature=signature, blockers=blockers, conflicts=conflicts)


async def merge_preview(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, data: SceneMergePreview,
) -> SceneMergePreviewOut:
    with db.no_autoflush:
        chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
        return await _merge_preview(db, chapter, data)


async def merge_scenes(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, data: SceneMerge,
) -> SceneMutation:
    from app.services.planning_scene_service import clone_scene_planning, retire_scene_planning
    from app.services.rich_document_structure import apply_merge, merge_document
    from app.services.setting_ai_visibility import bump_visibility_revision, lock_author_visibility_write

    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        await lock_author_visibility_write(db, user_id, project_id)
        chapter = await _write_chapter(db, user_id, project_id, chapter_id)
        audit.target(chapter)
        audit.track(chapter, ["content"])
        preview = await _merge_preview(db, chapter, data)
        if data.expected_planning_revision != preview.planning_revision:
            raise ConflictError("规划已变化，请重新预检", extra={
                "code": "stale_planning_revision", "current_revision": preview.planning_revision,
            })
        if data.signature != preview.signature:
            raise ConflictError("合并预览已变化，请重新核对", extra={"code": "scene_preview_changed"})
        if preview.blockers:
            raise ConflictError("请先处理场景保留引用，再合并", extra={
                "code": "scene_reference_conflict", "blockers": [b.model_dump() for b in preview.blockers],
            })
        left, right = [await require_scene(db, chapter, sid) for sid in data.scene_ids]
        rich = bool(await _rich_scene_ids(db, [left.id, right.id]))
        merged_doc = await merge_document(db, left, right) if rich else None
        moving_enabled = bool(await db.scalar(select(SceneSection.id).where(
            SceneSection.scene_id == right.id, SceneSection.active.is_(True), SceneSection.include_in_ai.is_(True),
        ).limit(1))) if rich else False
        before = project_scenes(await list_active_scenes(db, chapter.id))[0]
        await version_service.ensure_baseline(db, chapter, force=True)
        if data.metadata_source == "right":
            left.title, left.summary = right.title, right.summary
            await clone_scene_planning(db, project_id, right.id, left.id)
        left.content += right.separator_before + right.content
        if merged_doc is not None:
            await apply_merge(db, left, right, merged_doc)
            if moving_enabled:
                await bump_visibility_revision(db, project_id, tighten=True)
        right.active = False
        await db.flush()
        await retire_scene_planning(db, project_id, [right.id])
        await _set_order(db, await list_active_scenes(db, chapter.id))
        await refresh_projection(db, chapter, expected_revision=data.expected_content_revision)
        if chapter.content != before:
            raise ConflictError("场景合并未能保留原文，操作已拒绝")
        await _finish_structure(db, chapter, audit, "合并相邻场景")
        return await _mutation(db, chapter, [left])


async def delete_scene(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, scene_id: int, expected_revision: int,
) -> SceneMutation:
    from app.services.planning_scene_service import capture_scene_planning, retire_scene_planning

    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        chapter = await _write_chapter(db, user_id, project_id, chapter_id)
        audit.target(chapter)
        audit.track(chapter, ["content"])
        scene = await require_scene(db, chapter, scene_id)
        await check_revision(db, chapter, expected_revision)
        await _require_plain_scenes(db, [scene.id])
        if len(await list_active_scenes(db, chapter.id)) == 1:
            raise ConflictError("不能删除最后一个场景，请清空正文或保留空场景", extra={"code": "scene_last_required"})
        await _protect_references(db, chapter, [scene.id])
        snapshot = (await capture_scene_planning(db, project_id, [scene.id])).get(scene.id)
        if snapshot is not None and any(value == "override" for value in snapshot["raw"]["modes"].values()):
            raise ConflictError("该场景仍有独立本地规划，请先恢复继承或合并时明确选择", extra={
                "code": "scene_reference_conflict", "blockers": [
                    {"kind": "planning", "id": snapshot["unit_id"], "scene_id": scene.id, "label": "场景本地覆盖"},
                ],
            })
        await version_service.ensure_baseline(db, chapter, force=True)
        scene.active = False
        await db.flush()
        await retire_scene_planning(db, project_id, [scene.id])
        await _set_order(db, await list_active_scenes(db, chapter.id))
        await refresh_projection(db, chapter, expected_revision=expected_revision)
        await _finish_structure(db, chapter, audit, "移除场景（保留稳定身份）")
        return await _mutation(db, chapter, [])


async def resolve_position(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, fingerprint: str, start: int, end: int,
) -> ScenePositionOut:
    with db.no_autoflush:
        chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
        content, ranges = project_scenes(await list_active_scenes(db, chapter.id))
        current = content_fingerprint(content)
        result = ScenePositionOut(content_revision=chapter.content_revision, fingerprint=current)
        if fingerprint != current:
            result.reason = "stale"
            return result
        if not 0 <= start <= end <= len(content):
            raise BadRequestError("正文范围无效")
        target, result.reason = _range_target(ranges, start, end)
        if target is not None:
            result.scene_id, result.start, result.end = target.scene_id, start - target.start, end - target.start
        return result
