"""场景写入事务中的批注定位。只有已知替换步骤可使锚点平移。"""

from collections.abc import Iterable, Sequence
from itertools import batched

from sqlalchemy import case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chapter, Scene, SceneSection
from app.models.annotations import AnnotationThread
from app.schemas.annotations import MAX_EXCERPT_LENGTH
from app.schemas.rich_document import ProseEdit, is_text_boundary
from app.services.audit_writer import content_hash as content_fingerprint


def range_matches(content: str, start: int, end: int, selection_hash: str, section_anchors: Iterable[int]) -> bool:
    return (start < end and is_text_boundary(content, start) and is_text_boundary(content, end)
            and not any(start < position < end for position in section_anchors)
            and content_fingerprint(content[start:end]) == selection_hash)


def matches(thread: AnnotationThread, scene: Scene, section_anchors: Iterable[int], *,
            scene_hash: str | None = None) -> bool:
    return (thread.anchor_valid and scene.active and thread.chapter_id == scene.chapter_id
            and thread.scene_id == scene.id and thread.scene_hash == (scene_hash or content_fingerprint(scene.content))
            and scene.content[thread.start_offset:min(thread.end_offset, thread.start_offset + MAX_EXCERPT_LENGTH)]
            == thread.excerpt
            and range_matches(scene.content, thread.start_offset, thread.end_offset, thread.selection_hash,
                              section_anchors))


def _replays(before: str, after: str, edits: Sequence[ProseEdit]) -> bool:
    # 批改及撤销按从右至左给出互不重叠的步骤，可一次核对原稿并重建正文。
    if all(left.start > right.end for left, right in zip(edits, edits[1:], strict=False)):
        parts = []
        cursor = 0
        for edit in reversed(edits):
            if (edit.start > edit.end or not is_text_boundary(before, edit.start)
                    or not is_text_boundary(before, edit.end)):
                return False
            parts.extend((before[cursor:edit.start], edit.insert))
            cursor = edit.end
        parts.append(before[cursor:])
        return "".join(parts) == after
    for edit in edits:
        if (edit.start > edit.end or not is_text_boundary(before, edit.start)
                or not is_text_boundary(before, edit.end)):
            return False
        before = before[:edit.start] + edit.insert + before[edit.end:]
    return before == after


def mapped_span(start: int, end: int, edits: Sequence[ProseEdit]) -> tuple[int, int] | None:
    for edit in edits:
        if edit.start == edit.end and not edit.insert:
            continue
        if edit.end <= start:
            shift = len(edit.insert) - (edit.end - edit.start)
            start, end = start + shift, end + shift
        elif edit.start < end:
            return None
    return start, end


async def map_edits(db: AsyncSession, scene: Scene, after: str, edits: Sequence[ProseEdit]) -> None:
    """调用方持有正文写屏障。步骤坐标依次相对于前一步；缺失或对不上即留待统一失效。"""
    rows = list(await db.scalars(select(AnnotationThread).where(
        AnnotationThread.scene_id == scene.id, AnnotationThread.anchor_valid.is_(True),
    )))
    if not rows:
        return
    before_hash = content_fingerprint(scene.content)
    after_hash = content_fingerprint(after)
    proven = _replays(scene.content, after, edits)
    for thread in rows:
        span = mapped_span(thread.start_offset, thread.end_offset, edits) if proven else None
        if (span is None or scene.chapter_id != thread.chapter_id or thread.scene_hash != before_hash
                or not range_matches(scene.content, thread.start_offset, thread.end_offset, thread.selection_hash, ())
                or not range_matches(after, *span, thread.selection_hash, ())):
            thread.anchor_valid = False
        else:
            thread.start_offset, thread.end_offset = span
            thread.scene_hash = after_hash


async def reconcile_chapter(db: AsyncSession, chapter: Chapter, scenes: Sequence[Scene]) -> None:
    """各正文入口共用的最终校验。整篇覆盖、退场、区块穿入及无证据的范围永远失效。"""
    scope = (AnnotationThread.project_id == chapter.project_id, AnnotationThread.chapter_id == chapter.id,
             AnnotationThread.anchor_valid.is_(True))
    tracked = await db.scalars(select(AnnotationThread.scene_id).where(*scope).distinct())
    scene_by_id = {scene.id: scene for scene in scenes}
    section_inside = select(SceneSection.id).where(
        SceneSection.scene_id == AnnotationThread.scene_id, SceneSection.active.is_(True),
        SceneSection.anchor > AnnotationThread.start_offset, SceneSection.anchor < AnnotationThread.end_offset,
    ).exists()
    for batch in batched(tracked, 200):
        # 每个有讨论的活动场景只散列一次；退场景用不可能等于 SHA256 的空串。
        fingerprints = {sid: content_fingerprint(scene_by_id[sid].content) if sid in scene_by_id else ""
                        for sid in batch}
        expected = case(fingerprints, value=AnnotationThread.scene_id)
        await db.execute(update(AnnotationThread).where(
            *scope, AnnotationThread.scene_id.in_(batch), or_(AnnotationThread.scene_hash != expected, section_inside),
        ).values(anchor_valid=False).execution_options(synchronize_session="fetch"))


async def move_with_scene(db: AsyncSession, scene: Scene, from_chapter: int) -> None:
    await db.execute(update(AnnotationThread).where(
        AnnotationThread.scene_id == scene.id, AnnotationThread.chapter_id == from_chapter,
    ).values(chapter_id=scene.chapter_id))
