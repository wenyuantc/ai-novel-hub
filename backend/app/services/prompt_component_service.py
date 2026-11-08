"""提示词组件库：作者个人的可复用片段（CRUD、修订号 CAS、上限、使用关系、引用完整性、导入导出）。

- 归属：只属于作者本人，他人的组件一律 404，也不能被引用（展开只查本人的组件）。
- 引用方：本人的 Skill 正文、本人的模板覆盖 system / user 段（原文）。不建关系表，
  查询时现扫（本人 ≤100 Skill + ≤31 覆盖）。
- 完整性：被引用的组件不能删除、不能改标识（409 component_in_use）；
  改内容会让引用它的覆盖校验失败时拒绝（400 component_breaks_usages）。
- 并发：更新带 revision，不符 → 409 stale_revision。覆盖保存与组件修改的竞态由运行时的可读错误兜底。
"""

import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.components import has_refs, ref_slugs
from app.ai.prompts.registry import TEMPLATES
from app.ai.templating import TemplateIssue
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import PromptComponent, PromptTemplateOverride, Skill
from app.schemas.prompt_component import (
    COMPONENTS_FORMAT,
    COMPONENTS_VERSION,
    MAX_IMPORT_BYTES,
    ComponentBrief,
    ComponentCreate,
    ComponentFields,
    ComponentImportItemResult,
    ComponentImportResult,
    ComponentOut,
    ComponentUpdate,
    ComponentUsage,
)
from app.services import component_lookup, prompt_service

MAX_COMPONENTS = 200
NOT_FOUND = "组件不存在"
NESTED = "组件内不能引用其他组件"
SLUG_TAKEN = "标识已被使用，请换一个"


# ---------------------------------------------------------------- 使用关系


async def _usages(db: AsyncSession, user_id: int) -> dict[str, list[ComponentUsage]]:
    """{标识: 引用方}；只扫描本人的 Skill 与覆盖中含 `{{>` 的行。"""
    result: dict[str, list[ComponentUsage]] = {}
    skills = await db.scalars(
        select(Skill).where(Skill.owner_user_id == user_id, Skill.body.contains("{{>")).order_by(Skill.id)
    )
    for skill in skills:
        for slug in ref_slugs(skill.body):
            result.setdefault(slug, []).append(ComponentUsage(kind="skill", id=skill.id, name=skill.name))
    overrides = await db.scalars(
        select(PromptTemplateOverride)
        .where(
            PromptTemplateOverride.user_id == user_id,
            or_(PromptTemplateOverride.system.contains("{{>"), PromptTemplateOverride.user.contains("{{>")),
        )
        .order_by(PromptTemplateOverride.key)
    )
    for row in overrides:
        tpl = TEMPLATES.get(row.key)
        for slug in dict.fromkeys([*ref_slugs(row.system), *ref_slugs(row.user)]):
            result.setdefault(slug, []).append(
                ComponentUsage(kind="override", key=row.key, name=tpl.name if tpl else row.key, enabled=row.enabled)
            )
    return result


def _in_use(slug: str, usages: list[ComponentUsage], action: str) -> ConflictError:
    names = "、".join(f"{'Skill' if u.kind == 'skill' else '模板'}「{u.name}」" for u in usages)
    return ConflictError(
        f"组件 {slug} 正被 {names} 引用，不能{action}；请先移除这些引用",
        extra={"code": "component_in_use", "usages": [u.model_dump() for u in usages]},
    )


async def _impact(db: AsyncSession, user_id: int, slug: str, content: str) -> list[dict[str, Any]]:
    """用新内容展开并校验引用该组件的每个覆盖；返回失败项 [{key, name, errors}]。"""
    rows = (
        await db.scalars(
            select(PromptTemplateOverride)
            .where(
                PromptTemplateOverride.user_id == user_id,
                or_(PromptTemplateOverride.system.contains("{{>"), PromptTemplateOverride.user.contains("{{>")),
            )
            .order_by(PromptTemplateOverride.key)
        )
    ).all()
    rows = [r for r in rows if slug in ref_slugs(r.system) or slug in ref_slugs(r.user)]
    if not rows:
        return []
    contents = {**await component_lookup.contents(db, user_id), slug: content}
    failures: list[dict[str, Any]] = []
    for row in rows:
        tpl = TEMPLATES.get(row.key)
        if tpl is None:
            continue
        issues, _, _ = prompt_service.check(tpl, row.system, row.user, contents)
        if issues:
            failures.append({"key": row.key, "name": tpl.name, "errors": [i.as_dict() for i in issues]})
    return failures


