"""连载工作台的日历、存稿及不可变发布稿读取。列表只选择摘要列。"""

import calendar
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, UnauthorizedError
from app.models import Chapter
from app.models.serialization import SerializationEdition as Edition
from app.models.serialization import SerializationEntry as Entry
from app.models.serialization import SerializationGoal as Goal
from app.models.serialization import SerializationPublication as Publication
from app.schemas.reader import ReaderBlock
from app.services import serialization_snapshot
from app.services.agent.export_reads import capture_document_access, verify_document_access
from app.services.ai_dispatch import visibility_sessionmaker
from app.services.authorization_service import AuthenticatedActor
from app.services.document_export.model import DocumentModel, DocumentNode
from app.services.document_export.service import render_document_attachment
from app.services.export_service import ExportFile, chapter_heading
from app.services.project_lifecycle import as_utc
from app.services.serialization_snapshot import FrozenDraft, capture_draft
from app.services.setting_category_query_service import category_read_snapshot

BEIJING = ZoneInfo("Asia/Shanghai")


@asynccontextmanager
async def workbench_read_snapshot(db: AsyncSession, user_id: int, project_id: int):
    actor = db.info.get("authenticated_actor")
    if not isinstance(actor, AuthenticatedActor) or actor.id != user_id:
        raise UnauthorizedError("未登录或登录已过期")
    # 只读端点的原会话仅完成了登录鉴权。归还连接后，在新快照中再次验证同一 token，
    # 避免大量并行 GET 各占一个连接，再相互等待第二个连接。渲染期间同样不持有该连接。
    factory = visibility_sessionmaker(db)
    await db.rollback()
    async with category_read_snapshot(factory, actor, project_id, manual=True) as reader:
        yield reader


def _day_start(day: date) -> datetime:
    # 公历第一天的北京零时早于 datetime 的 UTC 下界。
    if day == date.min:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.combine(day, time.min, BEIJING).astimezone(UTC)


def _end_after(day: date) -> datetime:
    if day == date.max:
        return datetime.max.replace(tzinfo=UTC)
    return _day_start(day + timedelta(days=1))


def _first_publications(project_id: int):
    """先在整本书全时段按章排首发，再用外层条件选月份／日期。"""
    return (
        select(
            Publication.id.label("publication_id"),
            Edition.entry_id,
            Publication.published_at,
            func.row_number().over(
                partition_by=Edition.entry_id,
                order_by=(Publication.published_at, Publication.id),
            ).label("position"),
        )
        .join(Edition, Edition.id == Publication.edition_id)
        .join(Entry, Entry.id == Edition.entry_id)
        .where(Publication.project_id == project_id, Entry.project_id == project_id)
        .subquery()
    )


def _published(project_id: int):
    return (
        select(Publication.id)
        .join(Edition, Edition.id == Publication.edition_id)
        .where(Edition.entry_id == Entry.id, Publication.project_id == project_id)
        .correlate(Entry).exists()
    )


def _chapters(project_id: int, *, stock_only: bool = False):
    published = _published(project_id)
    latest = (select(Edition.id)
              .where(Edition.entry_id == Entry.id)
              .order_by(Edition.seq.desc()).limit(1).correlate(Entry).scalar_subquery())
    statement = (
        select(
            Chapter.id.label("chapter_id"), Chapter.chapter_no, Chapter.title, Chapter.status,
            func.coalesce(Entry.stage, "revision").label("stage"),
            Entry.planned_date, func.coalesce(Entry.revision, 0).label("revision"),
            published.label("is_published"),
            case((Entry.stage == "final", latest)).label("latest_edition_id"),
        )
        .select_from(Chapter)
        .outerjoin(Entry, and_(Entry.chapter_id == Chapter.id, Entry.project_id == Chapter.project_id))
        .where(Chapter.project_id == project_id)
    )
    if stock_only:
        statement = statement.where(Entry.stage == "final", latest.is_not(None), ~published)
    return statement


def _publications(project_id: int):
    return (
        select(
            Publication.id, Chapter.id.label("chapter_id"), Edition.id.label("edition_id"),
            Edition.seq.label("edition_seq"), Edition.chapter_no, Edition.title,
            Publication.channel, Publication.published_at, Publication.created_at,
        )
        .select_from(Publication)
        .join(Edition, Edition.id == Publication.edition_id)
        .join(Entry, Entry.id == Edition.entry_id)
        .outerjoin(Chapter, and_(Chapter.id == Entry.chapter_id, Chapter.project_id == Entry.project_id))
        .where(Publication.project_id == project_id, Entry.project_id == project_id)
    )


def _row_values(row) -> dict:
    # SQLite 会移除时区标志。接口一律返回可明确归入北京时间的 UTC 时刻。
    return {key: as_utc(value) if isinstance(value, datetime) else value for key, value in row.items()}


