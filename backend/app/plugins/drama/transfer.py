"""作品备份中的短剧数据及剧内引用。停用插件仍保留作品资料。"""

from collections import Counter
from typing import Any

from sqlalchemy import UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.plugins.drama import models as m
from app.plugins.drama.styles import STYLES
from app.services.project_transfer.rules import TableRule


def _series(pid):
    return select(m.DramaSeries.id).where(m.DramaSeries.project_id == pid)


async def backup_notice(db: AsyncSession, project_id: int) -> str | None:
    history = select(m.DramaAssetImage.id).where(
        m.DramaAssetImage.owner_type == "look", m.DramaAssetImage.series_id.in_(_series(project_id))
    )
    if await db.scalar(select(history.exists())):
        return "短剧造型和当前图片提示词随备份保存。造型图片文件及图片历史（含选定记录）留在原实例，导入后可重新生图。"
    return None


def _episodes(pid):
    return select(m.DramaEpisode.id).where(m.DramaEpisode.series_id.in_(_series(pid)))


def _characters(pid):
    return select(m.DramaCharacter.id).where(m.DramaCharacter.series_id.in_(_series(pid)))


def _shots(pid):
    return select(m.DramaShot.id).where(m.DramaShot.episode_id.in_(_episodes(pid)))


def _child(model, owner, parents, **other_refs):
    column = getattr(model, owner)
    (ref,) = column.foreign_keys
    return TableRule(
        model.__tablename__,
        model,
        lambda pid: column.in_(parents(pid)),
        fk={owner: ref.column.table.name, **other_refs},
    )


TRANSFER_RULES = (
    TableRule(
        "drama_series", m.DramaSeries, lambda pid: m.DramaSeries.project_id == pid, fill={"project_id": "project"}
    ),
    _child(m.DramaEpisode, "series_id", _series),
    _child(m.DramaEpisodeSource, "episode_id", _episodes, chapter_id="chapters"),
    _child(m.DramaCharacter, "series_id", _series, novel_character_id="characters"),
    _child(m.DramaLook, "character_id", _characters),
    _child(m.DramaScene, "series_id", _series, novel_world_setting_id="world_settings"),
    _child(m.DramaProp, "series_id", _series),
    _child(m.DramaShot, "episode_id", _episodes, scene_id="drama_scenes"),
    _child(m.DramaEpisodeLook, "episode_id", _episodes, look_id="drama_looks"),
    _child(m.DramaEpisodeScene, "episode_id", _episodes, scene_id="drama_scenes"),
    _child(m.DramaEpisodeProp, "episode_id", _episodes, prop_id="drama_props"),
    _child(m.DramaShotLook, "shot_id", _shots, look_id="drama_looks"),
    _child(m.DramaShotProp, "shot_id", _shots, prop_id="drama_props"),
)


async def backup_state(db: AsyncSession, pid: int) -> tuple:
    # 剧级 revision 覆盖素材库，集级 revision 覆盖剧本、分镜与关联。
    series = tuple(
        await db.execute(
            select(m.DramaSeries.id, m.DramaSeries.revision)
            .where(m.DramaSeries.project_id == pid)
            .order_by(m.DramaSeries.id)
        )
    )
    if not series:
        return ()
    episodes = tuple(
        await db.execute(
            select(m.DramaEpisode.id, m.DramaEpisode.revision)
            .where(m.DramaEpisode.series_id.in_(_series(pid)))
            .order_by(m.DramaEpisode.id)
        )
    )
    # 小说设定被删除时由外键 SET NULL 解除绑定，不会递增短剧修订号。
    links = []
    for model, field in (
        (m.DramaCharacter, m.DramaCharacter.novel_character_id),
        (m.DramaScene, m.DramaScene.novel_world_setting_id),
    ):
        links.append(
            tuple(
                await db.execute(
                    select(model.id, field)
                    .where(model.series_id.in_(_series(pid)), field.is_not(None))
                    .order_by(model.id)
                )
            )
        )
    return series, episodes, *links


