"""提示词模板：系统默认（registry，随代码发布）+ 用户覆盖（prompt_overrides，对该用户的全部项目生效）。

- 保存：校验 → upsert，base 记为当前系统默认（指纹 + 全文）→ 内容有变化才追加历史版本并裁剪到 MAX_VERSIONS。
- outdated = 覆盖的 base_fingerprint != 当前系统默认指纹；ack_default 只更新 base。
- 停用：运行时用系统默认，覆盖内容保留；恢复默认（DELETE）删除覆盖及其历史。
- 运行时 get_override 只返回启用的覆盖；各生成服务在组装 job 时取出并写入 job（中途修改模板不影响已组装的 job）。
- 组件引用 `{{> 标识 }}`：库内存原文；保存 / 导入 / 预览 / 回滚先用作者本人的组件展开，再对展开结果做全部校验，
  问题的行号映射回原文并标明来自哪个组件；运行时 get_override 返回展开后的内容；导出写展开后的内容。
"""

import json
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.components import ComponentRefError, Expansion, expand, expand_strict, has_refs
from app.ai.prompts.registry import CATEGORIES, TEMPLATES, PromptTemplate, default_fingerprint, sample_values
from app.ai.templating import PromptOverride, TemplateIssue, TemplateRenderError, render_messages, validate
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import PromptOverrideVersion, PromptTemplateOverride
from app.schemas.prompt import (
    MAX_IMPORT_BYTES,
    PROMPTS_FORMAT,
    PROMPTS_VERSION,
    LockedBlockOut,
    PromptImportItemResult,
    PromptImportResult,
    PromptOverrideOut,
    PromptPatch,
    PromptPreviewOut,
    PromptPreviewRequest,
    PromptSave,
    PromptTemplateBrief,
    PromptTemplateDetail,
    TemplateIssueOut,
    TemplateVarOut,
)
from app.schemas.prompt_component import ComponentRef
from app.services import component_lookup

MAX_VERSIONS = 20
PART_LABELS = {"system": "系统段", "user": "用户段"}


def get_registered(key: str) -> PromptTemplate:
    tpl = TEMPLATES.get(key)
    if tpl is None:
        raise NotFoundError("提示词模板不存在")
    return tpl


def format_issue(issue: TemplateIssue) -> str:
    where = PART_LABELS.get(issue.part or "", "")
    if issue.line:
        where = f"{where}第 {issue.line} 行"
    return f"{where}：{issue.message}" if where else issue.message


def _map_issue(issue: TemplateIssue, expansions: dict[str, Expansion]) -> TemplateIssue:
    exp = expansions.get(issue.part or "")
    if exp is None:
        return issue
    line, slug = exp.map_line(issue.line)
    message = f"组件 {slug}：{issue.message}" if slug else issue.message
    return TemplateIssue(issue.part, line, message)


def check(
    tpl: PromptTemplate, system: str, user: str, contents: dict[str, str] | None = None
) -> tuple[list[TemplateIssue], str, str]:
    """先展开组件再校验；返回 (问题（行号已映射回原文）, 展开后的 system, 展开后的 user)。

    没有组件引用时与直接 validate 完全相同。
    """
    expansions: dict[str, Expansion] = {
        "system": expand(system, contents or {}),
        "user": expand(user, contents or {}),
    }
    ref_issues = [
        TemplateIssue(part, line, message)  # type: ignore[arg-type]
        for part, exp in expansions.items()
        for line, _, message in exp.errors
    ]
    exp_system, exp_user = expansions["system"].text, expansions["user"].text
    if ref_issues:
        return ref_issues, exp_system, exp_user
    issues = [_map_issue(i, expansions) for i in validate(tpl.variables, exp_system, exp_user)]
    return issues, exp_system, exp_user


def raise_issues(issues: list[TemplateIssue]) -> None:
    if issues:
        raise BadRequestError(
            "模板校验失败：" + "；".join(format_issue(i) for i in issues),
            extra={"errors": [i.as_dict() for i in issues]},
        )


def ensure_valid(
    tpl: PromptTemplate, system: str, user: str, contents: dict[str, str] | None = None
) -> tuple[str, str]:
    """校验失败 → 400：detail 为中文摘要，errors 为逐条位置。返回展开组件后的 (system, user)。"""
    issues, exp_system, exp_user = check(tpl, system, user, contents)
    raise_issues(issues)
    return exp_system, exp_user


async def _get_row(db: AsyncSession, user_id: int, key: str) -> PromptTemplateOverride | None:
    return await db.scalar(
        select(PromptTemplateOverride).where(
            PromptTemplateOverride.user_id == user_id, PromptTemplateOverride.key == key
        )
    )


