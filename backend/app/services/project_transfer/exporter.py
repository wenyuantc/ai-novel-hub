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

from app.core.errors import NotFoundError
from app.models import Chapter, ChapterCharacter, InspirationSession, Project, ProjectCover, WritingStyle
from app.schemas.transfer import BackupEstimate, BackupOptions, EstimateGroup
from app.services import project_lifecycle, setting_ai_visibility
from app.services.project_service import get_owned_project
from app.services.project_transfer.registry import PROJECT_RULE, RULES, TableRule
from app.services.style_service import FIELD_LIMITS

FORMAT = "ainovel.project"
VERSION = 6
# 导出时的数据库迁移版本，仅供参考（新增迁移时同步更新）
APP_SCHEMA = "0037"
BATCH = 500
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
STYLE_FIELDS = ("name", *FIELD_LIMITS)


@dataclass(frozen=True)
class ExportPlan:
    project_id: int
    owner_id: int
    title: str
    options: BackupOptions

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
    return ExportPlan(project_id=project.id, owner_id=project.user_id, title=project.title, options=options)


def _value(v: Any) -> Any:
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_value)


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
    return cond


async def _assert_export_active(db: AsyncSession, plan: ExportPlan) -> None:
    if await db.scalar(select(Project.id).where(
        Project.id == plan.project_id, *project_lifecycle.active_conditions()
    )) is None:
        # 已发送的片段无法撤回，剩余文件必须中止，而不是生成貌似有效的空备份。
        raise NotFoundError("项目不存在，备份导出已中止")


async def _iter_rows(db: AsyncSession, rule: TableRule, plan: ExportPlan) -> AsyncIterator[Any]:
    table = rule.model.__table__
    base = select(table).where(_scope(rule, plan))
    if not rule.has_id:
        await _assert_export_active(db, plan)
        for row in (await db.execute(base.order_by(*table.primary_key.columns))).mappings():
            yield row
        return
    last = 0
    while True:
        await _assert_export_active(db, plan)
        rows = (await db.execute(base.where(table.c.id > last).order_by(table.c.id).limit(BATCH))).mappings().all()
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
        if len(rows) < BATCH:
            return
        last = rows[-1]["id"]


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
    included = included_tables(plan.options)
    async with sessionmaker() as db:
        project = await db.scalar(
            select(Project).where(Project.id == plan.project_id, *project_lifecycle.active_conditions())
        )
        if project is None:
            # 校验归属之后、开始写出之前项目被删除：输出一个无法导入的空文件
            yield b"{}"
            return
        ai_revision = project.ai_visibility_revision
        styles, refs = await _styles(db, project)
        cover = await db.get(ProjectCover, project.id)
        head = {
            "format": FORMAT,
            "version": VERSION,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "app_schema": APP_SCHEMA,
            "options": plan.options.model_dump(),
            "styles": styles,
            "cover": {"mime_type": cover.mime_type, "data": base64.b64encode(cover.data).decode("ascii")}
            if cover is not None else None,
        }
        await _assert_export_active(db, plan)
        project_row = (await db.execute(select(Project.__table__).where(Project.id == project.id))).mappings().one()
        project_data = _row(PROJECT_RULE, project_row, included)
        project_data["default_style_ref"] = refs.get(project.default_style_id) if project.default_style_id else None
        yield (_dumps(head)[:-1] + ',"project":' + _dumps(project_data) + ',"tables":{').encode("utf-8")
        for t_index, rule in enumerate(r for r in RULES if r.table in included):
            await _assert_export_active(db, plan)
            prefix = "," if t_index else ""
            yield f'{prefix}"{rule.table}":['.encode()
            first = True
            buffer: list[str] = []
            async for row in _iter_rows(db, rule, plan):
                data = _row(rule, row, included)
                if rule.table == "chapters":
                    data["style_ref"] = refs.get(row["style_id"]) if row["style_id"] is not None else None
                elif rule.table == "relationship_types":
                    data["global"] = row["project_id"] is None
                buffer.append(_dumps(data))
                if len(buffer) >= BATCH:
                    yield (("" if first else ",") + ",".join(buffer)).encode("utf-8")
                    first, buffer = False, []
            if buffer:
                yield (("" if first else ",") + ",".join(buffer)).encode("utf-8")
            yield b"]"
        # 新策略跨实体、字段、链接多表写入。不能结束一个混合新旧限制的可导入文件。
        await setting_ai_visibility.check_visibility_revision(db, plan.project_id, ai_revision)
        yield b"}}"


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
    return BackupEstimate(**groups)
