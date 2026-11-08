"""作者 AI 设置、同项目一跳关联及可见性配置代次。"""

from collections import defaultdict
from collections.abc import Collection
from itertools import batched
from typing import cast

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ConflictError, NotFoundError
from app.models import Project, SettingAiLink, SettingReferenceHandle
from app.schemas import setting_ai_visibility as s
from app.schemas.setting_fields import EntityRef, EntryKind, SettingEntry
from app.services import authorization_service as authz
from app.services import project_lifecycle, series_policy
from app.services import setting_catalog_service as catalog
from app.services.audit_writer import AuditOperation
from app.services.project_service import get_manageable_project, get_owned_project

HIDEABLE_BUILTIN_FIELDS = s.HIDEABLE_BUILTIN_FIELDS
EntryKey = catalog.EntryKey
_MODE_RANK = {"never": 0, "manual_only": 1, "detected": 2, "always": 3}


class InvalidAiSettingsError(AppError):
    status_code = 422


def effective_mention_enabled(
    kind: str,
    ai_mode: str | None,
    mention_enabled: bool | None,
    *,
    category: str | None = None,
) -> bool:
    if mention_enabled is not None:
        return mention_enabled
    return ai_mode is not None or kind == "character" or (kind == "world" and category in ("place", "item"))


def normalize_settings(kind: EntryKind, values: s.StoredAiSettings) -> s.StoredAiSettings:
    fields = HIDEABLE_BUILTIN_FIELDS[kind]
    hidden = set(values.ai_hidden_builtin_fields)
    if hidden.difference(fields):
        raise InvalidAiSettingsError("该设定条目包含不可隐藏的内建字段")
    return values.model_copy(update={
        "ai_hidden_builtin_fields": [field for field in fields if field in hidden],
        "mention_exclusions": s.normalize_exclusions(
            values.mention_exclusions, case_sensitive=values.mention_case_sensitive,
        ),
    })


def stale_visibility_revision(revision: int) -> ConflictError:
    return ConflictError(
        "AI 与提及设置已被其他操作修改，请刷新后保留草稿重试",
        extra={"code": "stale_revision", "scope": "ai_visibility", "current_revision": revision},
    )


async def visibility_revision(db: AsyncSession, project_id: int) -> int:
    with db.no_autoflush:
        revision = await db.scalar(select(Project.ai_visibility_revision).where(Project.id == project_id))
    if revision is None:
        raise NotFoundError("项目不存在")
    return revision


async def check_visibility_revision(db: AsyncSession, project_id: int, expected: int) -> int:
    current = await visibility_revision(db, project_id)
    if current != expected:
        raise stale_visibility_revision(current)
    return current


async def lock_author_visibility_write(
    db: AsyncSession, user_id: int, project_id: int, *, manage: bool = True,
) -> Project:
    """权限写屏障 → 发送写门禁 → 父项目锁；所有策略写路径保持同一顺序。"""
    from app.services import ai_dispatch

    sqlite = db.get_bind().dialect.name == "sqlite"
    timeout = int(await db.scalar(text("PRAGMA busy_timeout")) or 0) if sqlite else None
    if sqlite:
        # SQLite 写锁覆盖整库：认证屏障发生在可见性门禁之前，也须遵循同一限时。
        await db.execute(text(f"PRAGMA busy_timeout = {int(ai_dispatch._LOCK_WAIT_SECONDS * 1000)}"))

    try:
        if manage:
            project = await get_manageable_project(db, user_id, project_id)
            if project.user_id != user_id:
                await authz.require(db, user_id, "projects.edit", write=True, targets=(project.user_id,))
            else:
                await authz.require_authenticated(db, user_id, write=True)
        else:
            await get_owned_project(db, user_id, project_id)
            await authz.require_authenticated(db, user_id, write=True)
        await ai_dispatch.lock_visibility_write(db, project_id)
        return await project_lifecycle.lock_active(db, project_id)
    except OperationalError as exc:
        if sqlite and ai_dispatch.is_sqlite_busy(exc):
            raise ai_dispatch.VisibilityBusyError() from None
        raise
    finally:
        if timeout is not None:
            await db.execute(text(f"PRAGMA busy_timeout = {timeout}"))


async def bump_visibility_revision(
    db: AsyncSession, project_id: int, *, tighten: bool, expected: int | None = None,
) -> int:
    """调用方已取得发送写门禁及父项目锁；只在真实配置变化时调用，不提交。"""
    statement = update(Project).where(Project.id == project_id)
    if expected is not None:
        statement = statement.where(Project.ai_visibility_revision == expected)
    revision = await db.scalar(
        statement.values(
            ai_visibility_revision=Project.ai_visibility_revision + 1,
            ai_history_epoch=Project.ai_history_epoch + int(tighten),
        )
        .returning(Project.ai_visibility_revision)
        .execution_options(synchronize_session="fetch")
    )
    if revision is None:
        raise stale_visibility_revision(await visibility_revision(db, project_id))
    return revision