def _check_columns(tables):
    for rule in TRANSFER_RULES:
        model = rule.model.__table__
        rows = tables[rule.table]
        for constraint in model.constraints:
            if isinstance(constraint, UniqueConstraint):
                names = constraint.columns.keys()
                defaults = {k: model.c[k].default.arg if model.c[k].default is not None else None for k in names}
                seen = set()
                for row in rows:
                    key = tuple(row.get(k, defaults[k]) for k in names)
                    if None in key:  # SET NULL 后多个已删章节保留各自来源标题与顺序。
                        continue
                    if key in seen:
                        raise BadRequestError(f"短剧表 {rule.table} 的唯一字段重复")
                    seen.add(key)
        if "revision" in model.c and any(r.get("revision", 0) < 0 for r in rows):
            raise BadRequestError("短剧修订号不能为负")
    for series in tables["drama_series"]:
        if series.get("style", "3d") not in STYLES or series.get("aspect_ratio", "16:9") not in {"16:9", "9:16", "1:1"}:
            raise BadRequestError("短剧风格或画幅不合法")
    for episode in tables["drama_episodes"]:
        if (
            episode["number"] < 1
            or episode.get("status", "draft") not in {"draft", "active", "completed"}
            or episode.get("resolution", "720p") not in {"480p", "720p", "1080p"}
        ):
            raise BadRequestError("剧集编号、状态或分辨率不合法")
    if any(not 2 <= row.get("duration_seconds", 5) <= 30 for row in tables["drama_shots"]):
        raise BadRequestError("分镜时长必须在 2 到 30 秒之间")


def validate_backup(tables: dict[str, list[dict[str, Any]]]) -> None:
    for rule in TRANSFER_RULES:
        if rule.table not in tables:
            raise BadRequestError(f"备份缺少短剧表 {rule.table}")
    _check_columns(tables)
    episodes = {r["id"]: r["series_id"] for r in tables["drama_episodes"]}
    characters = {r["id"]: r["series_id"] for r in tables["drama_characters"]}
    looks = {r["id"]: characters[r["character_id"]] for r in tables["drama_looks"]}
    scenes = {r["id"]: r["series_id"] for r in tables["drama_scenes"]}
    props = {r["id"]: r["series_id"] for r in tables["drama_props"]}
    shots = {r["id"]: episodes[r["episode_id"]] for r in tables["drama_shots"]}
    defaults = Counter(r["character_id"] for r in tables["drama_looks"] if r.get("is_default"))
    if any(defaults[c] != 1 for c in characters):
        raise BadRequestError("短剧角色必须恰有一个默认造型")
    if (
        len(tables["drama_series"]) > 20
        or any(n > 200 for n in Counter(episodes.values()).values())
        or any(n > 20 for n in Counter(r["episode_id"] for r in tables["drama_episode_sources"]).values())
        or any(n > 200 for n in Counter(r["episode_id"] for r in tables["drama_shots"]).values())
        or any(len(r.get("source_text", "")) > 60_000 for r in tables["drama_episodes"])
    ):
        raise BadRequestError("短剧、剧集、来源章节、原文或分镜超过上限")
    for row in tables["drama_shots"]:
        if row.get("scene_id") is not None and scenes[row["scene_id"]] != episodes[row["episode_id"]]:
            raise BadRequestError("分镜引用了其他剧的场景")
    for table, owner_key, owners, asset_key, assets in (
        ("drama_episode_looks", "episode_id", episodes, "look_id", looks),
        ("drama_episode_scenes", "episode_id", episodes, "scene_id", scenes),
        ("drama_episode_props", "episode_id", episodes, "prop_id", props),
        ("drama_shot_looks", "shot_id", shots, "look_id", looks),
        ("drama_shot_props", "shot_id", shots, "prop_id", props),
    ):
        seen = set()
        for row in tables[table]:
            key = row[owner_key], row[asset_key]
            if key in seen or owners[key[0]] != assets[key[1]]:
                raise BadRequestError("短剧素材关联重复或跨剧引用")
            seen.add(key)
