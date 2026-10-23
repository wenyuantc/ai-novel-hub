"""世界观 / 角色 / 大纲 的通用 CRUD。所有入口先校验项目归属（user_id）。"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import Character, Outline, WorldSetting
from app.services.project_service import get_owned_project


@dataclass(frozen=True)
class StoryKind:
    model: type
    not_found: str
    order_by: tuple[Any, ...]


WORLD = StoryKind(WorldSetting, "世界观条目不存在", (WorldSetting.sort, WorldSetting.id))
CHARACTERS = StoryKind(Character, "角色不存在", (Character.sort, Character.id))
OUTLINES = StoryKind(Outline, "大纲条目不存在", (Outline.volume_no, Outline.chapter_no, Outline.sort, Outline.id))


async def list_items(db: AsyncSession, kind: StoryKind, project_id: int) -> list[Any]:
    rows = await db.scalars(select(kind.model).where(kind.model.project_id == project_id).order_by(*kind.order_by))
    return list(rows)


async def list_owned(db: AsyncSession, kind: StoryKind, user_id: int, project_id: int) -> list[Any]:
    await get_owned_project(db, user_id, project_id)
    return await list_items(db, kind, project_id)


async def get_owned(db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, item_id: int) -> Any:
    await get_owned_project(db, user_id, project_id)
    item = await db.scalar(select(kind.model).where(kind.model.id == item_id, kind.model.project_id == project_id))
    if item is None:
        raise NotFoundError(kind.not_found)
    return item


async def create_owned(db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, data: BaseModel) -> Any:
    await get_owned_project(db, user_id, project_id)
    values = data.model_dump()
    if values.get("sort") is None:
        max_sort = await db.scalar(select(func.max(kind.model.sort)).where(kind.model.project_id == project_id))
        values["sort"] = (max_sort or 0) + 1
    item = kind.model(project_id=project_id, **values)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def update_owned(
    db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, item_id: int, data: BaseModel
) -> Any:
    item = await get_owned(db, kind, user_id, project_id, item_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(item, field, value)
    await db.commit()
    await db.refresh(item)
    return item


async def delete_owned(db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, item_id: int) -> None:
    item = await get_owned(db, kind, user_id, project_id, item_id)
    await db.delete(item)
    await db.commit()


# ---- 供提示词/上下文使用的文本格式化 ----


def format_world(items: list[WorldSetting]) -> str:
    return "\n\n".join(f"■ {w.title}\n{w.content.strip()}" for w in items)


def format_characters(items: list[Character]) -> str:
    blocks = []
    for c in items:
        head = f"■ {c.name}" + (f"（{c.role_type}）" if c.role_type else "")
        lines = [head]
        if c.profile.strip():
            lines.append(c.profile.strip())
        if c.relations_text.strip():
            lines.append(f"人物关系：{c.relations_text.strip()}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_outlines(items: list[Outline]) -> str:
    lines: list[str] = []
    current_volume: int | None = None
    for o in items:
        if o.volume_no != current_volume:
            current_volume = o.volume_no
            lines.append(f"第{o.volume_no}卷" + (f" {o.volume_title}" if o.volume_title else ""))
        lines.append(f"  第{o.chapter_no}章 {o.title}：{o.summary.strip()}")
    return "\n".join(lines)
