"""分镜和素材引用在作品锁下同步保存；整集替换也供 AI 终态事务调用。"""

from collections import defaultdict
from itertools import batched

from sqlalchemy import case, delete, func, select, update

from app.core.errors import BadRequestError, NotFoundError
from app.plugins.drama.episode_schemas import ShotCreate, ShotOut, ShotPatch
from app.plugins.drama.models import (
    DramaCharacter,
    DramaLook,
    DramaProp,
    DramaScene,
    DramaShot,
    DramaShotLook,
    DramaShotProp,
)
from app.plugins.drama.services.episodes import check_revision, get_episode


async def _check_refs(db, sid, values):
    look_ids, prop_ids, scene_ids = set(), set(), set()
    for fields in values:
        for key, all_ids in (("look_ids", look_ids), ("prop_ids", prop_ids)):
            ids = fields.get(key, [])
            if len(ids) != len(set(ids)):
                raise BadRequestError("同一分镜的素材引用不能重复")
            all_ids.update(ids)
        if (scene_id := fields.get("scene_id")) is not None:
            scene_ids.add(scene_id)
    look_query = (
        select(DramaLook.id)
        .join(DramaCharacter, DramaLook.character_id == DramaCharacter.id)
        .where(
            DramaCharacter.series_id == sid,
        )
    )
    for ids, stmt, id_col in (
        (look_ids, look_query, DramaLook.id),
        (prop_ids, select(DramaProp.id).where(DramaProp.series_id == sid), DramaProp.id),
        (scene_ids, select(DramaScene.id).where(DramaScene.series_id == sid), DramaScene.id),
    ):
        for batch in batched(ids, 500):
            if set(await db.scalars(stmt.where(id_col.in_(batch)))) != set(batch):
                raise NotFoundError("所选素材不属于本剧或已删除")


async def _save_refs(db, shot_id, values, *, replace):
    for key, model, column in (("look_ids", DramaShotLook, "look_id"), ("prop_ids", DramaShotProp, "prop_id")):
        if key not in values:
            continue
        if replace:
            await db.execute(delete(model).where(model.shot_id == shot_id))
        db.add_all(model(shot_id=shot_id, **{column: item}) for item in values[key])


async def _add_shots(db, eid, start, values):
    shots = []
    for i, fields in enumerate(values, 1):
        content = {k: v for k, v in fields.items() if k not in {"look_ids", "prop_ids"}}
        shots.append(DramaShot(episode_id=eid, position=start + 10 * i, **content))
    db.add_all(shots)
    await db.flush()
    for shot, fields in zip(shots, values, strict=True):
        await _save_refs(db, shot.id, fields, replace=False)
    await db.flush()
    return shots


async def replace_shots(db, series, episode, rows):
    """调用方已锁定当前作品并核对输入版本；失败时整组分镜一同回滚。"""
    if len(rows) > 200:
        raise BadRequestError("每集最多 200 个分镜")
    values = [ShotCreate.model_validate(row).model_dump() for row in rows]
    await _check_refs(db, series.id, values)
    await db.execute(delete(DramaShot).where(DramaShot.episode_id == episode.id))
    await _add_shots(db, episode.id, 0, values)
    episode.revision += 1


async def shot_details(db, rows):
    if not rows:
        return []
    shot_ids = [row.id for row in rows]
    refs = defaultdict(lambda: {"look_ids": [], "prop_ids": []})
    for model, column, key in (
        (DramaShotLook, DramaShotLook.look_id, "look_ids"),
        (DramaShotProp, DramaShotProp.prop_id, "prop_ids"),
    ):
        result = await db.execute(select(model.shot_id, column).where(model.shot_id.in_(shot_ids)).order_by(column))
        for shot_id, ref_id in result:
            refs[shot_id][key].append(ref_id)
    return [
        ShotOut.model_validate(
            {
                **{key: getattr(row, key) for key in ShotOut.model_fields if key not in {"look_ids", "prop_ids"}},
                **refs[row.id],
            }
        )
        for row in rows
    ]


async def episode_shots(db, eid):
    rows = list(
        await db.scalars(
            select(DramaShot).where(DramaShot.episode_id == eid).order_by(DramaShot.position, DramaShot.id)
        )
    )
    return await shot_details(db, rows)


async def list_shots(db, actor, eid):
    await get_episode(db, actor, eid)
    return await episode_shots(db, eid)


async def create_shot(db, actor, eid, body: ShotCreate):
    series, episode = await get_episode(db, actor, eid, "content.edit")
    count, last = (
        await db.execute(select(func.count(), func.max(DramaShot.position)).where(DramaShot.episode_id == eid))
    ).one()
    if count >= 200:
        raise BadRequestError("每集最多 200 个分镜")
    values = body.model_dump()
    await _check_refs(db, series.id, [values])
    rows = await _add_shots(db, eid, last or 0, [values])
    episode.revision += 1
    (result,) = await shot_details(db, rows)
    await db.commit()
    return result


async def _edit_shot(db, actor, shot_id, revision):
    eid = await db.scalar(select(DramaShot.episode_id).where(DramaShot.id == shot_id))
    if eid is None:
        raise NotFoundError("分镜不存在")
    series, episode = await get_episode(db, actor, eid, "content.edit")
    shot = await db.get(DramaShot, shot_id, populate_existing=True)
    if shot is None or shot.episode_id != episode.id:
        raise NotFoundError("分镜不存在")
    check_revision(shot, revision)
    return series, episode, shot


async def update_shot(db, actor, shot_id, body: ShotPatch):
    series, episode, shot = await _edit_shot(db, actor, shot_id, body.revision)
    values = body.model_dump(exclude_unset=True, exclude={"revision"})
    await _check_refs(db, series.id, [values])
    for key, value in values.items():
        if key not in {"look_ids", "prop_ids"}:
            setattr(shot, key, value)
    await _save_refs(db, shot_id, values, replace=True)
    episode.revision += 1
    shot.revision += 1
    await db.flush()
    (result,) = await shot_details(db, [shot])
    await db.commit()
    return result


async def delete_shot(db, actor, shot_id, revision):
    _, episode, shot = await _edit_shot(db, actor, shot_id, revision)
    episode.revision += 1
    await db.delete(shot)
    await db.commit()


async def order_shots(db, actor, eid, order):
    _, episode = await get_episode(db, actor, eid, "content.edit")
    check_revision(episode, order.revision)
    positions = dict(
        (
            await db.execute(
                select(DramaShot.id, DramaShot.position).where(
                    DramaShot.episode_id == eid,
                )
            )
        ).all()
    )
    if len(positions) != len(order.shot_ids) or set(positions) != set(order.shot_ids):
        raise BadRequestError("排序必须包含本集全部分镜，每条仅限一次")
    if positions:
        # 先移到原位置和新位置以外的空区间，逐行唯一约束在交换位置时也成立。
        offset = max(max(positions.values()), len(positions) * 10) + 1
        await db.execute(
            update(DramaShot)
            .where(DramaShot.episode_id == eid)
            .values(
                position=DramaShot.position + offset,
            )
        )
        new_positions = {shot_id: index * 10 for index, shot_id in enumerate(order.shot_ids, 1)}
        await db.execute(
            update(DramaShot).where(DramaShot.episode_id == eid).values(
                position=case(new_positions, value=DramaShot.id),
            )
        )
    episode.revision += 1
    result = await episode_shots(db, eid)
    await db.commit()
    return result
