"""大纲进阶：大纲概览（含细纲与展开状态）、追加采纳（续写结果）、上移 / 下移。

- 排序只改 `sort`（卷内排列顺序），不改任何章节号。列表顺序为 (volume_no, sort, chapter_no, id)。
- 追加：章节号从 next_chapter_no 起顺延；卷号 / 卷名取自每条（续写草稿已按「接在当前卷后 / 开新卷」算好）。
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chapter, Outline
from app.schemas.outline import OutlineAppendRequest, OutlineDetail, OutlineMoveRequest
from app.schemas.story import OutlineOut
from app.services import plan_service, story_service
from app.services.project_service import get_manageable_project


async def list_outlines(db: AsyncSession, project_id: int) -> list[Outline]:
    return await story_service.list_items(db, story_service.OUTLINES, project_id)


async def next_chapter_no(db: AsyncSession, project_id: int) -> int:
    """全书最后一章之后的章节号：max(大纲的最大章节号, 已有章节的最大章节号) + 1。

    每条大纲占 max(1, 已建章数) 个章节号，已建的章节都在 chapters 里，因此两者取大即可。
    """
    outline_end = await db.scalar(select(func.max(Outline.chapter_no)).where(Outline.project_id == project_id))
    chapter_end = await db.scalar(select(func.max(Chapter.chapter_no)).where(Chapter.project_id == project_id))
    return max(outline_end or 0, chapter_end or 0) + 1


async def last_volume(db: AsyncSession, project_id: int) -> tuple[int, str]:
    """当前最后一卷 (卷号, 卷名)；没有大纲时 (0, "")。"""
    outlines = await list_outlines(db, project_id)
    if not outlines:
        return 0, ""
    last_no = max(o.volume_no for o in outlines)
    title = next((o.volume_title for o in outlines if o.volume_no == last_no and o.volume_title), "")
    return last_no, title


async def outline_tree(db: AsyncSession, user_id: int, project_id: int) -> list[OutlineDetail]:
    await get_manageable_project(db, user_id, project_id)
    outlines = await list_outlines(db, project_id)
    plans = await plan_service.plans_of(db, [o.id for o in outlines])
    all_plans = [p for items in plans.values() for p in items]
    outs = {p.id: p for p in await plan_service.plan_outs(db, project_id, all_plans)}
    singles: dict[int, int] = {}
    rows = await db.execute(
        select(Chapter.outline_id, Chapter.id)
        .where(Chapter.project_id == project_id, Chapter.outline_id.is_not(None), Chapter.plan_id.is_(None))
        .order_by(Chapter.chapter_no.desc(), Chapter.id.desc())
    )
    for oid, cid in rows.all():
        singles[oid] = cid  # 降序遍历，最终保留章节号最小的一章
    result = []
    for o in outlines:
        items = [outs[p.id] for p in plans.get(o.id, [])]
        result.append(
            OutlineDetail(
                **OutlineOut.model_validate(o).model_dump(),
                chapter_id=None if items else singles.get(o.id),
                plans=items,
                plan_count=len(items),
                built_count=len([p for p in items if p.chapter_id is not None]),
            )
        )
    return result


async def append_outlines(db: AsyncSession, user_id: int, project_id: int, data: OutlineAppendRequest) -> list[Outline]:
    """采纳续写结果：追加到末尾，章节号从当前最后一章之后顺延。单事务。"""
    await get_manageable_project(db, user_id, project_id, lock=True)
    start = await next_chapter_no(db, project_id)
    sort = await story_service.next_sort(db, Outline, project_id)
    created = []
    for i, item in enumerate(data.items):
        title = item.title.strip()
        if not title:
            continue
        o = Outline(
            project_id=project_id,
            volume_no=item.volume_no,
            volume_title=item.volume_title.strip(),
            chapter_no=start + len(created),
            title=title,
            summary=item.summary.strip(),
            sort=sort + i,
        )
        db.add(o)
        created.append(o)
    await db.commit()
    for o in created:
        await db.refresh(o)
    return created


async def move_outline(
    db: AsyncSession, user_id: int, project_id: int, outline_id: int, data: OutlineMoveRequest
) -> list[Outline]:
    """卷内上移 / 下移：先把该卷的 sort 规整为 1..n（按当前顺序），再与相邻一条交换。已在边界时不变。返回该卷的大纲。"""
    await get_manageable_project(db, user_id, project_id, lock=True)
    outline = await plan_service.get_project_outline(db, user_id, project_id, outline_id)
    volume = [o for o in await list_outlines(db, project_id) if o.volume_no == outline.volume_no]
    idx = next(i for i, o in enumerate(volume) if o.id == outline.id)
    target = idx - 1 if data.direction == "up" else idx + 1
    if 0 <= target < len(volume):
        volume[idx], volume[target] = volume[target], volume[idx]
    base = min((o.sort for o in volume), default=0)
    for i, o in enumerate(volume):
        o.sort = base + i
    await db.commit()
    return volume
