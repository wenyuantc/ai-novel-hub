"""拆书导入的解析记录：上传解析、列表 / 详情 / 单章全文、重切与校正（修订号乐观并发）、预估、开始导入、删除与过期清理。

- 所有入口按 user_id 校验归属；他人的记录与不存在统一 404。
- 每个用户最多保留 MAX_PENDING 个未导入的解析；创建满 TTL 仍未导入的在上传 / 列表前与应用启动时清理。
- 开始导入在同一事务内：新建项目（只新建）→ 按导入范围写入分卷大纲（每章一条、摘要为空）与章节正文（status = done）
  → 每章一个 source = import 的初始版本 → 删除解析记录 → task_service.create_task("book.import") 提交。
  任何一步失败整体回滚。
"""

import hashlib
import posixpath
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import anyio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import BookImport, Chapter, Outline, Project
from app.schemas.book_import import (
    BookChapterText,
    BookChapterView,
    BookImportBrief,
    BookImportOut,
    BookOpRequest,
    EstimateOut,
    EstimateRequest,
    ResplitRequest,
    StartRequest,
    StartResult,
    WarningCounts,
)
from app.schemas.task import TaskOut
from app.services import kb_parser, task_service, version_service
from app.services.book_import import reverse, splitter
from app.services.chapter_service import count_words

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

MAX_PENDING = 5
TTL = timedelta(days=7)
NOT_FOUND = "解析记录不存在"
STALE = "解析结果已被修改，请刷新后重试"
# 修订号过期的 409 在响应体里带 code（AppError.extra），前端据此区分其他 409（如已有拆书任务），不匹配 detail 文案
STALE_CODE = "stale_revision"
# 预估：每批的模板开销与已知角色名单、汇总上限、风格样章、深度分析每章的上下文开销（字）
DIGEST_OVERHEAD = 2000
KNOWN_OVERHEAD = 1500
SYNTHESIZE_INPUT = 40000
STYLE_INPUT = 6000
ANALYZE_OVERHEAD = 3000
# 拆书导入后的向导进度：四步都视为已完成，概览不再提示向导未完成（步骤名与前端 WizardPage 一致）
WIZARD_DONE = ["world", "careers", "characters", "outline"]


def _stale() -> ConflictError:
    return ConflictError(STALE, extra={"code": STALE_CODE})


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------- 清理与查询 ----------------


async def purge_expired(db: AsyncSession) -> int:
    result = await db.execute(delete(BookImport).where(BookImport.expires_at < _now()))
    await db.commit()
    return result.rowcount or 0


