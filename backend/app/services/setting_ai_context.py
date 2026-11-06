"""AI 专用设定选择与投影。人工档案、分类和导出不经过此边界。"""

from collections.abc import Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models import Character, WorldSetting
from app.schemas.setting_fields import EntryFieldsOut, FieldSlot, SettingEntry
from app.services.ai_dispatch import VisibilityStamp, read_visibility_stamp, require_visibility_stamp
from app.services.mentions.context import MentionCandidate, candidate_dictionary, load_candidates
from app.services.mentions.matcher import EntryKey, iter_matches, match_entities
from app.services.setting_ai_visibility import load_link_targets

_SENDABLE_MODES = frozenset({None, "always", "detected", "manual_only"})


def _detected(candidates: list[MentionCandidate], texts: tuple[str, ...]) -> set[EntryKey]:
    if not texts:
        return set()
    dictionary = candidate_dictionary(candidates)
    hits = set()
    for text in texts:
        for match in iter_matches(text, dictionary):
            owners = match_entities(match, dictionary)
            if len(owners) == 1:
                hits.add((owners[0].kind, owners[0].id))
    return hits


@dataclass
class AiScope:
    stamp: VisibilityStamp
    entries: dict[EntryKey, MentionCandidate]
    direct: frozenset[EntryKey]
    selected: set[EntryKey]
    kinds: frozenset[str] | None = None

    def includes(self, kind: str, ident: int) -> bool:
        return (kind, ident) in self.selected

    def ids(self, kind: str) -> set[int]:
        return {ident for entry_kind, ident in self.selected if entry_kind == kind}

    def legacy(self, kind: str, ident: int) -> bool:
        return (entry := self.entries.get((kind, ident))) is not None and entry.mode is None

    def field_visible(self, kind: str, ident: int, key: str) -> bool:
        entry = self.entries.get((kind, ident))
        return (
            (kind, ident) in self.selected and entry is not None and entry.mode != "never"
            and key not in entry.hidden_builtin_fields
        )

    def hidden(self, kind: str, ident: int) -> frozenset[str]:
        entry = self.entries[(kind, ident)]
        return entry.hidden_builtin_fields

    def legacy_roster_field(self, kind: str, ident: int, key: str) -> bool:
        entry = self.entries.get((kind, ident))
        return entry is not None and entry.mode is None and key not in entry.hidden_builtin_fields

    def include_associations(self, edges: Iterable[tuple[EntryKey, EntryKey]]) -> None:
        """一跳：用不可变 direct，而非随展开增长的 selected。"""
        self.selected.update(
            target
            for source, target in edges
            if source in self.direct
            and (candidate := self.entries.get(target)) is not None and candidate.mode in _SENDABLE_MODES
            and (self.kinds is None or target[0] in self.kinds)
        )

    def require_entry(self, kind: str, ident: int, *, field: str | None = None) -> None:
        if not self.includes(kind, ident) or (field and not self.field_visible(kind, ident, field)):
            raise BadRequestError("所选设定或字段不可用于 AI，请调整引用或可见策略", extra={
                "code": "setting_not_available",
            })

    def filter_fields(self, kind: str, ident: int, data: dict[str, Any]) -> dict[str, Any]:
        """仅处理本条目的平面 DTO；嵌套关联必须按其自己的身份另行投影。"""
        return {
            key: value for key, value in data.items()
            if not key.startswith(("ai_", "mention_")) and key != "always_in_context"
            and self.field_visible(kind, ident, key)
        }

    def ref_visible(self, owner_kind: str, owner_id: int, target_kind: str, target_id: int) -> bool:
        return self.includes(owner_kind, owner_id) and (
            self.includes(target_kind, target_id)
            or (self.legacy(owner_kind, owner_id) and self.legacy(target_kind, target_id))
        )

    def _reference_visible(self, owner: SettingEntry, target: SettingEntry | None) -> bool:
        return target is None or self.ref_visible(
            owner.reference.kind, owner.reference.id, target.reference.kind, target.reference.id,
        )

    def entry_fields(self, result: EntryFieldsOut) -> EntryFieldsOut:
        self.require_entry(result.entry.reference.kind, result.entry.reference.id)

        def project(slots: list[FieldSlot]) -> list[FieldSlot]:
            output = []
            for slot in slots:
                if slot.ai_hidden:
                    continue
                current = self._reference_visible(result.entry, slot.reference)
                base = self._reference_visible(result.entry, slot.base_reference)
                if current and base:
                    output.append(slot)
                    continue
                if not current and (not base or slot.base_reference is None):
                    continue
                if not base and slot.reference is None:
                    continue
                changes: dict[str, Any] = {}
                if not current:
                    changes.update(value=None, reference=None)
                if not base:
                    changes.update(base_value=None, base_reference=None)
                output.append(slot.model_copy(update=changes))
            return output

        return result.model_copy(update={
            "fields": project(result.fields), "legacy_fields": project(result.legacy_fields),
        })

    async def verify(self, db: AsyncSession) -> None:
        await require_visibility_stamp(db, self.stamp)


