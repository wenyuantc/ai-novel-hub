"""应用配置：全部来自环境变量 / .env。

关键密钥（JWT_SECRET、ENCRYPTION_KEY）缺失或非法时实例化即抛错，实现启动 fail-fast，
避免 API Key 以明文或弱密钥落库。
"""

from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://ainovel:ainovel@localhost:5432/ainovel"
    jwt_secret: str = Field(min_length=16)
    encryption_key: str
    registration_enabled: bool = True
    cookie_secure: bool = False
    access_token_expire_minutes: int = Field(default=60 * 24 * 7, gt=0)
    # 逗号分隔，例如 http://localhost:5173,http://127.0.0.1:5173；为空表示不启用 CORS
    cors_origins: str = ""
    # 章节生成上下文的字符预算
    context_char_budget: int = Field(default=12000, ge=1000)
    # 单次 AI 请求超时（秒）
    ai_request_timeout: float = Field(default=300.0, gt=0)
    # 是否允许 AI Base URL 指向本地 / 内网地址（接入 Ollama 等本地模型时开启）；开放注册的公网部署请保持关闭
    allow_private_ai_endpoints: bool = False
    # 生产模式下挂载的前端构建产物目录；不存在则跳过
    frontend_dist_dir: str = str(_DEFAULT_FRONTEND_DIST)

    @field_validator("encryption_key")
    @classmethod
    def _validate_fernet_key(cls, v: str) -> str:
        try:
            Fernet(v.encode())
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                "ENCRYPTION_KEY 不是合法的 Fernet 密钥，请使用 "
                '`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成'
            ) from exc
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
