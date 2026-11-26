"""部署者管理的本机插件清单。读取及校验清单不会启动任何程序。"""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.config import Settings
from app.core.errors import BadRequestError


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,23}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    version: str = Field(min_length=1, max_length=80)
    command: str
    args: list[str] = Field(default_factory=list, max_length=128)
    cwd: str = "/"
    env: dict[str, str] = Field(default_factory=dict, repr=False, max_length=100)
    env_from: dict[str, str] = Field(default_factory=dict, repr=False, max_length=100)

    def environment(self) -> dict[str, str]:
        try:
            return {**self.env, **{key: os.environ[source] for key, source in self.env_from.items()}}
        except KeyError:
            raise BadRequestError("本机插件所需环境未配置，请联系部署者") from None

    @field_validator("env", "env_from")
    @classmethod
    def environment_names(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key or "=" in key or "\0" in key or "\0" in raw for key, raw in value.items()):
            raise ValueError("环境定义不正确")
        return value

    @field_validator("args")
    @classmethod
    def literal_arguments(cls, value: list[str]) -> list[str]:
        if any("\0" in item for item in value):
            raise ValueError("固定参数不正确")
        return value

    @field_validator("command", "cwd")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute() or "\0" in value:
            raise ValueError("需要绝对路径")
        return value

    @property
    def available(self) -> bool:
        return (
            Path(self.command).is_file()
            and os.access(self.command, os.X_OK)
            and Path(self.cwd).is_dir()
            and all(source in os.environ for source in self.env_from.values())
        )


class CatalogFile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    plugins: list[CatalogEntry] = Field(max_length=200)


def read(settings: Settings) -> dict[str, CatalogEntry]:
    if not settings.mcp_stdio_catalog:
        return {}
    try:
        path = Path(settings.mcp_stdio_catalog)
        if not path.is_file():
            raise ValueError()
        with path.open("rb") as source:
            data = source.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError()
        entries = CatalogFile.model_validate_json(data).plugins
        catalog = {entry.id: entry for entry in entries}
        if len(catalog) != len(entries):
            raise ValueError()
        return catalog
    except (ValidationError, ValueError, OSError):
        raise BadRequestError("本机插件清单不可用，请联系部署者检查配置") from None


def resolve(settings: Settings, catalog_id: str) -> CatalogEntry:
    entry = read(settings).get(catalog_id)
    if entry is None or not entry.available:
        raise BadRequestError("本机插件未登记或当前节点未正确安装，请联系部署者")
    return entry
