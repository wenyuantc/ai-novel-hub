"""短剧资产与同一作品设定的单向关联。明确姓名优先于别名，歧义留给作者选择。"""

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import Character, WorldSetting
from app.plugins.drama.models import DramaCharacter, DramaScene


def _unique_match(matches):
    return matches[0] if len(matches) == 1 else None


def name_key(name: str) -> str:
    """匹配提取器给出的名字时允许括号内跟随身份说明。"""
    return name.replace("（", "(").partition("(")[0].strip().casefold() or name.strip().casefold()


async def character_matches(db, project_id):
    names = defaultdict(set)
    aliases = defaultdict(set)
    query = select(Character.id, Character.name, Character.aliases).where(Character.project_id == project_id)
    for identifier, name, other_names in await db.execute(query):
        names[name_key(name)].add(identifier)
        for alias in other_names:
            aliases[name_key(alias)].add(identifier)
    result = {}
    for key in names.keys() | aliases.keys():
        candidates = names.get(key) or aliases[key]
        if len(candidates) == 1:
            result[key] = next(iter(candidates))
    return result


async def link_character(
    db: AsyncSession, project_id: int, asset: DramaCharacter, *, target_id: int | None = None
) -> None:
    if target_id is not None:
        target = await db.scalar(select(Character).where(Character.id == target_id, Character.project_id == project_id))
        if target is None:
            raise NotFoundError("小说角色不存在")
    else:
        target = None
    asset.novel_character_id = target.id if target else None
    if target:
        asset.appearance = asset.appearance or target.appearance
        asset.description = asset.description or target.profile
        asset.role = asset.role or target.role_type


async def link_scene(
    db: AsyncSession, project_id: int, asset: DramaScene, *, target_id: int | None = None, auto: bool = False
) -> None:
    query = select(WorldSetting).where(WorldSetting.project_id == project_id, WorldSetting.category == "place")
    if auto:
        target = _unique_match((await db.scalars(query.where(WorldSetting.title == asset.location).limit(2))).all())
    elif target_id is not None:
        target = await db.scalar(query.where(WorldSetting.id == target_id))
        if target is None:
            raise NotFoundError("小说地点不存在")
    else:
        target = None
    asset.novel_world_setting_id = target.id if target else None
    if target:
        asset.description = asset.description or target.content
