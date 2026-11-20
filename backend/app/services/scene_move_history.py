"""场景移动证据、原生章节身份与结构恢复边界。"""

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ConflictError
from app.models import Chapter, Scene, SceneMove


def move_touches_chapter(chapter_id):
    return or_(SceneMove.from_chapter_id == chapter_id, SceneMove.to_chapter_id == chapter_id)


def _native_scene_ids():
    stationary = (
        select(Scene.id.label("scene_id"))
        .where(Scene.chapter_id == Chapter.id, ~select(SceneMove.id).where(SceneMove.scene_id == Scene.id).exists())
        .correlate(Chapter)
    )
    current_chapter = aliased(Chapter)
    moved = (
        select(SceneMove.scene_id)
        .join(Scene, Scene.id == SceneMove.scene_id)
        .join(current_chapter, current_chapter.id == Scene.chapter_id)
        .where(
            SceneMove.from_chapter_id == Chapter.id, SceneMove.first_move,
            current_chapter.project_id == Chapter.project_id,
        )
        .correlate(Chapter)
    )
    return stationary.union_all(moved).subquery()


def chapter_identity_expression():
    """用于已限定归属的 Chapter 查询。章身份来自最早的原生场景。"""
    native = _native_scene_ids()
    return select(func.min(native.c.scene_id)).scalar_subquery()


async def chapter_identity(db: AsyncSession, chapter_id: int) -> int:
    """调用方已核验作品归属，场景身份可比复用的 SQLite 章号更长久。"""
    identity = await db.scalar(select(chapter_identity_expression()).where(Chapter.id == chapter_id))
    if identity is None:
        raise ConflictError("章节缺少可识别的场景，请刷新后重试", extra={"code": "scene_invariant_error"})
    return identity


async def matches_chapter_identity(db: AsyncSession, chapter_id: int, scene_id: int) -> bool:
    """兼容既有连载请求中可证明同章的所有原生场景。"""
    native = _native_scene_ids()
    match = select(native.c.scene_id).where(native.c.scene_id == scene_id).exists()
    return (await db.scalar(select(Chapter.id).where(Chapter.id == chapter_id, match))) is not None


def restore_move_conflict(detail: str) -> ConflictError:
    return ConflictError(detail, extra={"code": "scene_move_restore_conflict"})


async def check_restore_move_boundary(
    db: AsyncSession, chapter_id: int, version_seq: int, snapshot_scene_ids: set[int] | None,
) -> None:
    if snapshot_scene_ids is None:
        moved = await db.scalar(select(SceneMove.id).where(move_touches_chapter(chapter_id)).limit(1))
        if moved is not None:
            raise restore_move_conflict("此章有跨章移动记录，请提取旧纯文本到指定场景")
        return

    incoming = set(await db.scalars(
        select(SceneMove.scene_id).join(Scene, Scene.id == SceneMove.scene_id).where(
            SceneMove.to_chapter_id == chapter_id, SceneMove.to_version_seq > version_seq,
            Scene.chapter_id == chapter_id, Scene.active.is_(True),
        )
    ))
    if incoming - snapshot_scene_ids:
        raise restore_move_conflict("此版本会移除后来移入的场景，请先处理场景归属")


async def record_scene_move(
    db: AsyncSession, *, scene_id: int, from_chapter_id: int, to_chapter_id: int,
    planning_unit_id: int | None, from_version_seq: int, to_version_seq: int,
) -> SceneMove:
    """调用方已持有作品及两章写锁，并已刷新场景及两章版本。"""
    previous = (await db.execute(select(SceneMove.to_chapter_id).where(SceneMove.scene_id == scene_id)
                                 .order_by(SceneMove.id.desc()).limit(1))).one_or_none()
    if previous is not None and previous.to_chapter_id != from_chapter_id:
        raise ConflictError("场景历史归属已变化，请刷新后重试", extra={"code": "scene_reference_invalid"})
    move = SceneMove(
        scene_id=scene_id, from_chapter_id=from_chapter_id, to_chapter_id=to_chapter_id,
        planning_unit_id=planning_unit_id, first_move=previous is None,
        from_version_seq=from_version_seq, to_version_seq=to_version_seq,
    )
    db.add(move)
    await db.flush()
    return move