async def _require_row(db: AsyncSession, user_id: int, key: str) -> PromptTemplateOverride:
    row = await _get_row(db, user_id, key)
    if row is None:
        raise NotFoundError("尚未覆盖该模板")
    return row


def _outdated(row: PromptTemplateOverride) -> bool:
    return row.base_fingerprint != default_fingerprint(row.key)


def _locked_blocks(tpl: PromptTemplate) -> list[LockedBlockOut]:
    return [LockedBlockOut(name=v.name, label=v.label, value=str(v.sample)) for v in tpl.locked]


def _brief(tpl: PromptTemplate, row: PromptTemplateOverride | None) -> PromptTemplateBrief:
    return PromptTemplateBrief(
        key=tpl.key,
        name=tpl.name,
        category=tpl.category,
        category_label=CATEGORIES.get(tpl.category, tpl.category),
        description=tpl.description,
        overridden=row is not None,
        enabled=row is not None and row.enabled,
        outdated=row is not None and _outdated(row),
        updated_at=row.updated_at if row is not None else None,
    )


async def _detail(
    db: AsyncSession, user_id: int, tpl: PromptTemplate, row: PromptTemplateOverride | None
) -> PromptTemplateDetail:
    brief = _brief(tpl, row)
    components = await component_lookup.refs(db, user_id, row.system, row.user) if row is not None else []
    return PromptTemplateDetail(
        **brief.model_dump(),
        components=[ComponentRef(**c) for c in components],
        default_system=tpl.system,
        default_user=tpl.user,
        default_fingerprint=default_fingerprint(tpl.key),
        variables=[
            TemplateVarOut(
                name=v.name, label=v.label, kind=v.kind, required=v.required, locked=v.locked, sample=v.sample
            )
            for v in tpl.variables
        ],
        locked=_locked_blocks(tpl),
        override=(
            PromptOverrideOut(
                system=row.system,
                user=row.user,
                enabled=row.enabled,
                base_fingerprint=row.base_fingerprint,
                base_system=row.base_system,
                base_user=row.base_user,
                updated_at=row.updated_at,
            )
            if row is not None
            else None
        ),
    )


async def list_templates(db: AsyncSession, user_id: int) -> list[PromptTemplateBrief]:
    rows = (await db.scalars(select(PromptTemplateOverride).where(PromptTemplateOverride.user_id == user_id))).all()
    by_key = {r.key: r for r in rows}
    return [_brief(tpl, by_key.get(key)) for key, tpl in TEMPLATES.items()]


async def get_template(db: AsyncSession, user_id: int, key: str) -> PromptTemplateDetail:
    tpl = get_registered(key)
    return await _detail(db, user_id, tpl, await _get_row(db, user_id, key))


async def apply_save(
    db: AsyncSession,
    user_id: int,
    tpl: PromptTemplate,
    system: str,
    user: str,
    note: str,
    *,
    force_version: bool = False,
) -> tuple[PromptTemplateOverride, bool]:
    """upsert 覆盖（不 commit）；返回 (覆盖, 内容是否有变化)。调用方已校验。"""
    row = await _get_row(db, user_id, tpl.key)
    created = row is None
    if row is None:
        row = PromptTemplateOverride(user_id=user_id, key=tpl.key, enabled=True)
        db.add(row)
    changed = created or row.system != system or row.user != user
    row.system, row.user = system, user
    row.base_fingerprint = default_fingerprint(tpl.key)
    row.base_system, row.base_user = tpl.system, tpl.user
    await db.flush()
    if changed or force_version:
        db.add(PromptOverrideVersion(override_id=row.id, system=system, user=user, note=note.strip()[:200]))
        await db.flush()
        await _prune_versions(db, row.id)
    return row, changed


async def ensure_current_version(db: AsyncSession, row: PromptTemplateOverride) -> None:
    """补齐旧数据未记录的当前正文（不提交），供覆盖前恢复。"""
    latest = await db.scalar(
        select(PromptOverrideVersion)
        .where(PromptOverrideVersion.override_id == row.id)
        .order_by(PromptOverrideVersion.id.desc())
        .limit(1)
    )
    if latest is None or (latest.system, latest.user) != (row.system, row.user):
        db.add(PromptOverrideVersion(override_id=row.id, system=row.system, user=row.user, note="工坊导入前的覆盖"))
        await db.flush()


