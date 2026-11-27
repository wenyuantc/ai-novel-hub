"""媒体服务在业务会话中使用的部署配置。"""

import hmac

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings


class MediaSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    media_dir: str
    public_base_url: str
    allow_private_backup_endpoints: bool
    backup_ca_bundle: str | None
    signing_key: bytes = Field(repr=False, exclude=True)

    @classmethod
    def from_deployment(cls, settings: Settings) -> "MediaSettings":
        return cls(
            media_dir=settings.media_dir,
            public_base_url=settings.public_base_url,
            allow_private_backup_endpoints=settings.allow_private_backup_endpoints,
            backup_ca_bundle=settings.backup_ca_bundle,
            signing_key=hmac.digest(settings.jwt_secret.encode(), b"ainovel/media-external/v1", "sha256"),
        )