async def purge_expired_all(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """应用启动时清理过期的解析记录。"""
    async with sessionmaker() as db:
        await purge_expired(db)


async def get_owned_import(db: AsyncSession, user_id: int, import_id: int, *, lock: bool = False) -> BookImport:
    stmt = select(BookImport).where(BookImport.id == import_id, BookImport.user_id == user_id)
    if lock:
        # PostgreSQL 行锁防止并发开始导入；SQLite 忽略 FOR UPDATE
        stmt = stmt.with_for_update()
    record = await db.scalar(stmt.execution_options(populate_existing=True))
    if record is None:
        raise NotFoundError(NOT_FOUND)
    return record


def _build_out(record: BookImport) -> BookImportOut:
    views, counts = splitter.chapter_views(record.text, record.chapters)
    return BookImportOut(
        id=record.id,
        filename=record.filename,
        kind=record.kind,  # type: ignore[arg-type]
        size=record.size,
        book_title=record.book_title,
        split_mode=record.split_mode,  # type: ignore[arg-type]
        split_pattern=record.split_pattern,
        revision=record.revision,
        notices=list(record.notices or []),
        chapters=[BookChapterView(**v) for v in views],
        warning_counts=WarningCounts(**counts),
        chapter_count=len(views),
        total_chars=sum(v["chars"] for v in views),
        too_many=len(views) > splitter.MAX_IMPORT_CHAPTERS,
        max_import_chapters=splitter.MAX_IMPORT_CHAPTERS,
        expires_at=record.expires_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def to_out(record: BookImport) -> BookImportOut:
    # 3000 章 / 500 万字的字数与提示统计放到线程里算，不阻塞事件循环
    return await anyio.to_thread.run_sync(_build_out, record)


async def list_imports(db: AsyncSession, user_id: int) -> list[BookImportBrief]:
    await purge_expired(db)
    rows = await db.scalars(
        select(BookImport).where(BookImport.user_id == user_id).order_by(BookImport.updated_at.desc(), BookImport.id)
    )
    records = list(rows)

    def build() -> list[BookImportBrief]:
        return [
            BookImportBrief(
                id=r.id,
                filename=r.filename,
                book_title=r.book_title,
                chapter_count=len(r.chapters or []),
                total_chars=count_words(r.text),
                expires_at=r.expires_at,
                updated_at=r.updated_at,
            )
            for r in records
        ]

    # 最多 5 份、每份 ≤ 500 万字：字数统计放到线程里，不阻塞事件循环
    return await anyio.to_thread.run_sync(build)


async def get_import(db: AsyncSession, user_id: int, import_id: int) -> BookImportOut:
    return await to_out(await get_owned_import(db, user_id, import_id))


async def chapter_text(db: AsyncSession, user_id: int, import_id: int, index: int) -> BookChapterText:
    record = await get_owned_import(db, user_id, import_id)
    if not 1 <= index <= len(record.chapters):
        raise NotFoundError("章节不存在")
    chapter = record.chapters[index - 1]
    lines = [line for line, _ in splitter.chapter_lines(record.text, chapter["segments"])]
    return BookChapterText(index=index, title=chapter["title"], lines=lines)


# ---------------- 上传 ----------------


def _clean_filename(filename: str) -> str:
    name = posixpath.basename((filename or "").replace("\\", "/")).strip()
    return name[-255:] or "未命名.txt"


async def upload(db: AsyncSession, user_id: int, filename: str, data: bytes) -> BookImportOut:
    filename = _clean_filename(filename)
    splitter.detect_kind(filename)
    if len(data) > kb_parser.MAX_FILE_BYTES:
        raise BadRequestError(kb_parser.TOO_LARGE)
    await purge_expired(db)
    pending = await db.scalar(select(func.count(BookImport.id)).where(BookImport.user_id == user_id)) or 0
    if pending >= MAX_PENDING:
        raise ConflictError(f"未导入的解析最多保留 {MAX_PENDING} 个，请先导入或删除已有的解析")
    parsed = await anyio.to_thread.run_sync(splitter.parse_book, filename, data)
    record = BookImport(
        user_id=user_id,
        filename=filename,
        kind=parsed.kind,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        book_title=parsed.book_title,
        text=parsed.text,
        split_mode="auto",
        split_pattern=None,
        chapters=parsed.chapters,
        notices=parsed.notices,
        doc_spans=parsed.doc_spans,
        revision=0,
        expires_at=_now() + TTL,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return await to_out(record)


# ---------------- 重切与校正 ----------------


async def _save_chapters(db: AsyncSession, record: BookImport, revision: int, values: dict[str, Any]) -> BookImportOut:
    """条件 UPDATE（revision 未变才写入并 +1）；修订号过期 → 409。"""
    result = await db.execute(
        update(BookImport)
        .where(BookImport.id == record.id, BookImport.revision == revision)
        .values(**values, revision=BookImport.revision + 1)
    )
    await db.commit()
    if result.rowcount != 1:
        raise _stale()
    await db.refresh(record)
    return await to_out(record)


async def resplit(db: AsyncSession, user_id: int, import_id: int, req: ResplitRequest) -> BookImportOut:
    """重新切章（清空之前的手工校正）。"""
    record = await get_owned_import(db, user_id, import_id)
    if record.revision != req.revision:
        raise _stale()
    if req.mode == "regex":
        pattern = splitter.validate_pattern(req.pattern or "")
        chapters, notices = await anyio.to_thread.run_sync(splitter.split_regex, record.kind, record.text, pattern)
    else:
        pattern = None
        chapters, notices = await anyio.to_thread.run_sync(
            splitter.split_auto, record.kind, record.text, record.doc_spans
        )
    values = {"chapters": chapters, "notices": notices, "split_mode": req.mode, "split_pattern": pattern}
    return await _save_chapters(db, record, req.revision, values)


async def apply_op(db: AsyncSession, user_id: int, import_id: int, req: BookOpRequest) -> BookImportOut:
    record = await get_owned_import(db, user_id, import_id)
    if record.revision != req.revision:
        raise _stale()
    chapters = splitter.apply_op(record.text, record.chapters, req.op, req.index, title=req.title, line=req.line)
    return await _save_chapters(db, record, req.revision, {"chapters": chapters})


async def delete_import(db: AsyncSession, user_id: int, import_id: int) -> None:
    record = await get_owned_import(db, user_id, import_id)
    await db.delete(record)
    await db.commit()


# ---------------- 预估与开始 ----------------


def _ranges(total: int, import_range: tuple[int, int], reverse_range: tuple[int, int]) -> tuple[int, int, int, int]:
    a, b = import_range
    c, d = reverse_range
    if not 1 <= a <= b <= total:
        raise BadRequestError("导入范围不正确")
    if b - a + 1 > splitter.MAX_IMPORT_CHAPTERS:
        raise BadRequestError(f"一次最多导入 {splitter.MAX_IMPORT_CHAPTERS} 章，请缩小导入范围")
    if not a <= c <= d <= b:
        raise BadRequestError("反推范围必须在导入范围之内")
    return a, b, c, d


def _estimate(record: BookImport, req: EstimateRequest) -> EstimateOut:
    a, b, c, d = _ranges(len(record.chapters), req.import_range, req.reverse_range)
    bodies = [splitter.body_text(record.text, ch["segments"]) for ch in record.chapters[a - 1 : b]]
    reverse_bodies = bodies[c - a : d - a + 1]
    items = [(i, i, [len(p) for p in reverse.split_parts(x)]) for i, x in enumerate(reverse_bodies) if x.strip()]
    batches = len(reverse.plan_batches(items))
    import_chars = sum(len(x) for x in bodies)
    reverse_chars = sum(len(x) for x in reverse_bodies)
    digest = batches * (DIGEST_OVERHEAD + KNOWN_OVERHEAD) + reverse_chars
    synthesize = SYNTHESIZE_INPUT if batches else 0
    style = STYLE_INPUT if req.extract_style else 0
    analyze = import_chars + len(bodies) * ANALYZE_OVERHEAD if req.deep_analyze else 0
    return EstimateOut(
        import_chapters=len(bodies),
        import_chars=import_chars,
        reverse_chars=reverse_chars,
        batches=batches,
        digest_input=digest,
        synthesize_input=synthesize,
        style_input=style,
        analyze_input=analyze,
        total_input=digest + synthesize + style + analyze,
    )


async def estimate(db: AsyncSession, user_id: int, import_id: int, req: EstimateRequest) -> EstimateOut:
    record = await get_owned_import(db, user_id, import_id)
    return await anyio.to_thread.run_sync(_estimate, record, req)


async def start(db: AsyncSession, engine: "TaskEngine", user_id: int, import_id: int, req: StartRequest) -> StartResult:
    from app.services.quota_service import check_project_creation

    record = await get_owned_import(db, user_id, import_id)
    if record.revision != req.revision:
        raise _stale()
    a, b, c, d = _ranges(len(record.chapters), req.import_range, req.reverse_range)
    title = req.title.strip()
    if not title:
        raise BadRequestError("书名不能为空")
    # 拆书任务互斥的快速检查（建项目之前）；validate_and_claim 里还会再查一次
    if await reverse.open_import(db, user_id) is not None:
        raise ConflictError(reverse.OPEN_CONFLICT)
    selected = record.chapters[a - 1 : b]

    def bodies() -> list[tuple[str, int]]:
        out = []
        for ch in selected:
            content = splitter.body_text(record.text, ch["segments"])
            out.append((content, count_words(content)))
        return out

    # 3000 章 / 500 万字的正文拼接与字数统计放到线程里
    contents = await anyio.to_thread.run_sync(bodies)
    await check_project_creation(db, user_id)
    record = await get_owned_import(db, user_id, import_id, lock=True)
    if record.revision != req.revision:
        raise _stale()
    try:
        project = Project(
            user_id=user_id,
            title=title,
            genre=req.genre.strip(),
            status="writing",
            wizard_progress={"done": list(WIZARD_DONE), "skipped": [], "current": None},
        )
        db.add(project)
        await db.flush()
        outlines = [
            Outline(
                project_id=project.id,
                volume_no=max(1, ch["volume_no"]),
                volume_title=ch["volume_title"],
                chapter_no=no,
                title=ch["title"] or f"第{no}章",
                summary="",
                sort=no,
            )
            for no, ch in enumerate(selected, start=1)
        ]
        db.add_all(outlines)
        await db.flush()
        chapters = []
        for no, (ch, outline, (content, words)) in enumerate(zip(selected, outlines, contents, strict=True), start=1):
            chapters.append(
                Chapter(
                    project_id=project.id,
                    outline_id=outline.id,
                    chapter_no=no,
                    title=ch["title"] or f"第{no}章",
                    content=content,
                    status="done",
                    word_count=words,
                )
            )
        db.add_all(chapters)
        await db.flush()
        from app.services.scene_service import initialize_chapter

        for chapter in chapters:
            await initialize_chapter(db, chapter)
        await version_service.add_initial(db, chapters, "import", note="拆书导入")
        await db.delete(record)
        await db.flush()
        params = {
            "project_id": project.id,
            # 导入后章节从 1 起重新编号：反推范围换算为新章号
            "reverse_chapter_nos": [c - a + 1, d - a + 1],
            "extract_style": req.extract_style,
            "deep_analyze": req.deep_analyze,
            "ai_config_id": req.ai_config_id,
        }
        task: TaskOut = await task_service.create_task(db, engine, user_id, reverse.KIND, params)
    except Exception:
        # 校验失败 / 撞唯一索引等：项目、章节与解析记录的删除一起回滚，不留残缺项目
        await db.rollback()
        raise
    return StartResult(project_id=project.id, task=task)


async def skip_task(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int) -> TaskOut:
    return await reverse.skip_paused(db, engine, user_id, task_id)