async def build_scope(
    db: AsyncSession,
    project_id: int,
    *,
    texts: Iterable[str] = (),
    explicit: Iterable[EntryKey] = (),
    legacy: Iterable[EntryKey] = (),
    legacy_all: bool = False,
    legacy_kinds: Iterable[str] = (),
    legacy_metadata_kinds: Iterable[str] = (),
    kinds: Iterable[str] | None = None,
    stamp: VisibilityStamp | None = None,
    candidates: list[MentionCandidate] | None = None,
) -> AiScope:
    """选择直接候选和作者配置的一跳关联；legacy 由各消费者提供原有纳入资格。"""
    stamp = stamp if stamp is not None else await read_visibility_stamp(db, project_id)
    if stamp.project_id != project_id:
        raise ValueError("设定范围与项目不一致")
    if candidates is None:
        candidates = await load_candidates(db, project_id)
    explicit_keys = frozenset(explicit)
    legacy_keys = frozenset(legacy)
    legacy_kind_set = frozenset(legacy_kinds)
    legacy_metadata_set = frozenset(legacy_metadata_kinds)
    allowed_kinds = frozenset(kinds) if kinds is not None else None
    # 全兼容作品仍走原路径；字面新逻辑只在存在使用者时扫描。
    should_detect = any(
        entry.mode == "detected" or (entry.mode is None and entry.tracking_explicit)
        for entry in candidates
    )
    hints = tuple(text for text in texts if text) if should_detect else ()
    hits = await anyio.to_thread.run_sync(_detected, candidates, hints) if hints else set()
    direct = set()
    for entry in candidates:
        key = entry.key
        if (allowed_kinds is not None and key[0] not in allowed_kinds) or entry.mode not in _SENDABLE_MODES:
            continue
        if (
            key in explicit_keys
            or entry.mode == "always"
            or (entry.mode == "detected" and key in hits)
            or (entry.mode is None and (
                legacy_all or key in legacy_keys or key[0] in legacy_kind_set
                or (entry.tracking_explicit and key in hits)
            ))
        ):
            direct.add(key)
    metadata = {
        entry.key for entry in candidates if entry.mode is None and entry.key[0] in legacy_metadata_set
        and (allowed_kinds is None or entry.key[0] in allowed_kinds)
    }
    result = AiScope(
        stamp, {entry.key: entry for entry in candidates}, frozenset(direct), direct | metadata, allowed_kinds,
    )
    if direct:
        targets = await load_link_targets(db, project_id, result.direct)
        result.include_associations((source, target) for source, linked in targets.items() for target in linked)
    await result.verify(db)
    return result


def current_world_text(scope: AiScope, worlds: Iterable[WorldSetting]) -> str:
    from app.services.story_service import format_world

    return format_world([
        SimpleNamespace(
            title=world.title, category=world.category,
            content=world.content if scope.field_visible("world", world.id, "content") else "",
        )
        for world in worlds if scope.includes("world", world.id)
    ])


def current_character_text(scope: AiScope, characters: Iterable[Character], *, roster: bool = False) -> str:
    from app.services.story_service import format_characters, format_roster

    properties = ("role_type",) if roster else ("role_type", "personality", "background", "profile", "relations_text")
    projected = [
        SimpleNamespace(name=char.name, aliases=char.aliases, **{
            key: getattr(char, key) if scope.field_visible("character", char.id, key) else "" for key in properties
        })
        for char in characters if scope.includes("character", char.id)
    ]
    return (format_roster if roster else format_characters)(projected)
