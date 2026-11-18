"""项目备份导出：流式写出 ainovel.project JSON。

- 先用请求 session 校验归属（他人项目 404），再由流式生成器用自己的 session 按注册表逐表查询、逐行编码；
  有 id 的表按 id 键集分页（每批 BATCH 行），大项目不会一次性载入内存。
- 外键原值导出；被引用表因可选项未勾选而不导出时，该外键导出为 null
  （如不导出生成记录时的 chapter_versions.generation_log_id）。
- 风格：项目默认风格与章节风格写进 styles[]（ref = s1、s2…），预设带 preset_key，
  所有风格都带全部字段（预设缺失时据此复制）。
- 全局预置关系类型带 global=true（导入时按 role_a / role_b 对应）。
"""

import base64
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import JSON, String, Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError, NotFoundError
from app.models import Chapter, ChapterCharacter, GenerationLog, InspirationSession, Project, ProjectCover, WritingStyle
from app.schemas.transfer import BackupEstimate, BackupOptions, EstimateGroup
from app.services import authorization_service, personal_projection, project_lifecycle, setting_ai_visibility
from app.services.project_service import get_owned_project
from app.services.project_transfer import thumbnails
from app.services.project_transfer.registry import PROJECT_RULE, RULES, TableRule
from app.services.project_transfer.serialization import SerializationState, serialization_state
from app.services.style_service import FIELD_LIMITS

FORMAT = "ainovel.project"
VERSION = 15
# 导出时的数据库迁移版本，仅供参考（新增迁移时同步更新）
APP_SCHEMA = "0064"
BATCH = 500
# 单个富文档 JSON 的上限远高于普通行：逐行查询避免一个批次放大到数 GB。
RICH_ROW_BATCH = 1
BUFFER_CHARS = 1_000_000
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
STYLE_FIELDS = ("name", *FIELD_LIMITS)


@dataclass(frozen=True)
class ExportPlan:
    project_id: int
    owner_id: int
    last_deleted_at: datetime | None
    title: str
    options: BackupOptions
    actor: authorization_service.AuthenticatedActor | int
    image_versions: dict[tuple[str, int], str]
    ai_revision: int
    planning_revision: int
    content_revisions: dict[int, int]
    serialization_state: SerializationState

    @property
    def filename(self) -> str:
        base = _UNSAFE_FILENAME_RE.sub("_", self.title).strip() or "novel"
        return f"{base}-备份-{datetime.now(UTC):%Y%m%d}.ainovel.json"

    @property
    def content_disposition(self) -> str:
        # RFC 5987：ASCII 兜底名 + UTF-8 编码的真实文件名
        return f"attachment; filename=\"backup.ainovel.json\"; filename*=UTF-8''{quote(self.filename, safe='')}"


def included_tables(options: BackupOptions) -> set[str]:
    chosen = options.model_dump()
    return {r.table for r in RULES if r.option is None or chosen.get(r.option)}


async def prepare_export(db: AsyncSession, user_id: int, project_id: int, options: BackupOptions) -> ExportPlan:
    project = await get_owned_project(db, user_id, project_id)
    content_revisions = dict((await db.execute(select(Chapter.id, Chapter.content_revision)
        .where(Chapter.project_id == project.id))).all())
    return ExportPlan(project_id=project.id, owner_id=project.user_id, last_deleted_at=project.last_deleted_at,
                      title=project.title, options=options,
                      actor=authorization_service.actor_snapshot(db, user_id),
                      image_versions=await thumbnails.versions(db, project.id),
                      ai_revision=project.ai_visibility_revision, planning_revision=project.planning_revision,
                      content_revisions=content_revisions,
                      serialization_state=await serialization_state(db, project.id))


def _value(v: Any) -> Any:
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_value)


_DROP = object()


def _normalized_key(value: str) -> str:
    """兼容大小写、下划线、驼峰及历史分隔符，但仅比较已知内部键。"""
    return "".join(char for char in value.casefold() if char.isalnum())


def _export_private_key(value: Any) -> bool:
    # 单书格式额外排除来源声明；普通字段和以 _ 开头的内部键不能混为一谈。
    return personal_projection.normalized_private_key(value) or (
        isinstance(value, str) and not value.startswith("_") and _normalized_key(value) == "modelsource"
    )


