"""给作品内的人工设定接口附加归属，不扩充作品的可读资料范围。"""

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import SeriesEntryUse
from app.schemas.story import CharacterWithOrigin, SettingOrigin, WorldSettingWithOrigin
from app.services import story_service
from app.services.project_service import get_manageable_project

SettingOut = CharacterWithOrigin | WorldSettingWithOrigin


async def read_book_settings(
    db: AsyncSession,
    kind: story_service.StoryKind,
    user_id: int,
    project_id: int,
    *,
    category: str | None = None,
    item_id: int | None = None,
) -> list[SettingOut]:
    project = await get_manageable_project(db, user_id, project_id)
    if kind is story_service.CHARACTERS:
        binding_column = SeriesEntryUse.character_id
        schema = CharacterWithOrigin
    elif kind is story_service.WORLD:
        binding_column = SeriesEntryUse.world_setting_id
        schema = WorldSettingWithOrigin
    else:
        raise ValueError("不支持该类型的书内设定投影")
    model = kind.model
    statement = (
        select(model, SeriesEntryUse.series_id, SeriesEntryUse.series_entry_id)
        .outerjoin(
            SeriesEntryUse, and_(SeriesEntryUse.project_id == model.project_id, binding_column == model.id),
        )
        .where(model.project_id == project_id)
        .order_by(*kind.order_by)
    )
    if category is not None and kind is story_service.WORLD:
        statement = statement.where(model.category == category)
    if item_id is not None:
        statement = statement.where(model.id == item_id)
    result: list[SettingOut] = []
    for item, series_id, entry_id in await db.execute(statement):
        out = schema.model_validate(item)
        if series_id is not None:
            if project.user_id == user_id:
                out.origin = SettingOrigin(scope="series", series_id=series_id, series_entry_id=entry_id)
            else:
                out.origin = SettingOrigin(scope="series")
        result.append(out)
    if item_id is not None and not result:
        raise NotFoundError(kind.not_found)
    return result
