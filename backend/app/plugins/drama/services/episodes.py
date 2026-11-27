"""剧和集的作品归属、快照与乐观锁。所有写入都先取得作品授权锁。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter
from app.plugins.drama.episode_schemas import EpisodeCreate, EpisodeOut, EpisodePatch, SeriesCreate, SeriesPatch
from app.plugins.drama.models import DramaEpisode, DramaEpisodeSource, DramaSeries
from app.plugins.drama.styles import STYLES
from app.services import project_access
from app.services.project_access import Capability
from app.services.rich_document_render import load_chapter_projections


def check_revision(row, revision: int):
    if row.revision != revision:
        raise ConflictError("内容已修改，请刷新后重试", extra={"code": "stale_revision"})


def check_style(style: str):
    if style not in STYLES:
        raise BadRequestError("请选择已有的短剧风格")


async def get_series(db: AsyncSession, actor: int, sid: int, capability: Capability = "content.read"):
    pid = await db.scalar(select(DramaSeries.project_id).where(DramaSeries.id == sid))
    if pid is None:
        raise NotFoundError("短剧不存在")
    await project_access.require_project(db, actor, pid, capability, for_update=capability == "content.edit")
    row = await db.get(DramaSeries, sid, populate_existing=True)
    if row is None or row.project_id != pid:
        raise NotFoundError("短剧不存在")
    return row


async def get_episode(db: AsyncSession, actor: int, eid: int, capability: Capability = "content.read"):
    sid = await db.scalar(select(DramaEpisode.series_id).where(DramaEpisode.id == eid))
    if sid is None:
        raise NotFoundError("剧集不存在")
    series = await get_series(db, actor, sid, capability)
    row = await db.get(DramaEpisode, eid, populate_existing=True)
    if row is None or row.series_id != series.id:
        raise NotFoundError("剧集不存在")
    return series, row


async def list_series(db, actor, pid):
    await project_access.require_project(db, actor, pid, "content.read")
    return (
        await db.scalars(
            select(DramaSeries).where(DramaSeries.project_id == pid).order_by(DramaSeries.sort, DramaSeries.id)
        )
    ).all()


async def create_series(db, actor, pid, body: SeriesCreate):
    await project_access.require_project(db, actor, pid, "content.edit", for_update=True)
    check_style(body.style)
    count, last = (
        await db.execute(select(func.count(), func.max(DramaSeries.sort)).where(DramaSeries.project_id == pid))
    ).one()
    if count >= 20:
        raise BadRequestError("每部作品最多创建 20 部剧")
    row = DramaSeries(project_id=pid, sort=(last or 0) + 1, **body.model_dump())
    db.add(row)
    await db.commit()
    return row


def apply_patch(row, body):
    check_revision(row, body.revision)
    for key, value in body.model_dump(exclude_unset=True, exclude={"revision"}).items():
        setattr(row, key, value)
    row.revision += 1


async def update_series(db, actor, sid, body: SeriesPatch):
    row = await get_series(db, actor, sid, "content.edit")
    if body.style is not None:
        check_style(body.style)
    apply_patch(row, body)
    await db.commit()
    return row


async def delete_series(db, actor, sid, revision):
    row = await get_series(db, actor, sid, "content.edit")
    check_revision(row, revision)
    await db.delete(row)
    await db.commit()


async def list_episodes(db, actor, sid):
    await get_series(db, actor, sid)
    stmt = select(DramaEpisode).options(defer(DramaEpisode.script), defer(DramaEpisode.source_text))
    return (await db.scalars(stmt.where(DramaEpisode.series_id == sid).order_by(DramaEpisode.number))).all()


async def episode_detail(db, row):
    sources = list(
        await db.scalars(
            select(DramaEpisodeSource).where(DramaEpisodeSource.episode_id == row.id).order_by(DramaEpisodeSource.sort)
        )
    )
    return EpisodeOut.model_validate(
        {
            **{key: getattr(row, key) for key in EpisodeOut.model_fields if key not in {"sources", "chapter_ids"}},
            "sources": sources,
            "chapter_ids": [s.chapter_id for s in sources if s.chapter_id is not None],
        }
    )


async def read_episode(db, actor, eid):
    _, row = await get_episode(db, actor, eid)
    return await episode_detail(db, row)


async def _source_chapters(db, pid, ids):
    if not ids:
        return []
    if len(ids) > 20 or len(set(ids)) != len(ids):
        raise BadRequestError("请选择至多 20 个不重复的连续章节")
    # 排名基于本作品全部章节，允许章节编号本身有空档或相同。
    ranked = (
        select(Chapter.id, func.row_number().over(order_by=(Chapter.chapter_no, Chapter.id)).label("pos"))
        .where(Chapter.project_id == pid)
        .subquery()
    )
    selected = (
        await db.execute(
            select(ranked.c.pos, Chapter.status)
            .join(Chapter, Chapter.id == ranked.c.id)
            .where(ranked.c.id.in_(ids))
            .order_by(ranked.c.pos)
        )
    ).all()
    if len(selected) != len(ids):
        raise NotFoundError("所选章节不存在")
    if selected[-1].pos - selected[0].pos + 1 != len(ids):
        raise BadRequestError("请选择连续章节")
    if any(status == "generating" for _, status in selected):
        raise ConflictError("所选章节正在生成，请稍后重试", extra={"code": "chapter_generating"})
    snapshots = await load_chapter_projections(db, pid, chapter_ids=ids, include_formatting=False)
    return [snapshot.chapter for snapshot in snapshots]


async def create_episode(db, actor, sid, body: EpisodeCreate):
    series = await get_series(db, actor, sid, "content.edit")
    count, last = (
        await db.execute(select(func.count(), func.max(DramaEpisode.number)).where(DramaEpisode.series_id == sid))
    ).one()
    if count >= 200:
        raise BadRequestError("每部剧最多创建 200 集")
    chapters = await _source_chapters(db, series.project_id, body.chapter_ids)
    source = body.source_text if body.source_text is not None else "\n\n".join(c.content for c in chapters)
    if len(source) > 60_000:
        raise BadRequestError("原始内容不能超过 6 万字")
    row = DramaEpisode(
        series_id=sid, number=(last or 0) + 1, title=body.title, resolution=body.resolution, source_text=source
    )
    db.add(row)
    await db.flush()
    db.add_all(
        DramaEpisodeSource(episode_id=row.id, chapter_id=c.id, chapter_title=c.title, sort=i)
        for i, c in enumerate(chapters, 1)
    )
    await db.flush()
    result = await episode_detail(db, row)
    await db.commit()
    return result


async def update_episode(db, actor, eid, body: EpisodePatch):
    _, row = await get_episode(db, actor, eid, "content.edit")
    apply_patch(row, body)
    result = await episode_detail(db, row)
    await db.commit()
    return result


async def delete_episode(db, actor, eid, revision):
    _, row = await get_episode(db, actor, eid, "content.edit")
    check_revision(row, revision)
    await db.delete(row)
    await db.commit()
