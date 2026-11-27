"""供纯格式渲染使用的只读文档节点。"""

from dataclasses import dataclass, field
from typing import Literal

from app.schemas.reader import ReaderBlock


@dataclass(frozen=True)
class DocumentNode:
    kind: Literal["heading", "text", "prose"]
    text: str
    level: int = 0
    new_page: bool = False
    blocks: list[ReaderBlock] | None = None
    source: str = "文档"


@dataclass(frozen=True)
class DocumentTable:
    headers: tuple[str, ...]
    rows: list[tuple[str, ...]]
    source: str


@dataclass
class DocumentModel:
    nodes: list[DocumentNode | DocumentTable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
