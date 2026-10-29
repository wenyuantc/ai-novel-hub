"""写作风格库：预设（user_id 为空，只读，可复制）+ 用户风格；解析优先级与风格段渲染；提取 / 试写（同步 SSE）。

- 可见性：预设或本人所有；他人的风格一律 404（不泄露存在性）。
- 解析优先级（resolve）：生成时临时指定 > 章节覆盖（chapters.style_id）> 项目默认（projects.default_style_id）
  > 不用风格。
- 删除被引用的风格：没有 force → 409 并附 references；force 后由外键 SET NULL，引用处回落到上一层。
- 风格段由 style.block 模板渲染（用户可覆盖）；向导与实体类提示词只用项目默认风格的一句话描述。
- 提取 / 试写照局部改写预览的模式：同步 SSE、不抢 generating 锁、不写生成记录（没有项目归属）。
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import build_style_block, build_style_extract_messages, build_style_trial_messages
from app.ai.prompts.style import BLOCK_FIELDS
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.ai.templating import PromptOverride
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.security import SecretCipher
from app.models import Chapter, Project, WritingStyle
from app.models.style_presets import PRESET_STYLES
from app.schemas.style import (
    MAX_IMPORT_STYLES,
    MAX_STYLE_IMPORT_BYTES,
    STYLES_FORMAT,
    STYLES_VERSION,
    StyleBrief,
    StyleChoice,
    StyleCreate,
    StyleDraft,
    StyleExtractRequest,
    StyleImportItem,
    StyleImportResult,
    StyleOut,
    StyleReferences,
    StyleTrialRequest,
    StyleUpdate,
)
from app.services import ai_config_service, prompt_service

logger = logging.getLogger(__name__)

# 除名称外可编辑的字段及其长度上限（与 writing_styles 列一致）
FIELD_LIMITS: dict[str, int] = {
    "description": 200,
    "pov": 500,
    "rhythm": 500,
    "dialogue": 500,
    "description_density": 500,
    "tone": 500,
    "diction": 500,
    "taboos": 1000,
    "sample": 2000,
    "extra": 5000,
}
NAME_LIMIT = 100
COPY_SUFFIX = "（副本）"
IMPORT_SUFFIX = "（导入）"
PRESET_READONLY = "预设风格不能修改或删除，可以复制后修改"


@dataclass(frozen=True)
class ResolvedStyle:
    id: int
    name: str
    description: str
    block: str
    # 解析来源：request（临时指定）/ chapter / project
    source: str = ""

    def as_log(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "block": self.block, "source": self.source}


def style_log(resolved: ResolvedStyle | None) -> dict[str, Any] | None:
    return resolved.as_log() if resolved is not None else None


# ---------------------------------------------------------------- 查询与可见性


def _visible(user_id: int):
    return or_(WritingStyle.user_id.is_(None), WritingStyle.user_id == user_id)


async def get_visible(db: AsyncSession, user_id: int, style_id: int) -> WritingStyle:
    style = await db.scalar(select(WritingStyle).where(WritingStyle.id == style_id, _visible(user_id)))
    if style is None:
        raise NotFoundError("写作风格不存在")
    return style


async def ensure_assignable(db: AsyncSession, user_id: int, style_id: int | None) -> None:
    """项目默认 / 章节覆盖只能指向可见的风格（请求体里的交叉引用 → 400）。"""
    if style_id is None:
        return
    if await db.scalar(select(WritingStyle.id).where(WritingStyle.id == style_id, _visible(user_id))) is None:
        raise BadRequestError("写作风格不存在或不可用")


async def _get_editable(db: AsyncSession, user_id: int, style_id: int) -> WritingStyle:
    style = await get_visible(db, user_id, style_id)
    if style.user_id is None:
        raise ForbiddenError(PRESET_READONLY)
    return style


async def references(db: AsyncSession, user_id: int, style_id: int) -> StyleReferences:
    """引用数只统计当前用户的项目与章节。"""
    projects = await db.scalar(
        select(func.count(Project.id)).where(Project.user_id == user_id, Project.default_style_id == style_id)
    )
    chapters = await db.scalar(
        select(func.count(Chapter.id))
        .join(Project, Project.id == Chapter.project_id)
        .where(Project.user_id == user_id, Chapter.style_id == style_id)
    )
    return StyleReferences(projects=int(projects or 0), chapters=int(chapters or 0))


def to_out(style: WritingStyle, refs: StyleReferences | None = None) -> StyleOut:
    out = StyleOut.model_validate(style)
    out.is_preset = style.user_id is None
    out.references = refs or StyleReferences()
    return out


def to_brief(style: WritingStyle) -> StyleBrief:
    return StyleBrief(id=style.id, name=style.name, description=style.description, is_preset=style.user_id is None)


async def list_styles(db: AsyncSession, user_id: int) -> list[StyleOut]:
    styles = (
        await db.scalars(
            select(WritingStyle)
            .where(_visible(user_id))
            # 预设在前（user_id 为空），其后为我的风格
            .order_by(WritingStyle.user_id.is_not(None), WritingStyle.sort, WritingStyle.id)
        )
    ).all()
    proj_counts = dict(
        (
            await db.execute(
                select(Project.default_style_id, func.count(Project.id))
                .where(Project.user_id == user_id, Project.default_style_id.is_not(None))
                .group_by(Project.default_style_id)
            )
        ).all()
    )
    chap_counts = dict(
        (
            await db.execute(
                select(Chapter.style_id, func.count(Chapter.id))
                .join(Project, Project.id == Chapter.project_id)
                .where(Project.user_id == user_id, Chapter.style_id.is_not(None))
                .group_by(Chapter.style_id)
            )
        ).all()
    )
    return [
        to_out(s, StyleReferences(projects=proj_counts.get(s.id, 0), chapters=chap_counts.get(s.id, 0))) for s in styles
    ]


async def get_style(db: AsyncSession, user_id: int, style_id: int) -> StyleOut:
    style = await get_visible(db, user_id, style_id)
    return to_out(style, await references(db, user_id, style_id))


# ---------------------------------------------------------------- 增删改


def clean_fields(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key == "name":
            out[key] = str(value).strip()[:NAME_LIMIT]
        elif key in FIELD_LIMITS:
            out[key] = str(value or "").strip()[: FIELD_LIMITS[key]]
        elif key == "sort":
            out[key] = int(value)
    return out


async def create_style(db: AsyncSession, user_id: int, data: StyleCreate) -> StyleOut:
    values = clean_fields(data.model_dump())
    if not values["name"]:
        raise BadRequestError("风格名称不能为空")
    style = WritingStyle(user_id=user_id, **values)
    db.add(style)
    await db.commit()
    await db.refresh(style)
    return to_out(style)


async def update_style(db: AsyncSession, user_id: int, style_id: int, data: StyleUpdate) -> StyleOut:
    style = await _get_editable(db, user_id, style_id)
    values = clean_fields({k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None})
    if "name" in values and not values["name"]:
        raise BadRequestError("风格名称不能为空")
    for key, value in values.items():
        setattr(style, key, value)
    await db.commit()
    await db.refresh(style)
    return to_out(style, await references(db, user_id, style_id))


async def delete_style(db: AsyncSession, user_id: int, style_id: int, *, force: bool = False) -> None:
    style = await _get_editable(db, user_id, style_id)
    refs = await references(db, user_id, style_id)
    if (refs.projects or refs.chapters) and not force:
        raise ConflictError(
            f"该风格被 {refs.projects} 个项目、{refs.chapters} 个章节引用，确认删除后引用处将回落到上一层",
            extra={"references": refs.model_dump()},
        )
    await db.delete(style)
    await db.commit()


async def _my_names(db: AsyncSession, user_id: int) -> set[str]:
    return set((await db.scalars(select(WritingStyle.name).where(WritingStyle.user_id == user_id))).all())


def unique_name(name: str, taken: set[str], suffix: str) -> str:
    if name not in taken:
        return name
    n = 1
    while True:
        tail = suffix if n == 1 else f"{suffix[:-1]}{n}{suffix[-1]}"
        candidate = name[: NAME_LIMIT - len(tail)] + tail
        if candidate not in taken:
            return candidate
        n += 1


def _fields(style: WritingStyle) -> dict[str, Any]:
    return {f: getattr(style, f) for f in ("name", *FIELD_LIMITS)}


async def copy_style(db: AsyncSession, user_id: int, style_id: int) -> StyleOut:
    src = await get_visible(db, user_id, style_id)
    values = _fields(src)
    taken = await _my_names(db, user_id)
    # 「名称（副本）」已被占用时依次尝试「名称（副本2）」……
    values["name"] = unique_name(src.name, taken | {src.name}, COPY_SUFFIX)
    style = WritingStyle(user_id=user_id, **values)
    db.add(style)
    await db.commit()
    await db.refresh(style)
    return to_out(style)


# ---------------------------------------------------------------- 导入导出


async def export_styles(db: AsyncSession, user_id: int, ids: list[int] | None) -> dict[str, Any]:
    stmt = select(WritingStyle).where(_visible(user_id))
    if ids:
        stmt = stmt.where(WritingStyle.id.in_(ids))
    else:
        stmt = stmt.where(WritingStyle.user_id == user_id)
    styles = (
        await db.scalars(stmt.order_by(WritingStyle.user_id.is_not(None), WritingStyle.sort, WritingStyle.id))
    ).all()
    return {"format": STYLES_FORMAT, "version": STYLES_VERSION, "items": [_fields(s) for s in styles]}


async def import_styles(db: AsyncSession, user_id: int, data: Any) -> StyleImportResult:
    size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > MAX_STYLE_IMPORT_BYTES:
        raise BadRequestError("风格文件过大，上限为 1MB")
    if not isinstance(data, dict) or data.get("format") != STYLES_FORMAT:
        raise BadRequestError("风格文件格式不正确：不是 ainovel.styles 导出文件")
    if data.get("version") != STYLES_VERSION:
        raise BadRequestError(f"不支持的风格文件版本：{data.get('version')}（当前支持 {STYLES_VERSION}）")
    items = data.get("items")
    if not isinstance(items, list):
        raise BadRequestError("风格文件格式不正确：items 必须是数组")
    if len(items) > MAX_IMPORT_STYLES:
        raise BadRequestError(f"单次最多导入 {MAX_IMPORT_STYLES} 个风格")
    taken = await _my_names(db, user_id)
    created: list[tuple[WritingStyle, str | None]] = []
    warnings: list[str] = []
    for i, raw in enumerate(items, start=1):
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            warnings.append(f"第 {i} 项缺少名称，已跳过")
            continue
        values = clean_fields({k: raw.get(k, "") for k in ("name", *FIELD_LIMITS) if isinstance(raw.get(k, ""), str)})
        original = values["name"]
        values["name"] = unique_name(original, taken, IMPORT_SUFFIX)
        taken.add(values["name"])
        style = WritingStyle(user_id=user_id, **values)
        db.add(style)
        created.append((style, original if values["name"] != original else None))
    await db.flush()
    await db.commit()
    return StyleImportResult(
        items=[StyleImportItem(id=s.id, name=s.name, renamed_from=renamed) for s, renamed in created],
        warnings=warnings,
    )


# ---------------------------------------------------------------- 预设同步


async def sync_presets(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """启动时按 preset_key upsert 预设（代码里改了预设，库里跟着更新）；失败只记日志，不阻止启动。"""
    try:
        async with sessionmaker() as db:
            existing = {
                s.preset_key: s
                for s in (await db.scalars(select(WritingStyle).where(WritingStyle.preset_key.is_not(None)))).all()
            }
            for i, preset in enumerate(PRESET_STYLES, start=1):
                row = existing.get(preset["preset_key"])
                if row is None:
                    db.add(WritingStyle(user_id=None, sort=i, **preset))
                    continue
                for key, value in preset.items():
                    if getattr(row, key) != value:
                        setattr(row, key, value)
                row.user_id, row.sort = None, i
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("同步预设写作风格失败")


# ---------------------------------------------------------------- 解析与渲染


async def render_block(db: AsyncSession, user_id: int, fields: dict[str, Any]) -> str:
    override = await prompt_service.get_override(db, user_id, "style.block")
    return build_style_block(fields, override)


async def _resolved(db: AsyncSession, user_id: int, style: WritingStyle, source: str) -> ResolvedStyle:
    return ResolvedStyle(
        id=style.id,
        name=style.name,
        description=style.description,
        block=await render_block(db, user_id, _fields(style)),
        source=source,
    )


async def resolve(
    db: AsyncSession, user_id: int, project: Project, chapter: Chapter | None, choice: StyleChoice = "inherit"
) -> ResolvedStyle | None:
    """临时指定 > 章节覆盖 > 项目默认 > 不用风格。临时指定的风格必须可见（否则 400）。"""
    if choice == "none":
        return None
    if isinstance(choice, int) and not isinstance(choice, bool):
        style = await db.scalar(select(WritingStyle).where(WritingStyle.id == choice, _visible(user_id)))
        if style is None:
            raise BadRequestError("指定的写作风格不存在或不可用")
        return await _resolved(db, user_id, style, "request")
    for style_id, source in ((chapter.style_id if chapter else None, "chapter"), (project.default_style_id, "project")):
        if style_id is None:
            continue
        style = await db.scalar(select(WritingStyle).where(WritingStyle.id == style_id, _visible(user_id)))
        if style is not None:
            return await _resolved(db, user_id, style, source)
    return None


async def validate_choice(db: AsyncSession, user_id: int, choice: StyleChoice) -> None:
    """创建任务时同步校验临时指定的风格（运行时会再按最新数据解析一次）。"""
    if isinstance(choice, int) and not isinstance(choice, bool):
        if await db.scalar(select(WritingStyle.id).where(WritingStyle.id == choice, _visible(user_id))) is None:
            raise BadRequestError("指定的写作风格不存在或不可用")


async def default_description(db: AsyncSession, project: Project) -> str:
    """项目默认风格的一句话描述（向导与实体类提示词的「写作风格：」行）。"""
    if project.default_style_id is None:
        return ""
    desc = await db.scalar(
        select(WritingStyle.description).where(WritingStyle.id == project.default_style_id, _visible(project.user_id))
    )
    return (desc or "").strip()


# ---------------------------------------------------------------- 项目兼容：旧的 style 自由文本


def legacy_style_name(title: str) -> str:
    return f"《{title}》原风格"[:NAME_LIMIT]


async def apply_legacy_style(db: AsyncSession, project: Project, text: str) -> str:
    """ProjectCreate / ProjectUpdate 仍接受 style 自由文本（兼容一个版本）：为项目创建或更新「原风格」并设为默认。

    不 commit；返回给前端的提示。空文本 = 当前默认若是原风格则取消默认（风格本身保留）。
    """
    text = text.strip()
    current = None
    if project.default_style_id is not None:
        current = await db.scalar(
            select(WritingStyle).where(
                WritingStyle.id == project.default_style_id, WritingStyle.user_id == project.user_id
            )
        )
    is_legacy = current is not None and current.name == legacy_style_name(project.title)
    if not text:
        if is_legacy:
            project.default_style_id = None
            return "style 字段已废弃：已取消项目默认风格，请改用 default_style_id"
        return "style 字段已废弃，请改用 default_style_id"
    if is_legacy and current is not None:
        current.description, current.extra = text[:100], text[:5000]
        return "style 字段已废弃：已更新项目的「原风格」，请改用写作风格库与 default_style_id"
    style = WritingStyle(
        user_id=project.user_id,
        name=unique_name(legacy_style_name(project.title), await _my_names(db, project.user_id), "（2）"),
        description=text[:100],
        extra=text[:5000],
    )
    db.add(style)
    await db.flush()
    project.default_style_id = style.id
    return "style 字段已废弃：已为项目创建「原风格」并设为默认，请改用写作风格库与 default_style_id"


# ---------------------------------------------------------------- 提取 / 试写（同步 SSE）


@dataclass
class StyleAIJob:
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None


async def _provider(db, user_id, ai_config_id, cipher, factory, ai_timeout, allow_private):
    cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    return cfg, provider


async def prepare_extract(
    db: AsyncSession,
    *,
    user_id: int,
    req: StyleExtractRequest,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> StyleAIJob:
    if len(req.sample_text.strip()) < 200:
        raise BadRequestError("样文至少需要 200 字")
    cfg, provider = await _provider(db, user_id, req.ai_config_id, cipher, factory, ai_timeout, allow_private)
    override = await prompt_service.get_override(db, user_id, "style.extract")
    return StyleAIJob(
        provider=provider,
        model=cfg.model,
        messages=build_style_extract_messages(sample_text=req.sample_text, override=override),
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
    )


async def prepare_trial(
    db: AsyncSession,
    *,
    user_id: int,
    req: StyleTrialRequest,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> StyleAIJob:
    if req.style_id is not None:
        fields = _fields(await get_visible(db, user_id, req.style_id))
    elif req.style is not None:
        fields = req.style.model_dump()
    else:
        raise BadRequestError("请指定风格或填写风格字段")
    block = await render_block(db, user_id, fields)
    if not block:
        raise BadRequestError("风格内容为空，无法试写")
    cfg, provider = await _provider(db, user_id, req.ai_config_id, cipher, factory, ai_timeout, allow_private)
    override: PromptOverride | None = await prompt_service.get_override(db, user_id, "style.trial")
    return StyleAIJob(
        provider=provider,
        model=cfg.model,
        messages=build_style_trial_messages(style=block, scene=req.scene, words=req.words, override=override),
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
    )


def normalize_draft(obj: Any) -> tuple[dict[str, str], list[str]]:
    """把模型输出规范化为风格草稿（字段截断到列长度）；非对象 → JSONExtractError。"""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        obj = obj[0]
    if not isinstance(obj, dict):
        raise JSONExtractError("输出不是 JSON 对象")
    warnings: list[str] = []
    draft = {k: str(obj.get(k) or "").strip() for k in BLOCK_FIELDS if k != "sample"}
    for key, limit in (("name", NAME_LIMIT), *FIELD_LIMITS.items()):
        if key in draft and len(draft[key]) > limit:
            draft[key] = draft[key][:limit]
            warnings.append(f"{key} 超出长度上限，已截断")
    if not draft.get("name"):
        draft["name"] = "提取的风格"
    draft["sample"] = ""
    return StyleDraft(**draft).model_dump(), warnings


async def _stream(job: StyleAIJob, pieces: list[str]) -> AsyncIterator[tuple[str, dict]]:
    stream_iter = job.provider.generate_stream(
        job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking
    )
    async with aclosing(stream_iter) as stream:
        async for piece in stream:
            pieces.append(piece)
            yield "delta", {"text": piece}


async def run_extract(job: StyleAIJob) -> AsyncIterator[tuple[str, dict]]:
    """流式输出模型原文；done 带解析出的风格草稿（不落库）。"""
    pieces: list[str] = []
    try:
        async with aclosing(_stream(job, pieces)) as events:
            async for event in events:
                yield event
        text = "".join(pieces).strip()
        if not text:
            raise AIProviderError("模型未返回任何内容")
        try:
            draft, warnings = normalize_draft(extract_json(text))
        except JSONExtractError as exc:
            yield "error", {"detail": f"AI 输出解析失败：{exc}，请重新提取"}
            return
        yield "done", {"draft": draft, "warnings": warnings}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except Exception:  # noqa: BLE001
        logger.exception("提取写作风格失败")
        yield "error", {"detail": "提取失败：服务器内部错误"}


async def run_trial(job: StyleAIJob) -> AsyncIterator[tuple[str, dict]]:
    """流式输出试写正文；不写入任何章节、不写生成记录。"""
    pieces: list[str] = []
    try:
        async with aclosing(_stream(job, pieces)) as events:
            async for event in events:
                yield event
        text = "".join(pieces).strip()
        if not text:
            raise AIProviderError("模型未返回任何内容")
        yield "done", {"text": text}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except Exception:  # noqa: BLE001
        logger.exception("风格试写失败")
        yield "error", {"detail": "试写失败：服务器内部错误"}