async def _page(db: AsyncSession, statement, offset: int, limit: int) -> dict:
    total = await db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    items = (await db.execute(statement.offset(offset).limit(limit))).mappings().all()
    return {"items": [_row_values(row) for row in items], "total": total, "offset": offset, "limit": limit}


async def overview(db: AsyncSession, user_id: int, project_id: int, month: date) -> dict:
    first_day = month.replace(day=1)
    length = calendar.monthrange(first_day.year, first_day.month)[1]
    days = {first_day.replace(day=i): {"date": first_day.replace(day=i), "planned_count": 0, "first_published_count": 0}
            for i in range(1, length + 1)}
    last_day = first_day.replace(day=length)
    async with workbench_read_snapshot(db, user_id, project_id) as reader:
        goal = await reader.get(Goal, project_id)
        target = goal.daily_goal if goal else None
        stock = await reader.scalar(select(func.count()).select_from(_chapters(project_id, stock_only=True).subquery()))
        plans = await reader.execute(
            select(Entry.planned_date, func.count()).join(
                Chapter, and_(Chapter.id == Entry.chapter_id, Chapter.project_id == project_id),
            ).where(Entry.project_id == project_id,
                    Entry.planned_date.between(first_day, last_day)).group_by(Entry.planned_date)
        )
        for day, count in plans:
            days[day]["planned_count"] = count
        first = _first_publications(project_id)
        # 按真实业务日的 UTC 边界聚合。SQLite 与 PostgreSQL 共用同一结果，兼容历史夏令时。
        day_number = case(*[(first.c.published_at < _end_after(day), day.day) for day in days])
        counts = await reader.execute(
            select(day_number.label("local_day"), func.count())
            .where(first.c.position == 1, first.c.published_at >= _day_start(first_day),
                   first.c.published_at < _end_after(last_day)).group_by("local_day")
        )
        for number, count in counts:
            days[first_day.replace(day=number)]["first_published_count"] = count
        support, remaining = divmod(stock, target) if target else (None, None)
        return {"daily_goal": target, "revision": goal.revision if goal else 0, "stock_count": stock,
                "support_days": support, "remaining_chapters": remaining, "days": list(days.values())}


async def list_chapters(db: AsyncSession, user_id: int, project_id: int, *, planned_date: date | None = None,
                        stage: str | None = None, stock_only: bool = False, offset: int = 0, limit: int = 50) -> dict:
    statement = _chapters(project_id, stock_only=stock_only)
    if planned_date is not None:
        statement = statement.where(Entry.planned_date == planned_date)
    if stage is not None:
        statement = statement.where(func.coalesce(Entry.stage, "revision") == stage)
    async with workbench_read_snapshot(db, user_id, project_id) as reader:
        return await _page(reader, statement.order_by(Chapter.chapter_no, Chapter.id), offset, limit)


async def day_detail(db: AsyncSession, user_id: int, project_id: int, day: date, offset: int = 0,
                     limit: int = 50) -> dict:
    planned = _chapters(project_id).where(Entry.planned_date == day).order_by(Chapter.chapter_no, Chapter.id)
    first = _first_publications(project_id)
    first_rows = _publications(project_id).join(first, first.c.publication_id == Publication.id).where(
        first.c.position == 1, first.c.published_at >= _day_start(day), first.c.published_at < _end_after(day),
    ).order_by(Publication.published_at, Publication.id)
    async with workbench_read_snapshot(db, user_id, project_id) as reader:
        return {"planned": await _page(reader, planned, offset, limit),
                "first_published": await _page(reader, first_rows, offset, limit)}


async def list_publications(db: AsyncSession, user_id: int, project_id: int, *, channel: str | None = None,
                            chapter_id: int | None = None, offset: int = 0, limit: int = 50) -> dict:
    statement = _publications(project_id)
    if chapter_id is not None:
        statement = statement.where(Chapter.id == chapter_id)
    if channel is not None:
        statement = statement.where(Publication.channel == channel.strip())
    statement = statement.order_by(Publication.published_at.desc(), Publication.id.desc())
    async with workbench_read_snapshot(db, user_id, project_id) as reader:
        return await _page(reader, statement, offset, limit)


def _edition_data(edition: Edition) -> dict:
    return {"id": edition.id, "seq": edition.seq, "chapter_no": edition.chapter_no, "title": edition.title,
            "created_at": as_utc(edition.created_at), "content": edition.content, "blocks": edition.blocks}


def _draft_data(draft: FrozenDraft) -> dict:
    return {"chapter_no": draft.chapter_no, "title": draft.title, "content": draft.content, "blocks": draft.blocks,
            "content_revision": draft.content_revision, "fingerprint": draft.fingerprint}


