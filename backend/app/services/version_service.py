"""章节版本写入规则（只含写入原语，不含 HTTP 入口）。

版本是"某个时刻正文的快照"，即事件发生后的状态。规则：
- 手动保存（record_manual）：与最新版本相同则跳过；最新版本是未标星的 manual 且距其
  created_at 不到 MANUAL_MERGE_WINDOW 时原地更新（固定窗口：合并不延长窗口）；否则新建。
  初始空稿不写，清空、结构或本地覆盖变化强制保留可撤销边界。
- AI 覆盖 / 局部改写 / 回滚之前先 ensure_baseline：当前正文非空且与最新版本不同时补一个 baseline。
- 每次新建版本后 prune：保留最新 MAX_VERSIONS 个未标星版本 + 全部标星版本；最新版本永不删除。

这些函数都不 commit，由调用方统一提交，保证"改正文 + 写版本 + 写日志"在同一个事务里。
写入前调用方必须经 scene_service 更新权威场景与章投影；版本同时保存结构与原始本地意图。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Chapter, ChapterVersion, Scene

MANUAL_MERGE_WINDOW = timedelta(minutes=10)
MAX_VERSIONS = 50


def _now() -> datetime:
    """服务端当前时间（UTC）；合并判断不依赖客户端时钟。"""
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    # SQLite 读出的时间不带时区（CURRENT_TIMESTAMP 为 UTC）
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def is_version_conflict(exc: IntegrityError) -> bool:
    original = exc.orig
    if str(original) == "UNIQUE constraint failed: chapter_versions.chapter_id, chapter_versions.seq":
        return True
    return any(
        getattr(error, "sqlstate", None) == "23505"
        and getattr(error, "constraint_name", None) == "uq_chapter_versions_chapter_id"
        for error in (original, getattr(original, "__cause__", None))
    )


async def flush_versioned(db: AsyncSession) -> None:
    """只翻译业务版本唯一竞争；事务回滚仍由外层 HTTP/审计边界负责。"""
    try:
        await db.flush()
    except IntegrityError as exc:
        if not is_version_conflict(exc):
            raise
        raise ConflictError("正文同时被其他操作修改，请刷新后重试") from exc


async def get_latest(db: AsyncSession, chapter_id: int) -> ChapterVersion | None:
    return await db.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter_id)
        .order_by(ChapterVersion.seq.desc())
        .limit(1)
    )


async def _next_seq(db: AsyncSession, chapter_id: int) -> int:
    current = await db.scalar(select(func.max(ChapterVersion.seq)).where(ChapterVersion.chapter_id == chapter_id))
    return (current or 0) + 1


async def prune(db: AsyncSession, chapter_id: int) -> None:
    """删除超出上限的最旧未标星版本。最新版本若未标星必然在前 MAX_VERSIONS 个之内，因此不会被删。"""
    stale = list(
        await db.scalars(
            select(ChapterVersion.id)
            .where(ChapterVersion.chapter_id == chapter_id, ChapterVersion.starred.is_(False))
            .order_by(ChapterVersion.seq.desc())
            .offset(MAX_VERSIONS)
        )
    )
    if stale:
        await db.execute(delete(ChapterVersion).where(ChapterVersion.id.in_(stale)))


async def record(
    db: AsyncSession, chapter: Chapter, source: str, *, log_id: int | None = None, note: str = ""
) -> ChapterVersion:
    """以当前正文新建一个版本并裁剪。"""
    from app.services.scene_service import capture_snapshot

    version = ChapterVersion(
        chapter_id=chapter.id,
        scene_snapshot=await capture_snapshot(db, chapter),
        seq=await _next_seq(db, chapter.id),
        source=source,
        content=chapter.content,
        word_count=chapter.word_count,
        starred=False,
        note=note[:200],
        generation_log_id=log_id,
    )
    db.add(version)
    await flush_versioned(db)
    await prune(db, chapter.id)
    return version


async def ensure_baseline(db: AsyncSession, chapter: Chapter, *, force: bool = False) -> None:
    """覆盖前保存真实结构；结构操作即使无正文也保留基线。"""
    from app.services.scene_service import capture_snapshot

    if not force and not chapter.content.strip():
        return
    latest = await get_latest(db, chapter.id)
    snapshot = await capture_snapshot(db, chapter)
    if latest is None or latest.content != chapter.content or latest.scene_snapshot != snapshot:
        await record(db, chapter, "baseline")


def same_structure(first: dict | None, second: dict | None) -> bool:
    """仅普通正文可合并；身份、分隔、摘要、覆盖意图变化都强制留边界。"""
    if first is None or second is None:
        return False
    def shape(value):
        return {**value, "scenes": [{k: v for k, v in scene.items() if k != "content"}
                                   for scene in value["scenes"]]}
    return shape(first) == shape(second)


async def record_manual(db: AsyncSession, chapter: Chapter) -> ChapterVersion | None:
    """相同结构的普通正文按原10分钟窗口合并；清空强制保留可撤销边界。"""
    from app.services.scene_service import capture_snapshot

    latest = await get_latest(db, chapter.id)
    snapshot = await capture_snapshot(db, chapter)
    if latest is not None and latest.content == chapter.content and latest.scene_snapshot == snapshot:
        return None
    if not chapter.content.strip():
        return await record(db, chapter, "manual", note="清空正文") if latest is not None else None
    if (
        latest is not None
        and latest.source == "manual"
        and not latest.starred
        and not latest.note
        and bool(latest.content.strip())
        and same_structure(latest.scene_snapshot, snapshot)
        and _now() - _aware(latest.created_at) < MANUAL_MERGE_WINDOW
    ):
        latest.content = chapter.content
        latest.word_count = chapter.word_count
        latest.scene_snapshot = snapshot
        await flush_versioned(db)
        return latest
    return await record(db, chapter, "manual")


async def add_initial(db: AsyncSession, chapters: list[Chapter], source: str, *, note: str = "") -> None:
    """新建章节批量初稿；调用方已显式初始化场景，没有规划单元或旧历史。"""
    ids = [chapter.id for chapter in chapters]
    scenes = list(await db.scalars(select(Scene).where(Scene.chapter_id.in_(ids), Scene.active.is_(True))
                                   .order_by(Scene.sort, Scene.id))) if ids else []
    snapshots: dict[int, list[dict]] = {}
    for scene in scenes:
        snapshots.setdefault(scene.chapter_id, []).append({
            "id": scene.id, "title": scene.title, "content": scene.content, "summary": scene.summary,
            "sort": scene.sort, "separator_before": scene.separator_before, "planning": None,
        })
    for chapter in chapters:
        if chapter.id not in snapshots:
            raise ConflictError("新章节缺少初始场景")
        if chapter.content.strip():
            db.add(ChapterVersion(
                chapter_id=chapter.id, seq=1, source=source, content=chapter.content,
                word_count=chapter.word_count, starred=False, note=note[:200],
                scene_snapshot={"schema_version": 1, "scenes": snapshots[chapter.id]},
            ))
