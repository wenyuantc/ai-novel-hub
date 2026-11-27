"""作品备份规则的数据契约，供核心与插件共同声明。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base
from app.services.project_transfer import remap

Option = Literal["versions", "snapshots", "generation_logs", "inspiration"]
TransferSnapshot = Callable[[AsyncSession, int], Awaitable[object]]
TransferValidator = Callable[[dict[str, list[dict[str, Any]]]], None]


@dataclass(frozen=True)
class TableRule:
    table: str
    model: type[Base]
    # 选出属于项目 pid 的行（导出用）
    scope: Callable[[int], ColumnElement[bool]]
    option: Option | None = None
    fill: dict[str, Literal["project", "user"]] = field(default_factory=dict)
    fk: dict[str, str] = field(default_factory=dict)
    deferred: frozenset[str] = frozenset()
    custom: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    json_refs: dict[str, remap.JsonRemap] = field(default_factory=dict)
    json_plain: tuple[str, ...] = ()
    str_refs: dict[str, Callable[[Any, remap.RemapContext], Any]] = field(default_factory=dict)

    @property
    def has_id(self) -> bool:
        return "id" in self.model.__table__.columns

    @property
    def columns(self) -> list[str]:
        """导出的普通列（含 id、fk、json），不含 fill / custom / exclude。"""
        skip = set(self.fill) | set(self.custom) | set(self.exclude)
        return [c.name for c in self.model.__table__.columns if c.name not in skip]
