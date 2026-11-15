"""世界观 / 角色 / 大纲 CRUD；人工路由显式启用管理权限，AI / 工具默认保持严格归属。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

import anyio
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import Character, Outline, WorldSetting
from app.schemas.setting_fields import EntityRef, EntryType
from app.services import (
    planning_lifecycle,
    project_access,
    series_policy,
    setting_ai_visibility,
    setting_catalog_service,
    setting_field_service,
    setting_progression_service,
)
from app.services.cast_service import is_protagonist_role
from app.services.project_service import get_manageable_project, get_owned_project

if TYPE_CHECKING:
    from app.services.mentions.context import MentionCandidate

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
    if manage:
        await project_access.require_project(db, user_id, project_id, "content.read")
    else:
        await get_owned_project(db, user_id, project_id)
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
    await setting_catalog_service.ensure_entry_timeline(db, project_id, "character", character.id)
    return character


async def new_world(db: AsyncSession, project_id: int, values: dict[str, Any]) -> WorldSetting:
    """新建世界观及其原始正文基线；组合事务统一复用，只 flush。"""
    values = dict(values)
    if values.get("sort") is None:
        values["sort"] = await next_sort(db, WorldSetting, project_id)
    world = WorldSetting(project_id=project_id, **values)
    db.add(world)
    await db.flush()
    await setting_catalog_service.ensure_entry_timeline(db, project_id, "world", world.id)
    return world


async def _candidate_snapshot(db: AsyncSession, project_id: int) -> list["MentionCandidate"]:
    from app.services.mentions.context import load_candidates

    return await load_candidates(db, project_id)


@asynccontextmanager
async def _candidate_write_rollback(db: AsyncSession, *, enabled: bool = True) -> AsyncIterator[None]:
    """授权完成后的候选写失败不能由内部调用方捕获后继续提交半笔变更。"""
    try:
        yield
    except BaseException:
        if enabled:
            with anyio.CancelScope(shield=True):
                await db.rollback()
        raise


async def _bump_new_candidate_competition(
    db: AsyncSession, project_id: int, before: list["MentionCandidate"],
) -> None:
    # 局部导入避免 card_service → 组织/故事服务的循环依赖。
    from app.services.card_service import _candidate_competition

    if _candidate_competition(before, await _candidate_snapshot(db, project_id)):
        await setting_ai_visibility.bump_visibility_revision(db, project_id, tighten=True)


def _identity_change_tightens(
    before: list["MentionCandidate"], kind: str, item_id: int, *,
    name: str | None = None, aliases: list[str] | None = None,
) -> bool:
    """旧词消失或新增词遮蔽其他候选，旧唯一命中即可能不再有资格。"""
    from app.services.card_service import _candidate_competition

    target = next((entry for entry in before if entry.key == (kind, item_id)), None)
    if target is None:
        return True  # 无法证明旧身份时保守失效。
    if not target.tracking.enabled:
        return False
    updated = target.ref.model_copy(update={
        "name": target.ref.name if name is None else name,
        "aliases": target.ref.aliases if aliases is None else aliases,
    })

    def words(ref) -> dict[str, str]:
        terms = (ref.name, *ref.aliases)
        return {
            term.strip() if target.tracking.case_sensitive else term.strip().casefold(): term.strip()
            for term in terms if term.strip()
        }

    old, new = words(target.ref), words(updated)
    if old.keys() - new.keys():
        return True
    added = [new[key] for key in new.keys() - old.keys()]
    if not added:
        return False
    added_ref = updated.model_copy(update={"name": added[0], "aliases": added[1:]})
    other_candidates = [entry for entry in before if entry.key != target.key]
    return _candidate_competition(other_candidates, [replace(target, ref=added_ref)])


async def _ensure_removed_candidate_bump(db: AsyncSession, project_id: int, before_revision: int) -> None:
    # 清理 AI 链接可能已经递增；删除本身也可能移除旧显式/唯一命中的资格。
    if await setting_ai_visibility.visibility_revision(db, project_id) == before_revision:
        await setting_ai_visibility.bump_visibility_revision(db, project_id, tighten=True)


async def get_owned(db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, item_id: int) -> Any:
    await get_owned_project(db, user_id, project_id)
    return await _get_item(db, kind, project_id, item_id)


async def get_manageable(db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, item_id: int) -> Any:
    await get_manageable_project(db, user_id, project_id)
    return await _get_item(db, kind, project_id, item_id)


async def get_collaborative(db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, item_id: int) -> Any:
    """人工资料入口：明确支持作品协作者，不改变已有工具的严格读取边界。"""
    await project_access.require_project(db, user_id, project_id, "content.read")
    return await _get_item(db, kind, project_id, item_id)


async def _get_item(db: AsyncSession, kind: StoryKind, project_id: int, item_id: int) -> Any:
    item = await db.scalar(select(kind.model).where(kind.model.id == item_id, kind.model.project_id == project_id))
    if item is None:
        raise NotFoundError(kind.not_found)
    return item


async def create_owned(
    db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, data: BaseModel, *, manage: bool = False
) -> Any:
    with db.no_autoflush:
        if kind in (CHARACTERS, WORLD):
            if manage:
                await project_access.require_project(
                    db, user_id, project_id, "content.edit", for_update=True, visibility_write=True,
                )
            else:
                await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id, manage=False)
        else:
            await _write_project(db, user_id, project_id, manage=manage)
    async with _candidate_write_rollback(db, enabled=kind in (CHARACTERS, WORLD)):
        candidates = await _candidate_snapshot(db, project_id) if kind in (CHARACTERS, WORLD) else None
        values = data.model_dump()
        if kind is WORLD:
            item = await new_world(db, project_id, values)
        elif kind is CHARACTERS:
            item = await new_character(db, project_id, values)
        else:
            if values.get("sort") is None:
                values["sort"] = await next_sort(db, kind.model, project_id)
            item = kind.model(project_id=project_id, **values)
            db.add(item)
        if candidates is not None:
            await _bump_new_candidate_competition(db, project_id, candidates)
        if kind is OUTLINES:
            await planning_lifecycle.bump_revision(db, project_id)
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
    values = data.model_dump(exclude_unset=True)
    check_legacy_pin = kind is CHARACTERS and any(
        values.get(key) is not None for key in ("always_in_context", "role_type")
    )
    check_legacy_tracking = kind is WORLD and values.get("category") is not None
    name_field = "name" if kind is CHARACTERS else "title"
    check_identity = kind in (CHARACTERS, WORLD) and any(
        values.get(key) is not None for key in (name_field, "aliases")
    )
    with db.no_autoflush:
        if check_legacy_pin or check_legacy_tracking or check_identity:
            if manage:
                await project_access.require_project(
                    db, user_id, project_id, "content.edit", for_update=True, visibility_write=True,
                )
            else:
                await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id, manage=False)
        else:
            await _write_project(db, user_id, project_id, manage=manage)
    async with _candidate_write_rollback(db, enabled=kind in (CHARACTERS, WORLD)):
        item = await _get_item(db, kind, project_id, item_id)
        identity_tightened = False
        if check_identity:
            identity_tightened = _identity_change_tightens(
                await _candidate_snapshot(db, project_id), "character" if kind is CHARACTERS else "world", item_id,
                name=values.get(name_field), aliases=values.get("aliases"),
            )
        if kind in (CHARACTERS, WORLD):
            projection_kind = "character" if kind is CHARACTERS else "world"
            shared_fields = (
                series_policy.CHARACTER_SHARED_FIELDS if kind is CHARACTERS else series_policy.WORLD_SHARED_FIELDS
            )
            static_fields = shared_fields - series_policy.shared_baseline_fields(projection_kind)
            if any(values.get(key) is not None and values[key] != getattr(item, key) for key in static_fields):
                await series_policy.reject_bound_entry(
                    db, project_id, projection_kind, item_id, actor_user_id=user_id if manage else None,
                )
        legacy_pin = check_legacy_pin and item.ai_mode is None
        before_pinned = legacy_pin and (item.always_in_context or is_protagonist_role(item.role_type))
        old_category = item.category if check_legacy_tracking else None
        if kind is WORLD:
            category = values.get("category")
            if category is not None and category != item.category:
                await setting_field_service.assert_entry_type_change_allowed(
                    db, project_id, "world", item.id, cast(EntryType, f"world.{category}")
                )
            await setting_progression_service.apply_world_content_update(
                db,
                item,
                content=values.pop("content", None),
                edit_mode=values.pop("edit_mode", None),
                expected_revision=values.pop("expected_revision", None),
                metadata_changed=any(
                    value is not None and value != getattr(item, key) for key, value in values.items()
                ),
                actor_user_id=user_id if manage else None,
            )
        elif kind is CHARACTERS:
            await setting_progression_service.apply_character_fields_update(
                db,
                item,
                values,
                edit_mode=values.pop("edit_mode", None),
                expected_revision=values.pop("expected_revision", None),
                actor_user_id=user_id if manage else None,
            )
        for field, value in values.items():
            if value is not None:
                setattr(item, field, value)
        visibility_changed = identity_tightened
        tightened = identity_tightened
        if legacy_pin:
            after_pinned = item.always_in_context or is_protagonist_role(item.role_type)
            if before_pinned != after_pinned:
                visibility_changed = True
                tightened = tightened or before_pinned
        if check_legacy_tracking and old_category != item.category:
            before_tracking = setting_ai_visibility.effective_mention_enabled(
                "world", item.ai_mode, item.mention_enabled, category=old_category,
            )
            after_tracking = setting_ai_visibility.effective_mention_enabled(
                "world", item.ai_mode, item.mention_enabled, category=item.category,
            )
            if before_tracking != after_tracking:
                # Legacy 世界观进入词典也可能使已有字面命中变为歧义。
                visibility_changed = tightened = True
        if visibility_changed:
            await setting_ai_visibility.bump_visibility_revision(db, project_id, tighten=tightened)
        if kind is OUTLINES:
            await planning_lifecycle.bump_revision(db, project_id)
        await db.commit()
        await db.refresh(item)
        return item


async def delete_owned(
    db: AsyncSession, kind: StoryKind, user_id: int, project_id: int, item_id: int, *, manage: bool = False
) -> None:
    with db.no_autoflush:
        if kind is CHARACTERS or kind is WORLD:
            if manage:
                await project_access.require_project(
                    db, user_id, project_id, "content.edit", for_update=True, visibility_write=True,
                )
            else:
                await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id, manage=False)
        else:
            await _write_project(db, user_id, project_id, manage=manage)
    async with _candidate_write_rollback(db, enabled=kind in (CHARACTERS, WORLD)):
        item = await _get_item(db, kind, project_id, item_id)
        if kind is CHARACTERS or kind is WORLD:
            before_revision = await setting_ai_visibility.visibility_revision(db, project_id)
            await series_policy.reject_bound_entry(
                db, project_id, "character" if kind is CHARACTERS else "world", item_id,
                actor_user_id=user_id if manage else None,
            )
            await setting_field_service.cleanup_unreferenced_handles(
                db, project_id, [EntityRef(kind="character" if kind is CHARACTERS else "world", id=item.id)]
            )
            await _ensure_removed_candidate_bump(db, project_id, before_revision)
        if kind is OUTLINES:
            await planning_lifecycle.detach_outlines(db, project_id, [item.id])
        elif kind is CHARACTERS:
            await planning_lifecycle.remove_character_references(db, project_id, [item.id])
        await db.delete(item)
        await planning_lifecycle.bump_revision(db, project_id)
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
        return await project_access.require_project(db, user_id, project_id, "content.edit", for_update=True)
    return await get_owned_project(db, user_id, project_id, lock=True)
