"""只接受平台 X-API-Key 的开放读取依赖，与网页登录认证分离。"""

from typing import Annotated

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader

from app.core.deps import DbDep, SettingsDep
from app.services.api_key_service import ApiKeyPrincipal, authenticate_key

key_header = APIKeyHeader(
    name="X-API-Key",
    scheme_name="PlatformAPIKey",
    description="在 API Key 管理页生成的平台访问密钥，只能读取已授权小说。",
    auto_error=False,
)


async def get_api_key_principal(
    db: DbDep, settings: SettingsDep, key: Annotated[str | None, Security(key_header)]
) -> ApiKeyPrincipal:
    return await authenticate_key(db, key, settings.open_api_rate_limit_per_minute)


OpenApiUser = Annotated[ApiKeyPrincipal, Depends(get_api_key_principal)]
