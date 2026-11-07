"""场景写作的只读快照与锁定范围；不另存正文，也不为旧负载续签。"""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts.scene import render_scene_scope
from app.core.errors import ConflictError, NotFoundError
from app.models import Chapter, Project, SettingTimeline
from app.services import planning_context_service, scene_service
from app.services.ai_dispatch import (
    VisibilityPolicyError,
    VisibilityStamp,
    read_visibility_stamp,
    require_visibility_stamp,
)


class SceneContextChanged(VisibilityPolicyError):
    def __init__(self) -> None:
        super().__init__("场景或相关设定已变化，请核对后重新生成", extra={"code": "scene_context_changed"})


@dataclass(frozen=True)
class SceneContextStamp:
    project_id: int
    chapter_id: int
    content_revision: int
    planning_revision: int
    signature: str
    visibility: VisibilityStamp

    def log_data(self) -> dict[str, Any]:
        data = asdict(self)
        deleted = data["visibility"]["last_deleted_at"]
        data["visibility"]["last_deleted_at"] = deleted.isoformat() if deleted is not None else None
        return data


async def check_revisions(db: AsyncSession, chapter: Chapter, content: int | None, planning: int | None) -> None:
    if content is not None:
        await scene_service.check_revision(db, chapter, content)
    if planning is not None:
        current = await planning_context_service.planning_revision(db, chapter.project_id)
        if current != planning:
            raise ConflictError(
                "规划已变化，请刷新后核对再试",
                extra={"code": "stale_planning_revision", "current_revision": current},
            )


async def read_stamp(db: AsyncSession, project_id: int, chapter_id: int) -> SceneContextStamp:
    """SQL 标量捕获目标章内容/位置、字段目录与潜在候选时间线版本。

    前章普通正文是构建时背景，不把其他章节并行改稿升级为目标章的写冲突。
    全局结构/改号仍由项目 planning_revision 屏障保护。
    """
    with db.no_autoflush:
        project = (await db.execute(select(
            Project.planning_revision, Project.fields_revision, Project.categories_revision,
        ).where(Project.id == project_id))).one_or_none()
        chapter = (await db.execute(select(
            Chapter.content_revision, Chapter.chapter_no, Chapter.title, Chapter.outline_id, Chapter.plan_id,
        ).where(Chapter.id == chapter_id, Chapter.project_id == project_id))).one_or_none()
        if project is None or chapter is None:
            raise NotFoundError("章节不存在")
        timelines = (await db.execute(select(SettingTimeline.id, SettingTimeline.revision)
                                     .where(SettingTimeline.project_id == project_id)
                                     .order_by(SettingTimeline.id))).all()
        signature = hashlib.sha256(json.dumps(
            [list(project), list(chapter), [list(row) for row in timelines]],
            ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        visibility = await read_visibility_stamp(db, project_id)
        return SceneContextStamp(project_id, chapter_id, chapter.content_revision,
                                 project.planning_revision, signature, visibility)


async def require_stamp(
    db: AsyncSession, expected: SceneContextStamp | dict[str, Any], *, sending: bool = False,
) -> None:
    """发送和采纳复核同一快照；不把当前版本重新标在旧消息上。"""
    if sending:
        project_id = expected["project_id"] if isinstance(expected, dict) else expected.project_id
        # 沿原 visibility send lease 的短事务复用项目读屏障，提交请求头即释放。
        # 不把锁保留到生成正文/响应流；作者写入使用相同项目父行屏障。
        await db.scalar(select(Project.id).where(Project.id == project_id).with_for_update(read=True))
    if isinstance(expected, dict):
        try:
            actual = await read_stamp(db, expected["project_id"], expected["chapter_id"])
        except (KeyError, TypeError, NotFoundError):
            raise SceneContextChanged from None
        if actual.log_data() != expected:
            raise SceneContextChanged
        return
    await require_visibility_stamp(db, expected.visibility)
    try:
        current = await read_stamp(db, expected.project_id, expected.chapter_id)
    except NotFoundError:
        raise SceneContextChanged from None
    if current != expected:
        raise SceneContextChanged


def scope_block(*, scene_id: int, title: str, summary: str, index: int, total: int,
                target_words: int, preceding_text: str = "", selection: bool = False) -> str:
    """范围只含作者正文／摘要；POV 和引用由经过 AiScope 过滤的 planning_context 负责。"""
    return render_scene_scope({
        "scene_id": scene_id, "title": title, "summary": summary, "index": index, "total": total,
        "target_words": target_words, "preceding_text": preceding_text, "selection": selection,
    })
