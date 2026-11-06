"""仅供模型工具使用的设定身份、子记录与字段投影。"""

from collections.abc import Iterable

from sqlalchemy import select

from app.core.errors import NotFoundError
from app.models import Career, Character, Organization, SettingTimeline, WorldSetting
from app.schemas.setting_ai_visibility import HIDEABLE_BUILTIN_FIELDS

from .base import ToolCtx, serialized

ENTRY_KINDS = {Character: "character", WorldSetting: "world", Organization: "organization", Career: "career"}
SUBJECT_REFERENCES = {
    "character": (("character", "character_id"),),
    "world": (("world", "world_setting_id"),),
    "relationship": (("character", "character_id"), ("character", "related_character_id")),
    "career": (("character", "character_id"), ("career", "career_id")),
    "membership": (("character", "character_id"), ("organization", "organization_id")),
    "organization": (("organization", "organization_id"),),
    "career_entry": (("career", "career_id"),),
}


def allowed(ctx: ToolCtx, kind: str, ident: int | None) -> bool:
    return ctx.scope is None or (type(ident) is int and ctx.scope.includes(kind, ident))


def allowed_field(ctx: ToolCtx, kind: str, ident: int | None, key: str) -> bool:
    return ctx.scope is None or (type(ident) is int and ctx.scope.field_visible(kind, ident, key))


def field_write_allowed(ctx: ToolCtx, kind: str, ident: int, keys: Iterable[str]) -> None:
    if any(
        not allowed_field(ctx, kind, ident, key)
        for key in set(keys).intersection(HIDEABLE_BUILTIN_FIELDS[kind])
    ):
        raise NotFoundError("不存在")


def timeline_allowed(ctx: ToolCtx, item) -> bool:
    return ctx.scope is None or all(
        allowed(ctx, kind, getattr(item, attr)) for kind, attr in SUBJECT_REFERENCES[item.subject_kind]
    )


async def timeline_fields_allowed(ctx: ToolCtx, timeline_id: int, fields: Iterable[str]) -> None:
    if ctx.scope is None:
        return
    row = await ctx.db.scalar(select(SettingTimeline).where(
        SettingTimeline.id == timeline_id, SettingTimeline.project_id == ctx.project_id,
    ))
    if row is None or not timeline_allowed(ctx, row):
        raise NotFoundError("不存在")
    keys = set(fields)
    refs = SUBJECT_REFERENCES[row.subject_kind]
    if len(refs) == 1:
        kind, attr = refs[0]
        field_write_allowed(ctx, kind, getattr(row, attr), keys)
    elif row.subject_kind == "career" and keys.intersection({"stage_id", "presence"}):
        if not allowed_field(ctx, "career", row.career_id, "stages"):
            raise NotFoundError("不存在")


def entry_payload(ctx: ToolCtx, kind: str, item) -> dict:
    data = serialized(item)
    if ctx.scope is not None:
        data = ctx.scope.filter_fields(kind, data["id"], data)
        # 聚合数和父组织引用都可能旁路披露未入选的条目。
        data.pop("member_count", None)
        data.pop("holder_count", None)
        if kind == "organization" and not allowed(ctx, "organization", data.get("parent_id")):
            data.pop("parent_id", None)
    return data


def assignment_payload(ctx: ToolCtx, item) -> dict:
    data = serialized(item)
    if not allowed_field(ctx, "career", item.career_id, "stages"):
        for key in ("stage_id", "stage_name", "stage_position", "stage_total"):
            data.pop(key, None)
    return data


def analysis_payload(ctx: ToolCtx, item) -> dict:
    data = serialized(item)
    if ctx.scope is None:
        return data
    # 原始变化及警告以旧名字而不是稳定 ID 为锚，不能可靠逆投影；只发送通过下述
    # ID/字段校验的正式变化。简介、原文建议属于独立的非结构化结果。
    data.pop("changes_raw", None)
    data.pop("warnings", None)
    required = {
        "character_state": (("character", "character_id"),),
        "life_status": (("character", "character_id"),),
        "relationship_create": (("character", "a_id"), ("character", "b_id")),
        "relationship_update": (("character", "a_id"), ("character", "b_id")),
        "career_stage": (("character", "character_id"), ("career", "career_id")),
        "org_membership": (("character", "character_id"), ("organization", "organization_id")),
    }

    def permitted(change: dict) -> bool:
        kind = change["kind"]
        target = change["target"]
        if not all(allowed(ctx, entry_kind, target.get(key)) for entry_kind, key in required.get(kind, ())):
            return False
        if kind in ("character_state", "life_status"):
            return allowed_field(
                ctx, "character", target["character_id"], "current_state" if kind == "character_state" else kind,
            )
        if kind == "career_stage":
            return allowed_field(ctx, "career", target["career_id"], "stages")
        return kind in required or kind.startswith("foreshadow_")

    visible = []
    for change in data["changes"]:
        if not permitted(change):
            continue
        # 失败说明是业务排查文案，可能引用完整资料；状态足以供模型判断。
        change.pop("reason", None)
        if change["kind"] == "foreshadow_new":
            # 人工确认会写入整个伏笔快照，关联角色不只可能出现在 target。
            for snapshot in (change["before"], change["after"]):
                if isinstance(snapshot, dict) and "character_ids" in snapshot:
                    ids = snapshot.pop("character_ids")
                    if isinstance(ids, list):
                        snapshot["character_ids"] = [ident for ident in ids if allowed(ctx, "character", ident)]
        visible.append(change)
    data["changes"] = visible
    return data
