"""单书批注的快照校验和审阅记录图验证。"""

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from itertools import batched
from typing import Any, NamedTuple

from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Chapter, Scene, SceneSection
from app.models.annotations import AnnotationMessage, AnnotationThread
from app.schemas.annotations import MAX_EXCERPT_LENGTH, MAX_MESSAGE_LENGTH
from app.schemas.rich_document import SceneDocument
from app.services import annotation_anchors
from app.services.scene_service import content_fingerprint

ANNOTATION_TABLES = ("annotation_threads", "annotation_messages")
_HASH = re.compile("[0-9a-f]{64}")
_UNSTORABLE_TEXT = re.compile(r"[\x00\ud800-\udfff]")


class AnnotationState(NamedTuple):
    threads: int
    resolved: int
    messages: int
    linked_authors: int


async def annotation_state(db: AsyncSession, project_id: int) -> AnnotationState:
    """留言只追加，处理单向，删号清除作者关联；章景变化另有章节版本守卫。"""
    threads = select(
        func.count().label("threads"),
        func.count().filter(AnnotationThread.resolved).label("resolved"),
    ).where(AnnotationThread.project_id == project_id).subquery()
    # 即使旧库存在错误引用，导出也不能泄漏另一部作品的审阅记录。
    foreign_chapter = select(AnnotationThread.id).join(Chapter, Chapter.id == AnnotationThread.chapter_id).where(
        Chapter.project_id != AnnotationThread.project_id,
        or_(AnnotationThread.project_id == project_id, Chapter.project_id == project_id),
    ).exists()
    foreign_scene = select(AnnotationThread.id).join(Scene, Scene.id == AnnotationThread.scene_id).join(
        Chapter, Chapter.id == Scene.chapter_id,
    ).where(
        or_(Chapter.project_id != AnnotationThread.project_id, Scene.chapter_id != AnnotationThread.chapter_id),
        or_(AnnotationThread.project_id == project_id, Chapter.project_id == project_id),
    ).exists()
    messages = select(
        func.count().label("messages"), func.count(AnnotationMessage.author_id).label("linked_authors"),
    ).select_from(AnnotationMessage).join(AnnotationThread).where(AnnotationThread.project_id == project_id).subquery()
    row = (await db.execute(select(
        threads.c.threads, threads.c.resolved, messages.c.messages, messages.c.linked_authors,
        foreign_chapter, foreign_scene,
    ).select_from(threads.join(messages, true())))).one()
    if row[4] or row[5]:
        raise ConflictError("批注记录的作品或章景归属不一致，无法导出")
    return AnnotationState(*row[:4])


def _matches_saved_range(thread: Mapping[str, Any], content: str, fingerprint: str,
                         section_anchors: Iterable[int]) -> bool:
    start, end = thread["start_offset"], thread["end_offset"]
    return (fingerprint == thread["scene_hash"]
            and annotation_anchors.range_matches(content, start, end, thread["selection_hash"], section_anchors)
            and content[start:min(end, start + MAX_EXCERPT_LENGTH)] == thread["excerpt"])


async def export_threads(db: AsyncSession, rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """与工作台一样复核位置。旧应用写入过的场景即使漏记失效，也要能恢复其讨论。"""
    by_scene = defaultdict(list)
    for thread in rows:
        if thread["anchor_valid"]:
            by_scene[thread["scene_id"]].append(thread)
    valid = set()
    # 逐小批读取原文，避免按讨论反复加载、散列同一场景。
    for group in batched(by_scene, 8):
        sections = defaultdict(list)
        for sid, position in await db.execute(select(SceneSection.scene_id, SceneSection.anchor).where(
            SceneSection.scene_id.in_(group), SceneSection.active,
        )):
            sections[sid].append(position)
        for sid, cid, content in await db.execute(select(Scene.id, Scene.chapter_id, Scene.content).where(
            Scene.id.in_(group), Scene.active,
        )):
            fingerprint = content_fingerprint(content)
            for thread in by_scene[sid]:
                if thread["chapter_id"] == cid and _matches_saved_range(thread, content, fingerprint, sections[sid]):
                    valid.add(thread["id"])
    return [{**row, "anchor_valid": row["id"] in valid} for row in rows]


def validate_annotation_tables(
    tables: dict[str, list[dict]], documents: dict[int, SceneDocument],
    project_id: int, error: Callable[[str], None],
) -> None:
    threads, messages = (tables.get(name, []) for name in ANNOTATION_TABLES)
    if not threads:
        return  # 通用外键校验会拒绝没有父讨论的留言。
    populated = set()
    for msg in messages:
        populated.add(msg["thread_id"])
        body, name = msg["body"], msg["author_name"]
        if (not body.strip() or len(body) > MAX_MESSAGE_LENGTH or not name.strip()
                or _UNSTORABLE_TEXT.search(body) or _UNSTORABLE_TEXT.search(name) or not msg.get("created_at")):
            error(f"批注留言 #{msg['id']} 的内容、历史署名或时间无效")

    scenes = {row["id"]: row for row in tables["scenes"]}
    scene_hashes = {}
    for thread in threads:
        sid, cid = thread["scene_id"], thread["chapter_id"]
        scene = scenes[sid]
        if (("project_id" in thread and (type(thread["project_id"]) is not int or thread["project_id"] != project_id))
                or scene["chapter_id"] != cid):
            error(f"批注 #{thread['id']} 的作品或章景归属矛盾")
        start, end = thread["start_offset"], thread["end_offset"]
        original_length = thread["original_end"] - thread["original_start"]
        if (thread["id"] not in populated or not thread.get("created_at")
                or type(thread.get("resolved")) is not bool or type(thread.get("anchor_valid")) is not bool
                or not _HASH.fullmatch(thread["selection_hash"]) or not _HASH.fullmatch(thread["scene_hash"])
                or thread["original_start"] < 0 or start < 0 or original_length <= 0 or end - start != original_length
                or _UNSTORABLE_TEXT.search(thread["excerpt"])
                or len(thread["excerpt"]) != min(original_length, MAX_EXCERPT_LENGTH)):
            error(f"批注 #{thread['id']} 缺少留言或原句记录无效")
            continue
        if not thread["anchor_valid"]:
            continue
        doc = documents[sid]
        if sid not in scene_hashes:
            scene_hashes[sid] = content_fingerprint(doc.content)
        if not scene.get("active", True) or not _matches_saved_range(
            thread, doc.content, scene_hashes[sid], (section.anchor for section in doc.sections),
        ):
            error(f"批注 #{thread['id']} 的有效位置与原句不符")