async def _prune_versions(db: AsyncSession, override_id: int) -> None:
    keep = (
        select(PromptOverrideVersion.id)
        .where(PromptOverrideVersion.override_id == override_id)
        .order_by(PromptOverrideVersion.id.desc())
        .limit(MAX_VERSIONS)
    )
    await db.execute(
        delete(PromptOverrideVersion).where(
            PromptOverrideVersion.override_id == override_id, PromptOverrideVersion.id.not_in(keep)
        )
    )


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        # 并发保存同一 key 时由 (user_id, key) 唯一约束兜底
        await db.rollback()
        raise ConflictError("该模板正在被另一处保存，请刷新后重试") from exc


async def save_override(db: AsyncSession, user_id: int, key: str, data: PromptSave) -> PromptTemplateDetail:
    tpl = get_registered(key)
    ensure_valid(tpl, data.system, data.user, await component_lookup.contents(db, user_id))
    row, _ = await apply_save(db, user_id, tpl, data.system, data.user, data.note)
    await _commit(db)
    await db.refresh(row)
    return await _detail(db, user_id, tpl, row)


async def patch_override(db: AsyncSession, user_id: int, key: str, data: PromptPatch) -> PromptTemplateDetail:
    tpl = get_registered(key)
    row = await _require_row(db, user_id, key)
    if data.enabled is not None:
        row.enabled = data.enabled
    if data.ack_default:
        row.base_fingerprint = default_fingerprint(key)
        row.base_system, row.base_user = tpl.system, tpl.user
    await db.commit()
    await db.refresh(row)
    return await _detail(db, user_id, tpl, row)


async def delete_override(db: AsyncSession, user_id: int, key: str) -> None:
    get_registered(key)
    row = await _require_row(db, user_id, key)
    await db.delete(row)
    await db.commit()


async def list_versions(db: AsyncSession, user_id: int, key: str) -> list[PromptOverrideVersion]:
    get_registered(key)
    row = await _require_row(db, user_id, key)
    return list(
        (
            await db.scalars(
                select(PromptOverrideVersion)
                .where(PromptOverrideVersion.override_id == row.id)
                .order_by(PromptOverrideVersion.id.desc())
            )
        ).all()
    )


async def restore_version(db: AsyncSession, user_id: int, key: str, version_id: int) -> PromptTemplateDetail:
    """回滚 = 把该版本内容作为一次新保存（同样要通过校验；系统默认升级后旧版本可能已不合法 → 400）。"""
    tpl = get_registered(key)
    row = await _require_row(db, user_id, key)
    version = await db.scalar(
        select(PromptOverrideVersion).where(
            PromptOverrideVersion.id == version_id, PromptOverrideVersion.override_id == row.id
        )
    )
    if version is None:
        raise NotFoundError("历史版本不存在")
    ensure_valid(tpl, version.system, version.user, await component_lookup.contents(db, user_id))
    row, _ = await apply_save(db, user_id, tpl, version.system, version.user, f"回滚到版本 #{version.id}")
    await db.commit()
    await db.refresh(row)
    return await _detail(db, user_id, tpl, row)


async def preview(db: AsyncSession, user_id: int, key: str, data: PromptPreviewRequest) -> PromptPreviewOut:
    """用请求体中的模板与变量渲染（变量缺省用示例值），返回 messages 与校验问题；不落库。组件先展开。"""
    tpl = get_registered(key)
    contents = await component_lookup.contents(db, user_id) if has_refs(data.system) or has_refs(data.user) else {}
    issues, system, user = check(tpl, data.system, data.user, contents)
    components = [ComponentRef(**c) for c in await component_lookup.refs(db, user_id, data.system, data.user)]
    locked = _locked_blocks(tpl)
    errors = [TemplateIssueOut(**i.as_dict()) for i in issues]
    if issues:
        return PromptPreviewOut(messages=[], errors=errors, locked=locked, components=components)
    values = sample_values(key)
    for var in tpl.variables:
        if not var.locked and var.name in data.variables:
            values[var.name] = _coerce(var.kind, data.variables[var.name])
    try:
        result = render_messages(tpl.variables, system, user, values, key=key)
    except TemplateRenderError as exc:
        errors.append(TemplateIssueOut(part=None, line=None, message=exc.detail))
        return PromptPreviewOut(messages=[], errors=errors, locked=locked, components=components)
    return PromptPreviewOut(
        messages=result.messages, errors=errors, locked=locked, components=components  # type: ignore[arg-type]
    )


def _coerce(kind: str, value: Any) -> Any:
    if kind == "list":
        return [str(v) for v in value] if isinstance(value, list) else []
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if kind == "bool":
        return bool(value)
    return "" if value is None else str(value)