def _internal_field(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return personal_projection.normalized_private_path(value) or any(
        _export_private_key(".".join(parts[index:])) for index in range(len(parts))
    )


def _private_guards(value: Any) -> bool:
    return personal_projection.has_private_guard({"guards": value})


def _partial_private(value: str, *, in_preview: bool, hide_model: bool) -> bool:
    """截断 JSON 若已出现内部键或预览护栏，不能原样暴露后续片段。"""
    if personal_projection.partial_private(value, in_preview=in_preview):
        return True
    return any(
        _export_private_key(key) or (hide_model and personal_projection.normalized_model_metadata_key(key))
        for key in personal_projection._partial_keys(value)
    )


def _strip_log_internal_params(
    value: Any, depth: int = 0, *, in_preview: bool = False, hide_model: bool = False,
) -> Any:
    """保留普通日志内容；递归移除原实例私有身份、凭据及已知预览护栏。"""
    if depth > 64:
        return None
    if isinstance(value, dict):
        field = next((item for key, item in value.items()
                      if isinstance(key, str) and _normalized_key(key) == "field"), None)
        if _internal_field(field):
            return _DROP
        result = {}
        for key, item in value.items():
            normalized = _normalized_key(key) if isinstance(key, str) else None
            if isinstance(key, str) and (
                _export_private_key(key)
                or (not key.startswith("_") and normalized == "guards" and _private_guards(item))
            ):
                continue
            if hide_model and personal_projection.normalized_model_metadata_key(key):
                result[key] = None
                continue
            child_preview = in_preview or normalized == "preview"
            cleaned = _strip_log_internal_params(item, depth + 1, in_preview=child_preview, hide_model=hide_model)
            if cleaned is not _DROP:
                result[key] = cleaned
        return result
    if isinstance(value, list):
        return [cleaned for item in value
                if (cleaned := _strip_log_internal_params(
                    item, depth + 1, in_preview=in_preview, hide_model=hide_model,
                )) is not _DROP]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[", '"')):
        try:
            parsed = json.loads(value)
        except RecursionError:
            return None
        except (ValueError, TypeError):
            if _partial_private(value, in_preview=in_preview, hide_model=hide_model):
                return "[私有内容已隐藏]"
            return value
        if isinstance(parsed, (dict, list, str)):
            cleaned = _strip_log_internal_params(parsed, depth + 1, in_preview=in_preview, hide_model=hide_model)
            if cleaned is _DROP:
                return None
            if cleaned != parsed:
                return json.dumps(cleaned, ensure_ascii=False)
    return value


def _project_log(row: Any, data: dict[str, Any], owner_id: int) -> dict[str, Any]:
    # 使用可信列判断模型来源；params 中的同名字段只是用户内容，不能授权模型元数据。
    projected = personal_projection._project_log_sources(
        [data], [(row["model_source"], row["model_config_owner_id"])], user_id=owner_id, is_owner=True,
    )[0]
    # 这两列不属于单书格式，来源始终不得成为导入时的证明。
    projected.pop("model_source", None)
    projected.pop("ai_config_id", None)
    source, config_owner = row["model_source"], row["model_config_owner_id"]
    actor_model = source == "actor" and type(config_owner) is int and config_owner == owner_id
    legacy_author = source is None and config_owner is None
    for field, fallback in (("params", {}), ("messages", []), ("error", "")):
        cleaned = _strip_log_internal_params(projected[field], hide_model=not (actor_model or legacy_author))
        projected[field] = fallback if cleaned is _DROP or (field == "error" and cleaned is None) else cleaned
    return projected


def _row(rule: TableRule, row: Any, included: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in rule.columns:
        v = row[col]
        target = rule.fk.get(col)
        if target is not None and target != "projects" and target not in included:
            v = None
        out[col] = _value(v)
    return out


def _scope(rule: TableRule, plan: ExportPlan):
    cond = rule.scope(plan.project_id)
    if rule.model is InspirationSession:
        cond = cond & (InspirationSession.user_id == plan.owner_id)
    elif rule.model is GenerationLog:
        cond = cond & (GenerationLog.actor_user_id == plan.owner_id)
    return cond


async def _assert_export_active(db: AsyncSession, plan: ExportPlan) -> None:
    row = (await db.execute(select(Project.last_deleted_at).where(
        Project.id == plan.project_id, Project.user_id == plan.owner_id, *project_lifecycle.active_conditions()
    ))).one_or_none()
    if row is None:
        # 已发送的片段无法撤回，剩余文件必须中止，而不是生成貌似有效的空备份。
        raise NotFoundError("项目不存在，备份导出已中止")
    _assert_same_lifecycle(plan, row.last_deleted_at)


def _assert_same_lifecycle(plan: ExportPlan, current: datetime | None) -> None:
    original = plan.last_deleted_at
    if ((original is None) != (current is None)
            or (original is not None and current is not None
                and project_lifecycle.as_utc(original) != project_lifecycle.as_utc(current))):
        raise ConflictError("备份导出期间作品已回收或恢复，请重新导出",
                            extra={"code": "project_lifecycle_changed"})


async def _iter_rows(db: AsyncSession, rule: TableRule, plan: ExportPlan) -> AsyncIterator[Any]:
    table = rule.model.__table__
    base = select(table).where(_scope(rule, plan))
    if not rule.has_id and rule.table != "scene_documents":
        await _assert_export_active(db, plan)
        for row in (await db.execute(base.order_by(*table.primary_key.columns))).mappings():
            yield row
        return
    key = table.c.id if rule.has_id else table.c.scene_id
    rich_row = rule.table in {"scene_documents", "scene_sections", "revision_batch_scenes", "serialization_editions"}
    page_size = RICH_ROW_BATCH if rich_row else BATCH
    last = 0
    while True:
        await _assert_export_active(db, plan)
        rows = (await db.execute(base.where(key > last).order_by(key).limit(page_size))).mappings().all()
        casts: dict[int, list[int]] = {}
        if rule.table == "chapter_plans":
            chapter_ids = [row["chapter_id"] for row in rows if row["chapter_id"] is not None]
            if chapter_ids:
                for chapter_id, character_id in await db.execute(
                    select(ChapterCharacter.chapter_id, ChapterCharacter.character_id)
                    .where(ChapterCharacter.chapter_id.in_(chapter_ids)).order_by(ChapterCharacter.character_id)
                ):
                    casts.setdefault(chapter_id, []).append(character_id)
        for row in rows:
            if rule.table == "chapter_plans" and row["chapter_id"] is not None:
                yield {**row, "character_ids": casts.get(row["chapter_id"], [])}
            else:
                yield row
        if len(rows) < page_size:
            return
        last = rows[-1][key.name]


async def _styles(db: AsyncSession, project: Project) -> tuple[list[dict[str, Any]], dict[int, str]]:
    ids = set(
        await db.scalars(
            select(Chapter.style_id).where(Chapter.project_id == project.id, Chapter.style_id.is_not(None)).distinct()
        )
    )
    if project.default_style_id is not None:
        ids.add(project.default_style_id)
    if not ids:
        return [], {}
    styles = list(await db.scalars(select(WritingStyle).where(WritingStyle.id.in_(ids)).order_by(WritingStyle.id)))
    refs: dict[int, str] = {}
    items = []
    for i, s in enumerate(styles, start=1):
        refs[s.id] = f"s{i}"
        items.append({"ref": refs[s.id], "preset_key": s.preset_key, **{f: getattr(s, f) for f in STYLE_FIELDS}})
    return items, refs


async def stream_export(sessionmaker: async_sessionmaker[AsyncSession], plan: ExportPlan) -> AsyncIterator[bytes]:
    async for chunk in thumbnails.bounded_stream(_stream_export(sessionmaker, plan)):
        yield chunk


async def _stream_export(sessionmaker: async_sessionmaker[AsyncSession], plan: ExportPlan) -> AsyncIterator[bytes]:
    included = included_tables(plan.options)
    exported_log_ids: set[int] = set()
    async with sessionmaker() as db:
        project = await db.scalar(
            select(Project).where(Project.id == plan.project_id, *project_lifecycle.active_conditions())
        )
        if project is None:
            # 校验归属之后、开始写出之前项目被删除：输出一个无法导入的空文件
            yield b"{}"
            return
        styles, refs = await _styles(db, project)
        cover = await db.get(ProjectCover, project.id)
        from app.services.pen_name_service import project_pen_name

        pen_name = await project_pen_name(db, project.id)
        await authorization_service.require_authenticated(db, plan.actor)
        head = {
            "format": FORMAT,
            "version": VERSION,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "app_schema": APP_SCHEMA,
            "options": plan.options.model_dump(),
            "styles": styles,
            "pen_name": pen_name,
            "cover": {"mime_type": cover.mime_type, "data": base64.b64encode(cover.data).decode("ascii")}
            if cover is not None else None,
        }
        await _assert_export_active(db, plan)
        project_row = (await db.execute(select(Project.__table__).where(Project.id == project.id))).mappings().one()
        project_data = _row(PROJECT_RULE, project_row, included)
        project_data["default_style_ref"] = refs.get(project.default_style_id) if project.default_style_id else None
        yield (_dumps(head)[:-1] + ',"project":' + _dumps(project_data) + ',"thumbnails":[').encode("utf-8")
        first_image = True
        async for image in thumbnails.iter_serialized(db, plan.image_versions):
            await _assert_export_active(db, plan)
            yield (("" if first_image else ",") + _dumps(image)).encode("utf-8")
            first_image = False
        await _assert_export_active(db, plan)
        yield b'],"tables":{'
        for t_index, rule in enumerate(r for r in RULES if r.table in included):
            await _assert_export_active(db, plan)
            prefix = "," if t_index else ""
            yield f'{prefix}"{rule.table}":['.encode()
            first = True
            buffer: list[str] = []
            buffer_chars = 0
            async for row in _iter_rows(db, rule, plan):
                data = _row(rule, row, included)
                if rule.table == "generation_logs":
                    data = _project_log(row, data, plan.owner_id)
                    exported_log_ids.add(row["id"])
                elif rule.table == "chapter_versions" and data["generation_log_id"] not in exported_log_ids:
                    data["generation_log_id"] = None
                elif rule.table == "revision_batch_scenes":
                    cid = row["chapter_id"]
                    data["matches_revision"] = (cid in plan.content_revisions and row["scene_id"] is not None
                                                and row["applied_revision"] == plan.content_revisions[cid])
                if rule.table == "chapters":
                    data["style_ref"] = refs.get(row["style_id"]) if row["style_id"] is not None else None
                elif rule.table == "relationship_types":
                    data["global"] = row["project_id"] is None
                encoded = _dumps(data)
                buffer.append(encoded)
                buffer_chars += len(encoded)
                if len(buffer) >= BATCH or buffer_chars >= BUFFER_CHARS:
                    yield (("" if first else ",") + ",".join(buffer)).encode("utf-8")
                    first, buffer, buffer_chars = False, [], 0
            if buffer:
                yield (("" if first else ",") + ",".join(buffer)).encode("utf-8")
            yield b"]"
        # 新策略跨实体、字段、链接多表写入。不能结束一个混合新旧限制的可导入文件。
        await validate_export(db, plan)
        yield b"}}"


async def validate_export(db: AsyncSession, plan: ExportPlan, *, complete: bool = True) -> None:
    """发送前和最后的闭合符之前复验原身份、作品及快照。"""
    await authorization_service.require_authenticated(db, plan.actor)
    project = await get_owned_project(db, plan.owner_id, plan.project_id)
    _assert_same_lifecycle(plan, project.last_deleted_at)
    if not complete:
        return
    await setting_ai_visibility.check_visibility_revision(db, plan.project_id, plan.ai_revision)
    current_revisions = dict((await db.execute(select(Chapter.id, Chapter.content_revision)
        .where(Chapter.project_id == plan.project_id))).all())
    current_planning = await db.scalar(select(Project.planning_revision).where(Project.id == plan.project_id))
    if await thumbnails.versions(db, plan.project_id) != plan.image_versions:
        raise ConflictError("备份导出期间缩略图已变化，请重新导出", extra={"code": "thumbnail_changed"})
    if await serialization_state(db, plan.project_id) != plan.serialization_state:
        raise ConflictError("备份导出期间连载数据已变化，请重新导出", extra={"code": "serialization_changed"})
    if current_revisions != plan.content_revisions or current_planning != plan.planning_revision:
        raise ConflictError("备份导出期间章节或场景已变化，请重新导出", extra={"code": "scene_context_changed"})


# ---------------- 大小估算 ----------------


def _text_columns(rule: TableRule) -> list:
    return [
        c for c in rule.model.__table__.columns if isinstance(c.type, String | Text | JSON) and c.name in rule.columns
    ]


async def _measure(db: AsyncSession, rule: TableRule, plan: ExportPlan) -> tuple[int, int]:
    cols = _text_columns(rule)
    lengths = [func.coalesce(func.sum(func.length(cast(c, Text))), 0) for c in cols]
    row = (await db.execute(select(func.count(), *lengths).select_from(rule.model).where(_scope(rule, plan)))).one()
    return int(row[0] or 0), sum(int(v or 0) for v in row[1:])


async def estimate(db: AsyncSession, user_id: int, project_id: int) -> BackupEstimate:
    plan = await prepare_export(db, user_id, project_id, BackupOptions())
    groups: dict[str, EstimateGroup] = {
        k: EstimateGroup() for k in ("core", "versions", "snapshots", "generation_logs", "inspiration")
    }
    for rule in RULES:
        rows, chars = await _measure(db, rule, plan)
        g = groups[rule.option or "core"]
        g.rows += rows
        g.chars += chars
    for g in groups.values():
        g.bytes = g.chars * 3 + g.rows * 150
    cover_size = await db.scalar(select(func.length(ProjectCover.data)).where(ProjectCover.project_id == project_id))
    if cover_size is not None:
        groups["core"].bytes += 4 * ((cover_size + 2) // 3)
    image_versions = await thumbnails.versions(db, project_id)
    if image_versions:
        from app.models.setting_images import SettingImageResource

        ids = list(set(image_versions.values()))
        sizes = {}
        for offset in range(0, len(ids), 400):
            rows = await db.execute(select(SettingImageResource.id, SettingImageResource.byte_size)
                                    .where(SettingImageResource.id.in_(ids[offset:offset + 400])))
            sizes.update(dict(rows.all()))
        groups["core"].bytes += sum(4 * ((sizes.get(version, 0) + 2) // 3) for version in image_versions.values())
    return BackupEstimate(**groups)
