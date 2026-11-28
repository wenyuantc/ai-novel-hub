"""按账号与剧集保存工作台的模型选择。授权和实际模型调度仍由任务受理层负责。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, NotFoundError
from app.models import AIConfig
from app.plugins.drama.episode_schemas import EpisodeModels
from app.plugins.drama.models import DramaEpisodeModelPreference
from app.plugins.drama.services.episodes import get_episode
from app.services.ai_config_service import lock_config_owner


def _values(row: DramaEpisodeModelPreference | None) -> dict[str, int | None]:
    return {kind: getattr(row, f"{kind}_config_id") if row is not None else None for kind in EpisodeModels.model_fields}


async def _config_types(db: AsyncSession, actor: int, values) -> dict[int, str]:
    ids = {value for value in values if value is not None}
    if not ids:
        return {}
    query = select(AIConfig.id, AIConfig.model_type).where(AIConfig.user_id == actor, AIConfig.id.in_(ids))
    return dict((await db.execute(query)).all())


async def read_models(db: AsyncSession, actor: int, eid: int) -> EpisodeModels:
    await get_episode(db, actor, eid)
    row = await db.get(DramaEpisodeModelPreference, (eid, actor))
    values = _values(row)
    types = await _config_types(db, actor, values.values())
    return EpisodeModels(**{kind: value if types.get(value) == kind else None for kind, value in values.items()})


async def update_models(db: AsyncSession, actor: int, eid: int, patch: EpisodeModels) -> EpisodeModels:
    await get_episode(db, actor, eid, "content.edit")
    # 与模型改类、删除使用相同的账号级配置锁。作品写锁将两个设备的部分更新串行化。
    await lock_config_owner(db, actor, write=False)
    row = await db.get(DramaEpisodeModelPreference, (eid, actor))
    changes = patch.model_dump(exclude_unset=True)
    values = _values(row) | changes
    types = await _config_types(db, actor, values.values())
    for kind, value in changes.items():
        if value is None:
            continue
        if value not in types:
            raise NotFoundError("模型配置不存在")
        if types[value] != kind:
            raise BadRequestError("请选择与用途匹配的模型类型")
    # 配置可能在此前已被改类。下次修改时一并清理对应的失效引用。
    for kind, value in values.items():
        if types.get(value) != kind:
            values[kind] = None
    if any(value is not None for value in values.values()):
        if row is None:
            row = DramaEpisodeModelPreference(episode_id=eid, user_id=actor)
            db.add(row)
        for kind, value in values.items():
            setattr(row, f"{kind}_config_id", value)
    elif row is not None:
        await db.delete(row)
    await db.commit()
    return EpisodeModels(**values)
