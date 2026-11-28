"""剧级素材库、角色默认造型与集的出场素材。写入由调用者统一提交。"""

from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Character, WorldSetting
from app.plugins.drama.models import (
    DramaCharacter,
    DramaEpisode,
    DramaEpisodeLook,
    DramaEpisodeProp,
    DramaEpisodeScene,
    DramaLook,
    DramaProp,
    DramaScene,
    DramaSeries,
)
from app.plugins.drama.services import linking
from app.plugins.drama.services.episodes import check_revision, get_episode, get_series
from app.plugins.drama.services.image_lifecycle import discard_owner_images

MODEL = {"characters": DramaCharacter, "looks": DramaLook, "scenes": DramaScene, "props": DramaProp}
MEMBERSHIP = {
    DramaLook: (DramaEpisodeLook, "look_id"),
    DramaProp: (DramaEpisodeProp, "prop_id"),
    DramaScene: (DramaEpisodeScene, "scene_id"),
}


def _fields(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


async def _series_id(db, model, resource_id):
    if model is DramaSeries:
        return resource_id
    if model is DramaLook:
        query = select(DramaCharacter.series_id).join(DramaLook, DramaLook.character_id == DramaCharacter.id)
    else:
        query = select(model.series_id)
    return await db.scalar(query.where(model.id == resource_id))


async def authorized(db, actor_id, model, resource_id, *, write):
    capability = "content.edit" if write else "content.read"
    if model is DramaEpisode:
        return await get_episode(db, actor_id, resource_id, capability)
    sid = await _series_id(db, model, resource_id)
    if sid is None:
        raise NotFoundError("短剧资源不存在")
    series = await get_series(db, actor_id, sid, capability)
    if model is DramaSeries:
        return series, series
    row = await db.get(model, resource_id, populate_existing=True)
    if row is None or await _series_id(db, model, resource_id) != sid:
        raise NotFoundError("短剧资源不存在")
    return series, row


def _for_episode(query, model, episode_id):
    if episode_id is None:
        return query
    rel, key = MEMBERSHIP[model]
    return query.join(rel, getattr(rel, key) == model.id).where(rel.episode_id == episode_id)


async def library(db: AsyncSession, series_id: int, episode_id: int | None = None, *, include_images=False) -> dict:
    look_query = (
        select(DramaLook)
        .join(DramaCharacter, DramaCharacter.id == DramaLook.character_id)
        .where(
            DramaCharacter.series_id == series_id,
        )
        .order_by(DramaLook.is_default.desc(), DramaLook.sort, DramaLook.id)
    )
    look_query = _for_episode(look_query, DramaLook, episode_id)
    overviews = None
    if include_images:
        from app.plugins.drama.services.images import library_overviews

        look_ids = look_query.with_only_columns(DramaLook.id, maintain_column_froms=True).order_by(None)
        overviews = await library_overviews(db, series_id, look_ids)
    looks = defaultdict(list)
    for look in await db.scalars(look_query):
        value = _fields(look)
        if overviews is not None:
            value.update(overviews[look.id])
        looks[look.character_id].append(value)
    char_query = (
        select(DramaCharacter, Character.name)
        .outerjoin(
            Character,
            Character.id == DramaCharacter.novel_character_id,
        )
        .where(DramaCharacter.series_id == series_id)
        .order_by(DramaCharacter.sort, DramaCharacter.id)
    )
    characters = [
        dict(_fields(char), novel_character_name=name, looks=looks[char.id])
        for char, name in await db.execute(char_query)
        if episode_id is None or char.id in looks
    ]
    scene_query = (
        select(DramaScene, WorldSetting.title)
        .outerjoin(
            WorldSetting,
            WorldSetting.id == DramaScene.novel_world_setting_id,
        )
        .where(DramaScene.series_id == series_id)
        .order_by(DramaScene.id)
    )
    scenes = [
        dict(_fields(scene), novel_world_setting_title=title)
        for scene, title in await db.execute(_for_episode(scene_query, DramaScene, episode_id))
    ]
    prop_query = select(DramaProp).where(DramaProp.series_id == series_id).order_by(DramaProp.id)
    props = [_fields(row) for row in await db.scalars(_for_episode(prop_query, DramaProp, episode_id))]
    return {"characters": characters, "scenes": scenes, "props": props}


async def describe(db, row):
    data = _fields(row)
    if isinstance(row, DramaCharacter):
        query = (
            select(DramaLook)
            .where(DramaLook.character_id == row.id)
            .order_by(
                DramaLook.is_default.desc(),
                DramaLook.sort,
                DramaLook.id,
            )
        )
        data["looks"] = [_fields(look) for look in await db.scalars(query)]
        data["novel_character_name"] = (
            await db.scalar(
                select(Character.name).where(Character.id == row.novel_character_id),
            )
            if row.novel_character_id
            else None
        )
    if isinstance(row, DramaScene):
        data["novel_world_setting_title"] = (
            await db.scalar(
                select(WorldSetting.title).where(WorldSetting.id == row.novel_world_setting_id),
            )
            if row.novel_world_setting_id
            else None
        )
    return data


async def _require_unique(db, row, values):
    if isinstance(row, DramaScene):
        tests = [
            DramaScene.series_id == row.series_id,
            DramaScene.location == values.get("location", row.location),
            DramaScene.time_of_day == values.get("time_of_day", row.time_of_day),
        ]
    else:
        model = type(row)
        scope = "character_id" if model is DramaLook else "series_id"
        tests = [getattr(model, scope) == getattr(row, scope), model.name == values.get("name", row.name)]
    if row.id is not None:
        tests.append(type(row).id != row.id)
    if await db.scalar(select(type(row).id).where(*tests).limit(1)) is not None:
        raise ConflictError("此素材名称已存在", extra={"code": "duplicate_asset"})


async def _clear_default(db, character_id):
    old = await db.scalar(select(DramaLook).where(DramaLook.character_id == character_id, DramaLook.is_default))
    if old:
        old.is_default = False
        old.revision += 1
        await db.flush()


async def _link_if_requested(db, series, row, values):
    if isinstance(row, DramaCharacter) and "novel_character_id" in values:
        await linking.link_character(db, series.project_id, row, target_id=values.get("novel_character_id"))
    if isinstance(row, DramaScene) and "novel_world_setting_id" in values:
        await linking.link_scene(db, series.project_id, row, target_id=values.get("novel_world_setting_id"))


async def create_asset(db, actor_id, kind, parent_id, values, *, episode_id=None, episode_revision=None):
    model = MODEL[kind]
    parent_model = DramaCharacter if model is DramaLook else DramaSeries
    series, parent = await authorized(db, actor_id, parent_model, parent_id, write=True)
    episode = None
    if episode_id is not None or episode_revision is not None:
        if episode_id is None or episode_revision is None:
            raise BadRequestError("关联到集时请同时提供集编号和修订号")
        episode = await db.get(DramaEpisode, episode_id, populate_existing=True)
        if not episode or episode.series_id != series.id:
            raise NotFoundError("剧集不存在")
        check_revision(episode, episode_revision)
    scope = {"character_id": parent.id} if model is DramaLook else {"series_id": series.id}
    # 外键通过下方作品范围校验后才写入。
    row = model(**scope, **{k: v for k, v in values.items() if not k.startswith("novel_")})
    await _require_unique(db, row, values)
    await _link_if_requested(db, series, row, values)
    if model is DramaLook and row.is_default:
        await _clear_default(db, row.character_id)
    db.add(row)
    await db.flush()
    member = row
    if model is DramaCharacter:
        member = DramaLook(character_id=row.id, name="默认造型", is_default=True)
        db.add(member)
        await db.flush()
    if episode:
        await _bind(db, episode.id, member)
        episode.revision += 1
    series.revision += 1
    await db.flush()
    return await describe(db, row)


async def update_asset(db, actor_id, kind, resource_id, values):
    series, row = await authorized(db, actor_id, MODEL[kind], resource_id, write=True)
    check_revision(row, values.pop("revision"))
    await _require_unique(db, row, values)
    if isinstance(row, DramaLook) and "is_default" in values:
        if row.is_default and not values["is_default"]:
            raise BadRequestError("请先将另一套造型设为默认")
        if values["is_default"] and not row.is_default:
            await _clear_default(db, row.character_id)
    for key, value in values.items():
        if not key.startswith("novel_"):
            setattr(row, key, value)
    await _link_if_requested(db, series, row, values)
    row.revision += 1
    series.revision += 1
    await db.flush()
    return await describe(db, row)


async def delete_asset(db, actor_id, kind, resource_id, revision):
    series, row = await authorized(db, actor_id, MODEL[kind], resource_id, write=True)
    check_revision(row, revision)
    if isinstance(row, DramaLook) and row.is_default:
        raise BadRequestError("默认造型不能删除，请先将另一套造型设为默认")
    series.revision += 1
    await discard_owner_images(db, series, row)
    await db.delete(row)
    await db.flush()


async def _bind(db, episode_id, row):
    rel, key = MEMBERSHIP[type(row)]
    pk = {"episode_id": episode_id, key: row.id}
    if await db.get(rel, pk) is not None:
        return False
    db.add(rel(**pk))
    await db.flush()
    return True


async def set_membership(db, actor_id, episode_id, kind, resource_id, revision, *, add):
    series, episode = await authorized(db, actor_id, DramaEpisode, episode_id, write=True)
    check_revision(episode, revision)
    model = MODEL[kind]
    if await _series_id(db, model, resource_id) != series.id:
        raise NotFoundError("本剧素材不存在")
    if add:
        changed = await _bind(db, episode.id, await db.get(model, resource_id))
    else:
        rel, key = MEMBERSHIP[model]
        result = await db.execute(delete(rel).where(rel.episode_id == episode.id, getattr(rel, key) == resource_id))
        changed = result.rowcount > 0
    if changed:
        episode.revision += 1
    await db.flush()


def _look_key(name):
    return "".join(name.split()).casefold()


def _merge_fields(row, item, keys):
    changed = False
    for key in keys:
        if item.get(key) and getattr(row, key) != item[key]:
            setattr(row, key, item[key])
            changed = True
    if changed and row.id is not None:
        row.revision += 1


async def _merge_looks(db, episode, character, items):
    existing = (
        await db.scalars(
            select(DramaLook)
            .where(DramaLook.character_id == character.id)
            .order_by(DramaLook.is_default.desc(), DramaLook.id)
        )
    ).all()
    if not items:
        items = [{"name": existing[0].name if existing else "默认造型"}]
    by_name = {_look_key(look.name): look for look in existing}
    for item in items:
        key = _look_key(item["name"])
        look = by_name.get(key)
        if look is None:
            look = DramaLook(character_id=character.id, name=item["name"], is_default=not by_name)
            by_name[key] = look
            db.add(look)
        _merge_fields(look, item, ("styling",))
        await db.flush()
        await _bind(db, episode.id, look)


def _merge_key(key_fields, values):
    return tuple(linking.name_key(v) if key_fields == ("name",) else v.strip().casefold() for v in values)


async def merge_extracted(db, series, episode, draft):
    """在调用方已完成授权、修订核对的终态事务中合并；任何失败由任务事务整体回滚。"""
    if episode.series_id != series.id:
        raise BadRequestError("剧集与素材库不一致")
    for kind, key_fields, narrative in (
        ("characters", ("name",), ("role", "description", "appearance")),
        ("scenes", ("location", "time_of_day"), ("description", "lighting")),
        ("props", ("name",), ("category", "description")),
    ):
        items = draft.get(kind, [])
        if not items:
            continue
        model = MODEL[kind]
        existing = await db.scalars(select(model).where(model.series_id == series.id).order_by(model.id))
        index = {}
        for row in existing:
            key = _merge_key(key_fields, (getattr(row, field) for field in key_fields))
            index.setdefault(key, row)
        novel_ids = await linking.character_matches(db, series.project_id) if model is DramaCharacter else {}
        for item in items:
            key = _merge_key(key_fields, (item[field] for field in key_fields))
            row = index.get(key)
            is_new = row is None
            if is_new:
                row = model(series_id=series.id, **{field: item[field] for field in key_fields})
                index[key] = row
            previous_revision = row.revision
            _merge_fields(row, item, narrative)
            if model is DramaCharacter and row.novel_character_id is None:
                target = novel_ids.get(linking.name_key(row.name))
                if target is not None:
                    await linking.link_character(db, series.project_id, row, target_id=target)
            if model is DramaScene and row.novel_world_setting_id is None:
                await linking.link_scene(db, series.project_id, row, auto=True)
            if is_new:
                db.add(row)
            elif db.is_modified(row) and row.revision == previous_revision:
                row.revision += 1
            await db.flush()
            if model is DramaCharacter:
                await _merge_looks(db, episode, row, item.get("looks", []))
            else:
                await _bind(db, episode.id, row)
    series.revision += 1
    episode.revision += 1
    await db.flush()