def _breaks(slug: str, failures: list[dict[str, Any]]) -> BadRequestError:
    reasons = "；".join(
        f"模板「{f['name']}」：" + "；".join(prompt_service.format_issue(_issue(e)) for e in f["errors"])
        for f in failures
    )
    return BadRequestError(
        f"修改组件 {slug} 会让引用它的模板覆盖校验失败：{reasons}",
        extra={"code": "component_breaks_usages", "failures": failures},
    )


def _issue(data: dict[str, Any]) -> TemplateIssue:
    return TemplateIssue(data.get("part"), data.get("line"), data.get("message", ""))


# ---------------------------------------------------------------- 输出


def _brief(row: PromptComponent, usage_count: int) -> ComponentBrief:
    return ComponentBrief(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        revision=row.revision,
        content_chars=len(row.content),
        usage_count=usage_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def to_out(db: AsyncSession, user_id: int, row: PromptComponent) -> ComponentOut:
    usages = (await _usages(db, user_id)).get(row.slug, [])
    return ComponentOut(**_brief(row, len(usages)).model_dump(), content=row.content, usages=usages)


async def list_components(db: AsyncSession, user_id: int, q: str = "") -> list[ComponentBrief]:
    stmt = select(PromptComponent).where(PromptComponent.user_id == user_id)
    q = q.strip()
    if q:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(
            or_(
                PromptComponent.slug.ilike(pattern, escape="\\"),
                PromptComponent.name.ilike(pattern, escape="\\"),
                PromptComponent.description.ilike(pattern, escape="\\"),
                PromptComponent.content.ilike(pattern, escape="\\"),
            )
        )
    rows = (await db.scalars(stmt.order_by(PromptComponent.slug))).all()
    usages = await _usages(db, user_id)
    return [_brief(r, len(usages.get(r.slug, []))) for r in rows]


async def get_component(db: AsyncSession, user_id: int, component_id: int) -> PromptComponent:
    row = await db.scalar(
        select(PromptComponent)
        .where(PromptComponent.id == component_id, PromptComponent.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundError(NOT_FOUND)
    return row


# ---------------------------------------------------------------- 写


def _check_content(content: str) -> None:
    if has_refs(content):
        raise BadRequestError(NESTED)


async def _check_quota(db: AsyncSession, user_id: int) -> None:
    count = await db.scalar(select(func.count(PromptComponent.id)).where(PromptComponent.user_id == user_id))
    if (count or 0) >= MAX_COMPONENTS:
        raise BadRequestError(f"最多保存 {MAX_COMPONENTS} 个组件")


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(SLUG_TAKEN) from exc


async def _slug_taken(db: AsyncSession, user_id: int, slug: str, exclude_id: int | None = None) -> bool:
    stmt = select(PromptComponent.id).where(PromptComponent.user_id == user_id, PromptComponent.slug == slug)
    if exclude_id is not None:
        stmt = stmt.where(PromptComponent.id != exclude_id)
    return await db.scalar(stmt) is not None


async def create_component(db: AsyncSession, user_id: int, data: ComponentCreate) -> ComponentOut:
    _check_content(data.content)
    await _check_quota(db, user_id)
    if await _slug_taken(db, user_id, data.slug):
        raise ConflictError(SLUG_TAKEN)
    row = PromptComponent(
        user_id=user_id, slug=data.slug, name=data.name, description=data.description, content=data.content
    )
    db.add(row)
    await _commit(db)
    await db.refresh(row)
    return await to_out(db, user_id, row)


def _stale() -> ConflictError:
    return ConflictError(
        "组件已被修改，请加载最新版本后再保存；当前草稿可继续保留", extra={"code": "stale_revision"}
    )


async def _guard_change(db: AsyncSession, user_id: int, row: PromptComponent, data: ComponentFields) -> None:
    """改标识：被引用 → 409；改内容：会让引用它的覆盖校验失败 → 400。"""
    if data.slug != row.slug:
        usages = (await _usages(db, user_id)).get(row.slug, [])
        if usages:
            raise _in_use(row.slug, usages, "修改标识")
        if await _slug_taken(db, user_id, data.slug, exclude_id=row.id):
            raise ConflictError(SLUG_TAKEN)
    if data.content != row.content:
        failures = await _impact(db, user_id, row.slug, data.content)
        if failures:
            raise _breaks(row.slug, failures)


async def update_component(db: AsyncSession, user_id: int, component_id: int, data: ComponentUpdate) -> ComponentOut:
    row = await get_component(db, user_id, component_id)
    if row.revision != data.revision:
        raise _stale()
    _check_content(data.content)
    await _guard_change(db, user_id, row, data)
    values = {"slug": data.slug, "name": data.name, "description": data.description, "content": data.content}
    if all(getattr(row, k) == v for k, v in values.items()):
        return await to_out(db, user_id, row)
    result = await db.execute(
        update(PromptComponent)
        .where(
            PromptComponent.id == component_id,
            PromptComponent.user_id == user_id,
            PromptComponent.revision == data.revision,
        )
        .values(**values, revision=PromptComponent.revision + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _stale()
    await _commit(db)
    return await to_out(db, user_id, await get_component(db, user_id, component_id))


async def delete_component(db: AsyncSession, user_id: int, component_id: int) -> None:
    row = await get_component(db, user_id, component_id)
    usages = (await _usages(db, user_id)).get(row.slug, [])
    if usages:
        raise _in_use(row.slug, usages, "删除")
    await db.delete(row)
    await db.commit()


# ---------------------------------------------------------------- 导入导出


async def export_components(db: AsyncSession, user_id: int) -> dict[str, Any]:
    rows = (
        await db.scalars(
            select(PromptComponent).where(PromptComponent.user_id == user_id).order_by(PromptComponent.slug)
        )
    ).all()
    return {
        "format": COMPONENTS_FORMAT,
        "version": COMPONENTS_VERSION,
        "items": [
            {"slug": r.slug, "name": r.name, "description": r.description, "content": r.content} for r in rows
        ],
    }


def _parse_import(data: Any) -> list[Any]:
    size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > MAX_IMPORT_BYTES:
        raise BadRequestError("组件文件过大，上限为 1MB")
    if not isinstance(data, dict) or data.get("format") != COMPONENTS_FORMAT:
        raise BadRequestError("组件文件格式不正确：不是 ainovel.prompt-components 导出文件")
    if data.get("version") != COMPONENTS_VERSION:
        raise BadRequestError(f"不支持的组件文件版本：{data.get('version')}（当前支持 {COMPONENTS_VERSION}）")
    items = data.get("items")
    if not isinstance(items, list):
        raise BadRequestError("组件文件格式不正确：items 必须是数组")
    return items


def _validation_reason(exc: ValidationError) -> str:
    return "；".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {str(e['msg']).removeprefix('Value error, ')}"
        for e in exc.errors(include_input=False)
    )


async def import_components(db: AsyncSession, user_id: int, data: Any) -> ComponentImportResult:
    """逐项导入：按标识新建或更新；更新同样做引用影响校验。整个导入一个事务。"""
    items = _parse_import(data)
    results: list[ComponentImportItemResult] = []
    seen: set[str] = set()
    existing = {
        r.slug: r
        for r in (await db.scalars(select(PromptComponent).where(PromptComponent.user_id == user_id))).all()
    }
    count = len(existing)
    for raw in items:
        slug = str(raw.get("slug", ""))[:48] if isinstance(raw, dict) else ""
        if not isinstance(raw, dict):
            results.append(ComponentImportItemResult(slug=slug, status="invalid", message="条目格式不正确"))
            continue
        try:
            item = ComponentCreate.model_validate(
                {k: raw.get(k, "") for k in ("slug", "name", "description", "content")}
            )
        except ValidationError as exc:
            results.append(ComponentImportItemResult(slug=slug, status="invalid", message=_validation_reason(exc)))
            continue
        if item.slug in seen:
            results.append(
                ComponentImportItemResult(slug=item.slug, status="invalid", message="文件内重复，只取第一项")
            )
            continue
        seen.add(item.slug)
        if has_refs(item.content):
            results.append(ComponentImportItemResult(slug=item.slug, status="invalid", message=NESTED))
            continue
        row = existing.get(item.slug)
        if row is None:
            if count >= MAX_COMPONENTS:
                results.append(
                    ComponentImportItemResult(
                        slug=item.slug, status="invalid", message=f"最多保存 {MAX_COMPONENTS} 个组件"
                    )
                )
                continue
            row = PromptComponent(
                user_id=user_id, slug=item.slug, name=item.name, description=item.description, content=item.content
            )
            db.add(row)
            await db.flush()
            existing[item.slug] = row
            count += 1
            results.append(ComponentImportItemResult(slug=item.slug, status="created"))
            continue
        if (row.name, row.description, row.content) == (item.name, item.description, item.content):
            results.append(ComponentImportItemResult(slug=item.slug, status="unchanged"))
            continue
        if item.content != row.content:
            failures = await _impact(db, user_id, row.slug, item.content)
            if failures:
                results.append(
                    ComponentImportItemResult(
                        slug=item.slug, status="invalid", message=_breaks(row.slug, failures).detail, failures=failures
                    )
                )
                continue
        row.name, row.description, row.content = item.name, item.description, item.content
        row.revision = row.revision + 1
        await db.flush()
        results.append(ComponentImportItemResult(slug=item.slug, status="updated"))
    await _commit(db)
    return ComponentImportResult(items=results)