async def load_link_targets(
    db: AsyncSession, project_id: int, source_keys: Collection[EntryKey],
) -> dict[EntryKey, tuple[EntryKey, ...]]:
    """仅取指定根的一跳目标；不解析目标出边，不读档案正文。"""
    links: dict[EntryKey, list[EntryKey]] = defaultdict(list)
    with db.no_autoflush:
        for keys in batched(sorted(set(source_keys)), 200):
            source_handles = await catalog.find_reference_handles(db, project_id, keys)
            sources = {handle.id: key for key, handle in source_handles.items()}
            if not sources:
                continue
            rows = await db.execute(
                select(SettingAiLink.source_handle_id, SettingReferenceHandle)
                .join(
                    SettingReferenceHandle,
                    (SettingReferenceHandle.id == SettingAiLink.target_handle_id)
                    & (SettingReferenceHandle.project_id == SettingAiLink.project_id),
                )
                .where(SettingAiLink.project_id == project_id, SettingAiLink.source_handle_id.in_(sources))
                .order_by(SettingAiLink.id)
            )
            for source_id, target in rows:
                links[sources[source_id]].append(catalog.reference_handle_key(target))
    return {source: tuple(sorted(targets)) for source, targets in links.items()}


def _ref(key: EntryKey) -> EntityRef:
    return EntityRef(kind=cast(EntryKind, key[0]), id=key[1])


def _public_settings(
    entry: SettingEntry, stored: s.StoredAiSettings, links: list[SettingEntry], revision: int,
) -> s.EntryAiSettingsOut:
    kind = entry.reference.kind
    return s.EntryAiSettingsOut(
        **(stored.model_dump() | {
            "mention_enabled": effective_mention_enabled(
                kind, stored.ai_mode, stored.mention_enabled, category=entry.category,
            ),
        }),
        hideable_builtin_fields=list(HIDEABLE_BUILTIN_FIELDS[kind]),
        links=links,
        ai_visibility_revision=revision,
    )


async def read_settings(db: AsyncSession, user_id: int, project_id: int, kind: EntryKind, entry_id: int):
    with db.no_autoflush:
        await get_manageable_project(db, user_id, project_id)
        before = await visibility_revision(db, project_id)
        entry = await catalog.get_entry(db, project_id, kind, entry_id)
        model = catalog.ENTRY_MODELS[kind]
        stored = (await db.execute(
            select(*(getattr(model, field) for field in s.StoredAiSettings.model_fields))
            .where(model.project_id == project_id, model.id == entry_id)
        )).one_or_none()
        if stored is None:
            raise NotFoundError("设定条目不存在")
        targets = (await load_link_targets(db, project_id, [(kind, entry_id)])).get((kind, entry_id), ())
        try:
            target_entries = await catalog.get_entries(db, project_id, [_ref(key) for key in targets])
        except NotFoundError:
            await check_visibility_revision(db, project_id, before)
            raise
        await check_visibility_revision(db, project_id, before)
        return _public_settings(
            entry, s.StoredAiSettings.model_validate(stored), [target_entries[key] for key in targets], before,
        )


def _tightens(
    entry: SettingEntry,
    before: s.StoredAiSettings,
    after: s.StoredAiSettings,
    *,
    removed_links: bool,
) -> bool:
    kind = entry.reference.kind
    previous_enabled = effective_mention_enabled(kind, before.ai_mode, before.mention_enabled, category=entry.category)
    next_enabled = effective_mention_enabled(kind, after.ai_mode, after.mention_enabled, category=entry.category)
    # NULL 的资格随旧消费者而异；除始终允许外，切换显式档位可能减少披露。
    narrowed_mode = _MODE_RANK.get(after.ai_mode, 3) < _MODE_RANK.get(before.ai_mode, 3)

    def exclusion_keys(values: s.StoredAiSettings) -> set[str]:
        return {
            phrase if values.mention_case_sensitive else phrase.casefold() for phrase in values.mention_exclusions
        }

    # 匹配有全词典竞争：启用候选、放宽大小写或移除排除词也可能遮蔽另一条目的原唯一命中。
    changed_matching = previous_enabled != next_enabled or (previous_enabled and next_enabled and (
        before.mention_case_sensitive != after.mention_case_sensitive
        or exclusion_keys(before) != exclusion_keys(after)
        # 两边匹配规则相同的剩余情况中，旧角色显式设为开启也会切换到统一歧义算法。
        or (kind == "character" and before.ai_mode is None and after.ai_mode is None
            and before.mention_enabled != after.mention_enabled)
    ))
    return bool(
        narrowed_mode
        or changed_matching
        or removed_links
        or set(after.ai_hidden_builtin_fields) - set(before.ai_hidden_builtin_fields)
    )


