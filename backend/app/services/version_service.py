"""章节版本写入规则（只含写入原语，不含 HTTP 入口）。

版本是"某个时刻正文的快照"，即事件发生后的状态。规则：
- 手动保存（record_manual）：与最新版本相同则跳过；最新版本是未标星的 manual 且距其
  created_at 不到 MANUAL_MERGE_WINDOW 时原地更新（固定窗口：合并不延长窗口）；否则新建。正文为空时不写版本。
- AI 覆盖 / 局部改写 / 回滚之前先 ensure_baseline：当前正文非空且与最新版本不同时补一个 baseline。
- 每次新建版本后 prune：保留最新 MAX_VERSIONS 个未标星版本 + 全部标星版本；最新版本永不删除。

这些函数都不 commit，由调用方统一提交，保证"改正文 + 写版本 + 写日志"在同一个事务里。
写入版本时取 chapter.content / chapter.word_count，调用方需先更新这两个字段。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Chapter, ChapterVersion

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
    version = ChapterVersion(
        chapter_id=chapter.id,
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


async def ensure_baseline(db: AsyncSession, chapter: Chapter) -> None:
    """覆盖正文前调用：当前正文非空且未被最新版本保存过时，补一个 baseline 快照。"""
    if not chapter.content.strip():
        return
    latest = await get_latest(db, chapter.id)
    if latest is None or latest.content != chapter.content:
        await record(db, chapter, "baseline")


async def record_manual(db: AsyncSession, chapter: Chapter) -> ChapterVersion | None:
    """手动保存后调用（chapter.content 已是新正文）。"""
    if not chapter.content.strip():
        return None
    latest = await get_latest(db, chapter.id)
    if latest is not None and latest.content == chapter.content:
        return None
    if (
        latest is not None
        and latest.source == "manual"
        and not latest.starred
        and _now() - _aware(latest.created_at) < MANUAL_MERGE_WINDOW
    ):
        latest.content = chapter.content
        latest.word_count = chapter.word_count
        await flush_versioned(db)
        return latest
    return await record(db, chapter, "manual")


def add_initial(db: AsyncSession, chapters: list[Chapter], source: str, *, note: str = "") -> None:
    """给刚创建、还没有任何版本的章节批量写入第 1 个版本（如拆书导入）；正文为空的跳过。不 flush、不 commit。

    新章节没有历史版本，不需要逐章查询 seq 与裁剪，因此批量 add（导入可达 3000 章）。
    """
    db.add_all(
        ChapterVersion(
            chapter_id=c.id,
            seq=1,
            source=source,
            content=c.content,
            word_count=c.word_count,
            starred=False,
            note=note[:200],
        )
        for c in chapters
        if c.content.strip()
    )
