"""在调用者的事务内读取六项运行设置，共用同一份冻结部署基线。"""

import time
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.models import SystemSettings
from app.schemas.system_settings import SYSTEM_SETTING_KEYS, SystemSettingsChanges
from app.services import authorization_service as authz


class SettingsUnavailable(AppError):
    status_code = 503

    def __init__(self):
        super().__init__("系统设置暂不可用，请稍后重试")


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    registration_enabled: bool
    task_max_concurrency: int
    task_max_per_user: int
    auto_analyze_enabled: bool
    mcp_enabled: bool
    open_api_rate_limit_per_minute: int


@dataclass(slots=True)
class SettingsSource:
    deployment: EffectiveSettings
    verified_until: float = 0.0


def configure_deployment(maker: async_sessionmaker[AsyncSession], settings: Settings) -> SettingsSource:
    baseline = EffectiveSettings(**{key: getattr(settings, key) for key in SYSTEM_SETTING_KEYS})
    info = maker.kw.setdefault("info", {})
    source = info.setdefault("system_settings_source", SettingsSource(baseline))
    if source.deployment != baseline:
        raise ValueError("系统设置部署基线已固定")
    return source


def validated_row(row: SystemSettings | None) -> tuple[int, dict, datetime | None]:
    if row is None:
        return 0, {}, None
    if type(row.revision) is not int or row.revision < 1 or type(row.overrides) is not dict:
        raise ValueError("持久设置无效")
    changes = SystemSettingsChanges.model_validate(row.overrides).model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise ValueError("持久设置无效")
    return row.revision, changes, row.updated_at


def effective_settings_from_row(db: AsyncSession, row: SystemSettings | None) -> EffectiveSettings:
    source = db.info.get("system_settings_source")
    if not isinstance(source, SettingsSource) or not authz.runtime_for(db).settings_confirmed:
        raise SettingsUnavailable()
    try:
        _, changes, _ = validated_row(row)
        return replace(source.deployment, **changes) if changes else source.deployment
    except ValueError:
        raise SettingsUnavailable() from None


async def effective_settings(db: AsyncSession) -> EffectiveSettings:
    """调用者负责其判定所需的物理门禁，这里只执行 SELECT。"""
    try:
        with db.no_autoflush:
            row = await db.get(SystemSettings, 1, populate_existing=True)
        return effective_settings_from_row(db, row)
    except Exception:
        raise SettingsUnavailable() from None


async def settings_for_consumer(db: AsyncSession, published: Settings) -> Settings | EffectiveSettings:
    """普通设置在一个周期内可使用本进程已发布的值，过期时直接在当前事务读取。"""
    source = db.info.get("system_settings_source")
    if (isinstance(source, SettingsSource) and authz.runtime_for(db).settings_confirmed
            and time.monotonic() < source.verified_until):
        return published
    return await effective_settings(db)