def _available_editions(project_id: int, chapter_id: int, latest_id: int | None):
    published = select(Publication.id).where(
        Publication.edition_id == Edition.id, Publication.project_id == project_id,
    ).exists()
    return (
        select(Edition).join(Entry, Entry.id == Edition.entry_id)
        .where(Entry.chapter_id == chapter_id, Entry.project_id == project_id,
               (Edition.id == latest_id) | published)
    )


async def chapter_detail(db: AsyncSession, user_id: int, project_id: int, chapter_id: int,
                         edition_id: int | None = None) -> dict:
    async with workbench_read_snapshot(db, user_id, project_id) as reader:
        chapter = (await reader.execute(_chapters(project_id).where(Chapter.id == chapter_id))).mappings().one_or_none()
        if chapter is None:
            raise NotFoundError("章节不存在")
        draft = (await capture_draft(reader, project_id, chapter_id) if chapter.status != "generating" else None)
        available = _available_editions(project_id, chapter_id, chapter.latest_edition_id).with_only_columns(
            Edition.id, Edition.seq, Edition.chapter_no, Edition.title, Edition.created_at,
        ).order_by(Edition.seq.desc())
        choices = (await reader.execute(available)).mappings().all()
        selected_id = edition_id if edition_id is not None else chapter.latest_edition_id
        edition = None
        if selected_id is not None:
            if not any(row.id == selected_id for row in choices):
                raise NotFoundError("定稿不存在")
            edition = await reader.get(Edition, selected_id)
        return {"chapter": dict(chapter), "scene_id": await serialization_snapshot.chapter_scene_id(reader, chapter_id),
                "draft": _draft_data(draft) if draft else None,
                "edition": _edition_data(edition) if edition else None,
                "editions": [_row_values(row) for row in choices],
                "draft_changed": edition is not None and draft is not None and draft.fingerprint != edition.fingerprint}


async def _read_publication(db: AsyncSession, project_id: int, publication_id: int):
    statement = _publications(project_id).add_columns(Chapter.status).where(Publication.id == publication_id)
    row = (await db.execute(statement)).mappings().one_or_none()
    if row is None:
        raise NotFoundError("发布记录不存在")
    edition = await db.get(Edition, row.edition_id)
    record = _row_values(row)
    return record.pop("status"), record, edition


async def publication_detail(db: AsyncSession, user_id: int, project_id: int, publication_id: int) -> dict:
    async with workbench_read_snapshot(db, user_id, project_id) as reader:
        status, record, edition = await _read_publication(reader, project_id, publication_id)
        draft = (await capture_draft(reader, project_id, record["chapter_id"])
                 if status is not None and status != "generating" else None)
        return {"publication": record, "edition": _edition_data(edition),
                "draft": _draft_data(draft) if draft else None,
                "draft_changed": draft is not None and draft.fingerprint != edition.fingerprint}


def _edition_document(edition: Edition, suffix: str) -> tuple[DocumentModel, str]:
    heading = chapter_heading(edition)
    blocks = [ReaderBlock.model_validate(block) for block in edition.blocks] if edition.blocks is not None else None
    model = DocumentModel(nodes=[
        DocumentNode("heading", heading, level=1, source="发布章标题"),
        DocumentNode("prose", edition.content, blocks=blocks, source="发布正文"),
    ])
    short_heading = heading.encode("utf-8")[:160].decode("utf-8", errors="ignore")
    return model, f"{short_heading}-{suffix}"


async def export_publication(db: AsyncSession, user_id: int, project_id: int, publication_id: int,
                             format: str) -> ExportFile:
    # 在同一只读快照内核验导出能力并读取当次原稿，离开事务后才渲染。
    async with workbench_read_snapshot(db, user_id, project_id) as reader:
        capture = await capture_document_access(reader, user_id, project_id, [])
        _, _, edition = await _read_publication(reader, project_id, publication_id)
        model, basename = _edition_document(edition, f"发布记录{publication_id}")
    attachment = await render_document_attachment(model, format, basename)
    await verify_document_access(db, capture)
    return attachment


async def export_edition(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, edition_id: int,
) -> ExportFile:
    async with workbench_read_snapshot(db, user_id, project_id) as reader:
        capture = await capture_document_access(reader, user_id, project_id, [])
        chapter = (await reader.execute(
            _chapters(project_id).where(Chapter.id == chapter_id),
        )).mappings().one_or_none()
        if chapter is None:
            raise NotFoundError("章节不存在")
        edition = await reader.scalar(
            _available_editions(project_id, chapter_id, chapter.latest_edition_id).where(Edition.id == edition_id),
        )
        if edition is None:
            raise NotFoundError("定稿不存在")
        model, basename = _edition_document(edition, f"定稿{edition.seq}")
    attachment = await render_document_attachment(model, "txt", basename)
    await verify_document_access(db, capture)
    return attachment