async def replace_links(
    db: AsyncSession, project_id: int, source: SettingEntry, targets: Collection[SettingEntry],
) -> None:
    """调用者已校验归属、数量、自关联且持写锁；保留未变化边与句柄身份。"""
    handles = await catalog.ensure_reference_handles(db, project_id, [source, *targets])
    source_handle_id = handles[source.reference.kind, source.reference.id].id
    target_ids = {handles[target.reference.kind, target.reference.id].id for target in targets}
    condition = (SettingAiLink.project_id == project_id) & (SettingAiLink.source_handle_id == source_handle_id)
    existing_ids = set(await db.scalars(select(SettingAiLink.target_handle_id).where(condition)))
    removed = existing_ids - target_ids
    if removed:
        await db.execute(delete(SettingAiLink).where(condition, SettingAiLink.target_handle_id.in_(removed)))
    db.add_all([
        SettingAiLink(project_id=project_id, source_handle_id=source_handle_id, target_handle_id=target_id)
        for target_id in sorted(target_ids - existing_ids)
    ])
    await db.flush()


async def validate_project_links(db: AsyncSession, project_id: int) -> None:
    """供导入与撤销复核同一配置约束；两端的项目和身份由 FK／字段图共同验证。"""
    with db.no_autoflush:
        excessive = await db.scalar(
            select(SettingAiLink.source_handle_id).where(SettingAiLink.project_id == project_id)
            .group_by(SettingAiLink.source_handle_id).having(func.count() > s.MAX_AI_LINKS).limit(1)
        )
    if excessive is not None:
        raise ConflictError("设定的单向关联目标超过上限", extra={"code": "invalid_ai_link_graph"})


async def write_settings(
    db: AsyncSession, user_id: int, project_id: int, kind: EntryKind, entry_id: int, data: s.EntryAiSettingsUpdate,
) -> s.EntryAiSettingsOut:
    async with AuditOperation(db, user_id, "project.update", "project", project_id, project_id=project_id) as audit:
        try:
            project = await lock_author_visibility_write(db, user_id, project_id)
            audit.target(project)
            model = catalog.ENTRY_MODELS[kind]
            item = await db.scalar(
                select(model).where(model.project_id == project_id, model.id == entry_id)
                .execution_options(populate_existing=True)
            )
            if item is None:
                raise NotFoundError("设定条目不存在")
            revision = await check_visibility_revision(db, project_id, data.expected_ai_visibility_revision)
            entry = await catalog.get_entry(db, project_id, kind, entry_id)
            patch = data.model_dump(exclude_unset=True, exclude={"links", "expected_ai_visibility_revision"})
            old = s.StoredAiSettings.model_validate(item)
            if old.ai_mode is None and old.mention_enabled is None and "ai_mode" in patch:
                # 档位不隐式改旧条目的追踪；作者可在同一 PATCH 显式开启。
                patch.setdefault(
                    "mention_enabled", effective_mention_enabled(kind, None, None, category=entry.category),
                )
            new = normalize_settings(kind, s.StoredAiSettings.model_validate(old.model_dump() | patch))
            source = (kind, entry_id)
            current_targets = (await load_link_targets(db, project_id, [source])).get(source, ())
            next_targets = (
                tuple(sorted((target.kind, target.id) for target in data.links))
                if data.links is not None else current_targets
            )
            if source in next_targets:
                raise InvalidAiSettingsError("不能将设定条目关联到自身")
            entries = await catalog.get_entries(db, project_id, [_ref(key) for key in next_targets])
            targets = [entries[key] for key in next_targets]
            changed = {key: value for key, value in new.model_dump().items() if getattr(old, key) != value}
            if kind in ("character", "world") and changed.keys() & series_policy.AI_SHARED_FIELDS:
                await series_policy.reject_bound_entry(db, project_id, kind, entry_id)
            links_changed = next_targets != current_targets
            if changed or links_changed:
                revision = await bump_visibility_revision(
                    db, project_id, expected=revision,
                    tighten=_tightens(entry, old, new, removed_links=bool(set(current_targets) - set(next_targets))),
                )
                for key, value in changed.items():
                    setattr(item, key, value)
                if links_changed:
                    await replace_links(db, project_id, entry, targets)
                audit.summary.changed_fields = sorted(set(changed) | ({"links"} if links_changed else set()))
                audit.affected([entry_id])
                await audit.commit()
            else:
                await db.commit()
            return _public_settings(entry, new, targets, revision)
        except BaseException:
            await db.rollback()
            raise