async def get_override(db: AsyncSession, user_id: int, key: str) -> PromptOverride | None:
    """运行时取用：只返回启用的覆盖；version_id 为当前内容对应的最新历史版本。"""
    row = await _get_row(db, user_id, key)
    if row is None or not row.enabled:
        return None
    version_id = await db.scalar(
        select(func.max(PromptOverrideVersion.id)).where(PromptOverrideVersion.override_id == row.id)
    )
    system, user = row.system, row.user
    if has_refs(system) or has_refs(user):
        contents = await component_lookup.contents(db, user_id)
        try:
            system, user = expand_strict(system, contents), expand_strict(user, contents)
        except ComponentRefError as exc:
            name = TEMPLATES[key].name if key in TEMPLATES else key
            reason = f"引用的组件 {exc.slug} 不存在" if exc.message.startswith("引用的组件不存在") else exc.message
            raise TemplateRenderError(f"提示词模板「{name}」{reason}，请检查") from exc
    return PromptOverride(system=system, user=user, version_id=version_id)


def _expand_for_export(text: str, contents: dict[str, str]) -> str:
    """离开作者本人时展开为正文；展开失败（理论上不会发生）时保留原文。"""
    if not has_refs(text):
        return text
    result = expand(text, contents)
    return text if result.errors else result.text


# ---------------------------------------------------------------- 导入导出


async def export_overrides(db: AsyncSession, user_id: int) -> dict[str, Any]:
    rows = (
        await db.scalars(
            select(PromptTemplateOverride)
            .where(PromptTemplateOverride.user_id == user_id)
            .order_by(PromptTemplateOverride.key)
        )
    ).all()
    contents = await component_lookup.contents(db, user_id) if any(
        has_refs(r.system) or has_refs(r.user) for r in rows
    ) else {}
    return {
        "format": PROMPTS_FORMAT,
        "version": PROMPTS_VERSION,
        "items": [
            {
                "key": r.key,
                "system": _expand_for_export(r.system, contents),
                "user": _expand_for_export(r.user, contents),
                "enabled": r.enabled,
                "base_fingerprint": r.base_fingerprint,
            }
            for r in rows
        ],
    }


def _parse_import(data: Any) -> list[Any]:
    size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > MAX_IMPORT_BYTES:
        raise BadRequestError("模板文件过大，上限为 1MB")
    if not isinstance(data, dict) or data.get("format") != PROMPTS_FORMAT:
        raise BadRequestError("模板文件格式不正确：不是 ainovel.prompts 导出文件")
    if data.get("version") != PROMPTS_VERSION:
        raise BadRequestError(f"不支持的模板文件版本：{data.get('version')}（当前支持 {PROMPTS_VERSION}）")
    items = data.get("items")
    if not isinstance(items, list):
        raise BadRequestError("模板文件格式不正确：items 必须是数组")
    return items


async def import_overrides(db: AsyncSession, user_id: int, data: Any) -> PromptImportResult:
    """逐项导入：未知 key 跳过；每项都要通过保存时的校验；与系统默认完全相同的不创建覆盖。整个导入一个事务。"""
    items = _parse_import(data)
    contents = await component_lookup.contents(db, user_id)
    results: list[PromptImportItemResult] = []
    seen: set[str] = set()
    for raw in items:
        key = str(raw.get("key", "")) if isinstance(raw, dict) else ""
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("user"), str)
            or not isinstance(raw.get("system", ""), str)
        ):
            results.append(PromptImportItemResult(key=key, status="skipped", message="条目格式不正确"))
            continue
        tpl = TEMPLATES.get(key)
        if tpl is None:
            results.append(PromptImportItemResult(key=key, status="skipped", message="未知的模板 key，已跳过"))
            continue
        if key in seen:
            results.append(PromptImportItemResult(key=key, status="skipped", message="文件内重复，只取第一项"))
            continue
        seen.add(key)
        system, user = raw.get("system", ""), raw["user"]
        issues, _, _ = check(tpl, system, user, contents)
        if issues:
            results.append(
                PromptImportItemResult(
                    key=key,
                    status="invalid",
                    message="模板校验失败",
                    errors=[TemplateIssueOut(**i.as_dict()) for i in issues],
                )
            )
            continue
        if system == tpl.system and user == tpl.user:
            results.append(PromptImportItemResult(key=key, status="default", message="与系统默认相同，未创建覆盖"))
            continue
        existed = await _get_row(db, user_id, key) is not None
        row, changed = await apply_save(db, user_id, tpl, system, user, "从文件导入")
        if isinstance(raw.get("enabled"), bool):
            row.enabled = raw["enabled"]
        status = "unchanged" if not changed else ("updated" if existed else "created")
        results.append(PromptImportItemResult(key=key, status=status))
    await _commit(db)
    return PromptImportResult(items=results)
