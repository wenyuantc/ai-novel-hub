"""世界观 / 角色 / 大纲 CRUD；人工路由显式启用管理权限，AI / 工具默认保持严格归属。"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import Character, Outline, WorldSetting
from app.services.cast_service import is_protagonist_role
from app.services.project_service import get_manageable_project, get_owned_project

WORLD_CATEGORY_LABELS = {
    "place": "地点",
    "faction": "势力",
    "item": "物品",
    "rule": "规则",
    "history": "历史",
    "other": "其他",
}


@dataclass(frozen=True)
class StoryKind:
    model: type
    not_found: str
    order_by: tuple[Any, ...]


WORLD = StoryKind(WorldSetting, "世界观条目不存在", (WorldSetting.sort, WorldSetting.id))
CHARACTERS = StoryKind(Character, "角色不存在", (Character.sort, Character.id))
# 卷内按 sort 排列（大纲页上移 / 下移只改 sort，不改章节号）；sort 相同时按章节号
OUTLINES = StoryKind(Outline, "大纲条目不存在", (Outline.volume_no, Outline.sort, Outline.chapter_no, Outline.id))


async def list_items(db: AsyncSession, kind: StoryKind, project_id: int, *, category: str | None = None) -> list[Any]:
    stmt = select(kind.model).where(kind.model.project_id == project_id)
    if category is not None and kind is WORLD:
        stmt = stmt.where(WorldSetting.category == category)
    rows = await db.scalars(stmt.order_by(*kind.order_by))
    return list(rows)


async def list_owned(
    db: AsyncSession,
    kind: StoryKind,
    user_id: int,
    project_id: int,
    *,
    category: str | None = None,
    manage: bool = False,
) -> list[Any]:
    await (get_manageable_project if manage else get_owned_project)(db, user_id, project_id)
    return await list_items(db, kind, project_id, category=category)


async def next_sort(db: AsyncSession, model: Any, project_id: int) -> int:
    max_sort = await db.scalar(select(func.max(model.sort)).where(model.project_id == project_id))
    return (max_sort or 0) + 1


def apply_character_defaults(values: dict[str, Any]) -> dict[str, Any]:
    """新建角色：未指定「始终注入上下文」时按定位推断（主角默认开启）。"""
    if values.get("always_in_context") is None:
        values["always_in_context"] = is_protagonist_role(values.get("role_type"))
    return values


async def new_character(db: AsyncSession, project_id: int, values: dict[str, Any]) -> Character:
    """新建角色并 flush（不 commit）；供 with-relations、卡片导入等单事务写入复用。"""
    values = apply_character_defaults(dict(values))
    if values.get("sort") is None:
        values["sort"] = await next_sort(db, Character, project_id)
    character = Character(project_id=project_id, **values)
    db.add(character)
    await db.flush()
    return character


async def get_owned(db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, item_id: int) -> Any:
    await get_owned_project(db, user_id, project_id)
    return await _get_item(db, kind, project_id, item_id)


async def get_manageable(db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, item_id: int) -> Any:
    await get_manageable_project(db, user_id, project_id)
    return await _get_item(db, kind, project_id, item_id)


async def _get_item(db: AsyncSession, kind: StoryKind, project_id: int, item_id: int) -> Any:
    item = await db.scalar(select(kind.model).where(kind.model.id == item_id, kind.model.project_id == project_id))
    if item is None:
        raise NotFoundError(kind.not_found)
    return item


async def create_owned(
    db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, data: BaseModel, *, manage: bool = False
) -> Any:
    await _write_project(db, user_id, project_id, manage=manage)
    values = data.model_dump()
    if kind is CHARACTERS:
        apply_character_defaults(values)
    if values.get("sort") is None:
        values["sort"] = await next_sort(db, kind.model, project_id)
    item = kind.model(project_id=project_id, **values)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def update_owned(
    db: AsyncSession,
    kind: StoryKind,
    user_id: int,
    project_id: int,
    item_id: int,
    data: BaseModel,
    *,
    manage: bool = False,
) -> Any:
    await _write_project(db, user_id, project_id, manage=manage)
    item = await (get_manageable if manage else get_owned)(db, kind, user_id, project_id, item_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(item, field, value)
    await db.commit()
    await db.refresh(item)
    return item


async def delete_owned(
    db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, item_id: int, *, manage: bool = False
) -> None:
    await _write_project(db, user_id, project_id, manage=manage)
    item = await (get_manageable if manage else get_owned)(db, kind, user_id, project_id, item_id)
    await db.delete(item)
    await db.commit()


# ---- 供提示词/上下文使用的文本格式化 ----


def _world_entry(w: WorldSetting) -> str:
    return f"■ {w.title}\n{w.content.strip()}"


def format_world(items: list[WorldSetting]) -> str:
    """按分类分组输出（地点 → 势力 → 物品 → 规则 → 历史 → 其他）；只有一个分类时不加分组标题。"""
    groups: dict[str, list[WorldSetting]] = {}
    for w in items:
        cat = w.category if w.category in WORLD_CATEGORY_LABELS else "other"
        groups.setdefault(cat, []).append(w)
    if len(groups) <= 1:
        return "\n\n".join(_world_entry(w) for w in items)
    blocks = []
    for cat, label in WORLD_CATEGORY_LABELS.items():
        if cat in groups:
            blocks.append(f"〔{label}〕\n" + "\n\n".join(_world_entry(w) for w in groups[cat]))
    return "\n\n".join(blocks)


def format_characters(items: list[Character]) -> str:
    """向导参考用的角色摘要（简介 + 结构化描写 + 旧关系备注）。"""
    blocks = []
    for c in items:
        head = f"■ {c.name}" + (f"（{c.role_type}）" if c.role_type else "")
        if c.aliases:
            head += f" 别名：{'、'.join(c.aliases)}"
        lines = [head]
        for label, value in (("性格", c.personality), ("背景", c.background)):
            if value.strip():
                lines.append(f"{label}：{value.strip()}")
        if c.profile.strip():
            lines.append(c.profile.strip())
        if c.relations_text.strip():
            lines.append(f"人物关系：{c.relations_text.strip()}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_roster(items: list[Character]) -> str:
    """只列名字 / 定位 / 别名的简短名单（AI 单体生成时用于避免重名）。"""
    lines = []
    for c in items:
        line = c.name + (f"（{c.role_type}）" if c.role_type else "")
        if c.aliases:
            line += f" 别名：{'、'.join(c.aliases)}"
        lines.append(line)
    return "\n".join(lines)


def format_outlines(items: list[Outline]) -> str:
    lines: list[str] = []
    current_volume: int | None = None
    for o in items:
        if o.volume_no != current_volume:
            current_volume = o.volume_no
            lines.append(f"第{o.volume_no}卷" + (f" {o.volume_title}" if o.volume_title else ""))
        lines.append(f"  第{o.chapter_no}章 {o.title}：{o.summary.strip()}")
    return "\n".join(lines)


async def _write_project(db: AsyncSession, user_id: int, project_id: int, *, manage: bool):
    if manage:
        return await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    return await get_owned_project(db, user_id, project_id, lock=True)
