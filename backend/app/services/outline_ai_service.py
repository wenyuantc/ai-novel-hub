"""大纲续写 / 展开（后台任务 outline.continue / outline.expand）。只产出草稿，不写库，也不写生成记录。

- outline.continue（resource_key = outline:{pid}）：参考世界观、最近 10 条章纲、最近章节的分析摘要、
  未回收的伏笔与作者方向，续写 count 条章纲。草稿的卷号 / 卷名 / 章节号由服务端计算（接在最后一卷后，或开新卷），采纳时
  `POST /outlines/append` 会按当时的末尾重新顺延章节号。
- outline.expand（resource_key = expand:{oid}）：把一条章纲展开为 count 章细纲（count 含已有正文、需保留的细纲）。
  参考前后各 2 条章纲、角色名单、保留细纲；新章数 > 5 时分批生成，每批附上已生成细纲的关键事件；
  跨章重复的关键事件在规范化时去掉并给出 warning。结果附当前已有细纲及其状态（has_content = 保留）。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import ProjectInfo, build_outline_continue_messages, build_outline_expand_messages
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.ai.templating import PromptOverride
from app.core.errors import BadRequestError
from app.core.security import SecretCipher
from app.models import Chapter, Foreshadow, Outline
from app.schemas.outline import (
    DEFAULT_EST_WORDS,
    MAX_EST_WORDS,
    MIN_EST_WORDS,
    OutlineContinueRequest,
    OutlineExpandRequest,
    clean_key_events,
)
from app.services import (
    ai_config_service,
    foreshadow_service,
    kb_service,
    memory_service,
    outline_service,
    plan_service,
    prompt_service,
    retrieval_service,
    story_service,
)
from app.services.context_builder import latest_summaries
from app.services.entity_ai_service import resolve_name
from app.services.generation_service import Event
from app.services.project_service import get_owned_project, project_info

logger = logging.getLogger(__name__)

OutlineKind = Literal["outline.continue", "outline.expand"]
RECENT_OUTLINES = 10
RECENT_SUMMARIES = 10
FORESHADOW_LIMIT = 20
EXPAND_BATCH = 5
NEIGHBORS = 2
REFERENCE_WORLD_CHARS = 4000
REFERENCE_TEXT_CHARS = 4000
REFERENCE_ROSTER_CHARS = 3000
MAX_KEY_EVENTS_PER_PLAN = 10


@dataclass
class OutlineJob:
    kind: OutlineKind
    project_id: int
    provider: LLMProvider
    model: str
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None
    # outline.continue：一次调用
    messages: list[ChatMessage] = field(default_factory=list)
    count: int = 0
    new_volume: dict[str, Any] | None = None
    # outline.expand：分批生成所需的素材
    outline_id: int | None = None
    total: int = 0
    info: ProjectInfo | None = None
    prompt_parts: dict[str, str] = field(default_factory=dict)
    # 组装 job 时取出的用户覆盖（任务运行中途修改模板不影响本次任务）
    override: PromptOverride | None = None


# ---------------- 规范化（纯函数） ----------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "；".join(_text(v) for v in value if _text(v))
    if isinstance(value, dict):
        return "；".join(f"{k}：{_text(v)}" for k, v in value.items() if _text(v))
    return str(value).strip()


def _list(obj: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in keys:
            if isinstance(obj.get(k), list):
                return obj[k]
        lists = [v for v in obj.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    raise JSONExtractError("JSON 结构不符合预期")


def normalize_continue(obj: Any) -> tuple[list[dict[str, str]], str]:
    """续写输出 → ([{title, summary}], 模型给的卷名)。兼容按卷嵌套（取各卷 chapters 依次拼接）。"""
    items = _list(obj, ("outlines", "chapters", "items"))
    volume_title = _text(obj.get("volume_title")) if isinstance(obj, dict) else ""
    if items and all(isinstance(v, dict) and isinstance(v.get("chapters"), list) for v in items):
        volume_title = volume_title or _text(items[0].get("volume_title") or items[0].get("title"))
        items = [ch for v in items for ch in v["chapters"]]
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = _text(it.get("title") or it.get("name"))[:200]
        if title:
            rows.append({"title": title, "summary": _text(it.get("summary") or it.get("content"))})
    return rows, volume_title[:200]


def _events(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [v for v in value.replace("；", "\n").replace(";", "\n").split("\n")]
    if not isinstance(value, list):
        return []
    return clean_key_events([_text(v) for v in value])[:MAX_KEY_EVENTS_PER_PLAN]


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.replace("，", ",").replace("、", ",").split(",")
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        name = _text(v.get("name") if isinstance(v, dict) else v)[:100]
        if name and name not in out:
            out.append(name)
    return out


def _est_words(value: Any) -> int:
    try:
        return max(MIN_EST_WORDS, min(MAX_EST_WORDS, int(float(value))))
    except (TypeError, ValueError):
        return DEFAULT_EST_WORDS


def normalize_expand(obj: Any) -> list[dict[str, Any]]:
    items = _list(obj, ("chapters", "plans", "items"))
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = _text(it.get("title") or it.get("name"))[:200]
        if not title:
            continue
        rows.append(
            {
                "title": title,
                "summary": _text(it.get("summary") or it.get("content")),
                "key_events": _events(it.get("key_events") or it.get("events")),
                "characters": _names(it.get("characters") or it.get("character_names")),
                "emotion": _text(it.get("emotion") or it.get("tone"))[:100],
                "goal": _text(it.get("goal") or it.get("purpose")),
                "est_words": _est_words(it.get("est_words") or it.get("words")),
            }
        )
    return rows


def dedupe_key_events(drafts: list[dict[str, Any]], seen: set[str]) -> list[str]:
    """跨章去掉重复的关键事件（忽略空白差异）；seen 会被更新。返回警告。"""
    warnings = []
    for d in drafts:
        kept = []
        for e in d["key_events"]:
            key = "".join(e.split())
            if key in seen:
                warnings.append(f"「{d['title']}」的关键事件「{e}」与其他章重复，已去掉")
                continue
            seen.add(key)
            kept.append(e)
        d["key_events"] = kept
    return warnings


# ---------------- 校验 / 组装 ----------------


async def _kept_plans(db: AsyncSession, project_id: int, outline_id: int) -> list[Any]:
    plans = await plan_service.plan_outs(
        db, project_id, (await plan_service.plans_of(db, [outline_id])).get(outline_id, [])
    )
    return [p for p in plans if p.has_content]


def _check_total(total: int, kept: int) -> None:
    if total <= kept:
        raise BadRequestError(f"展开章数需大于已有正文的细纲数（{kept} 章）")


async def validate_outline_task(
    db: AsyncSession,
    *,
    user_id: int,
    kind: OutlineKind,
    project_id: int,
    params: dict[str, Any],
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> tuple[int, str, str]:
    """创建任务时同步校验；返回 (ai_config_id, 标题, resource_key)。不修改任何数据。"""
    await get_owned_project(db, user_id, project_id, lock=True)
    if kind == "outline.expand":
        req = OutlineExpandRequest.model_validate(params)
        outline_id = int(params["outline_id"])
        outline = await plan_service.get_project_outline(db, user_id, project_id, outline_id)
        _check_total(req.count, len(await _kept_plans(db, project_id, outline_id)))
        title = f"展开大纲 · 第{outline.chapter_no}章 {outline.title}"
        resource_key = f"expand:{outline_id}"
        ai_config_id = req.ai_config_id
    else:
        ai_config_id = OutlineContinueRequest.model_validate(params).ai_config_id
        title = "AI 续写大纲"
        resource_key = f"outline:{project_id}"
    cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    return cfg.id, title[:200], resource_key


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "……"


async def _story_so_far(db: AsyncSession, project_id: int) -> tuple[str, set[int]]:
    """最近 10 章（有分析摘要的）的摘要，以及这些章节的章节号（召回旧情节时不再重复它们的摘要）。"""
    rows = (
        await db.execute(
            select(Chapter.id, Chapter.chapter_no, Chapter.title)
            .where(Chapter.project_id == project_id)
            .order_by(Chapter.chapter_no.desc(), Chapter.id.desc())
        )
    ).all()
    summaries = await latest_summaries(db, project_id, [r[0] for r in rows])
    picked = [(no, title, summaries[cid]) for cid, no, title in rows if cid in summaries][:RECENT_SUMMARIES]
    text = "\n".join(f"第{no}章 {title}：{' '.join(s.split())}" for no, title, s in reversed(picked))
    return text, {no for no, _, _ in picked}


async def _open_foreshadows(db: AsyncSession, project_id: int, next_no: int) -> str:
    """超期 → 长线 → 其余已埋未回收的伏笔，最多 20 条。"""
    rem = await foreshadow_service.reminders(db, project_id, next_no)
    picked: list[Foreshadow] = []
    for f in [*rem.overdue, *rem.long_term]:
        if f not in picked:
            picked.append(f)
    others = await db.scalars(
        select(Foreshadow)
        .where(Foreshadow.project_id == project_id, Foreshadow.status.in_(foreshadow_service.OPEN_STATUSES))
        .order_by(Foreshadow.importance.desc(), Foreshadow.id)
    )
    for f in others:
        if f.id not in {p.id for p in picked}:
            picked.append(f)
    lines = []
    for f in picked[:FORESHADOW_LIMIT]:
        when = []
        if f.actual_plant_chapter:
            when.append(f"第{f.actual_plant_chapter}章埋下")
        when.append(f"计划第{f.planned_resolve_chapter}章回收" if f.planned_resolve_chapter else "长线")
        content = " ".join((f.content or "").split())[:60]
        lines.append(f"- {f.title}（{'，'.join(when)}）" + (f"：{content}" if content else ""))
    return "\n".join(lines)


def _outline_line(o: Any) -> str:
    return f"第{o.chapter_no}章 {o.title}：{' '.join(o.summary.split())}"


def _kept_line(p: Any) -> str:
    line = f"第{p.seq}节《{p.title}》：{' '.join(p.summary.split())}"
    if p.key_events:
        line += f"；关键事件：{'、'.join(p.key_events)}"
    return line


async def build_outline_job(
    db: AsyncSession,
    *,
    user_id: int,
    kind: OutlineKind,
    project_id: int,
    params: dict[str, Any],
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> OutlineJob:
    project = await get_owned_project(db, user_id, project_id)
    cfg = await ai_config_service.resolve_config(db, user_id, params.get("ai_config_id"))
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    info = await project_info(db, project)
    outlines = await outline_service.list_outlines(db, project_id)
    job = OutlineJob(
        kind=kind,
        project_id=project_id,
        provider=provider,
        model=cfg.model,
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
        override=await prompt_service.get_override(db, user_id, kind),
    )
    if kind == "outline.continue":
        req = OutlineContinueRequest.model_validate(params)
        next_no = await outline_service.next_chapter_no(db, project_id)
        last_no, last_title = await outline_service.last_volume(db, project_id)
        if req.new_volume is not None:
            title = req.new_volume.title.strip()
            volume_hint = f"这些章节属于新的一卷：第 {last_no + 1} 卷" + (
                f"「{title}」。" if title else "，请在 volume_title 中给出卷名。"
            )
        elif last_no:
            volume_hint = f"这些章节接在当前第 {last_no} 卷" + (f"「{last_title}」" if last_title else "") + "之后。"
        else:
            volume_hint = "这是全书的第 1 卷。"
        world = await story_service.list_items(db, story_service.WORLD, project_id)
        story_so_far, summarized = await _story_so_far(db, project_id)
        # 检索：查询 = 作者给的方向 + 最近的章纲标题；旧情节只召回续写起点之前的
        query = "\n".join([req.direction, *(o.title for o in outlines[-RECENT_OUTLINES:])])
        knowledge, memories = await _retrieval(
            db, project, next_no, query, summarized, cipher, factory, ai_timeout, allow_private, "大纲续写"
        )
        job.messages = build_outline_continue_messages(
            info,
            count=req.count,
            next_chapter_no=next_no,
            volume_hint=volume_hint,
            world_text=_clip(story_service.format_world(world), REFERENCE_WORLD_CHARS),
            recent_outlines_text=_clip(
                story_service.format_outlines(outlines[-RECENT_OUTLINES:]), REFERENCE_TEXT_CHARS
            ),
            story_so_far_text=_clip(story_so_far, REFERENCE_TEXT_CHARS),
            foreshadows_text=await _open_foreshadows(db, project_id, next_no),
            direction=req.direction,
            knowledge=knowledge,
            memories=memories,
            override=job.override,
        )
        job.count = req.count
        job.new_volume = req.new_volume.model_dump() if req.new_volume is not None else None
        return job

    req_e = OutlineExpandRequest.model_validate(params)
    outline_id = int(params["outline_id"])
    outline = await plan_service.get_project_outline(db, user_id, project_id, outline_id)
    kept = await _kept_plans(db, project_id, outline_id)
    _check_total(req_e.count, len(kept))
    idx = next(i for i, o in enumerate(outlines) if o.id == outline.id)
    before = outlines[max(0, idx - NEIGHBORS) : idx]
    after = outlines[idx + 1 : idx + 1 + NEIGHBORS]
    neighbors = [f"（前）{_outline_line(o)}" for o in before] + [f"（后）{_outline_line(o)}" for o in after]
    characters = await story_service.list_items(db, story_service.CHARACTERS, project_id)
    # 检索：查询 = 要展开的章纲；旧情节只召回这条章纲之前的
    knowledge, memories = await _retrieval(
        db,
        project,
        outline.chapter_no,
        "\n".join([outline.title, outline.summary, req_e.instruction]),
        set(),
        cipher,
        factory,
        ai_timeout,
        allow_private,
        "大纲展开",
    )
    job.outline_id = outline_id
    job.total = req_e.count
    job.count = req_e.count - len(kept)
    job.info = info
    job.prompt_parts = {
        "outline_text": _outline_line(outline)
        + (f"（第{outline.volume_no}卷 {outline.volume_title}）" if outline.volume_title else ""),
        "neighbors_text": "\n".join(neighbors),
        "roster_text": _clip(story_service.format_roster(characters), REFERENCE_ROSTER_CHARS),
        "kept_text": "\n".join(_kept_line(p) for p in kept),
        "instruction": req_e.instruction,
        "knowledge": knowledge,
        "memories": memories,
    }
    return job


async def _retrieval(
    db: AsyncSession,
    project: Any,
    chapter_no: int,
    query: str,
    summarized: set[int],
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    purpose: str,
) -> tuple[str, str]:
    """(参考资料正文, 相关旧情节)；检索出错或没有内容时为空串。"""
    embedder = await retrieval_service.get_embedder(
        db, project.user_id, cipher=cipher, factory=factory, ai_timeout=ai_timeout, allow_private=allow_private
    )
    knowledge = await kb_service.knowledge_text(db, project, query, embedder=embedder, purpose=purpose)
    recalled = await memory_service.recall(
        db, project, chapter_no, query, embedder=embedder, summary_chapter_nos=summarized
    )
    return knowledge, memory_service.format_lines(recalled)


def expand_messages(job: OutlineJob, count: int, previous_events: list[str]) -> list[ChatMessage]:
    assert job.info is not None
    return build_outline_expand_messages(
        job.info,
        outline_text=job.prompt_parts["outline_text"],
        neighbors_text=job.prompt_parts["neighbors_text"],
        roster_text=job.prompt_parts["roster_text"],
        kept_text=job.prompt_parts["kept_text"],
        previous_events_text="\n".join(f"- {e}" for e in previous_events),
        count=count,
        total=job.total,
        instruction=job.prompt_parts["instruction"],
        knowledge=job.prompt_parts.get("knowledge", ""),
        memories=job.prompt_parts.get("memories", ""),
        override=job.override,
    )


# ---------------- 结果 ----------------


async def continue_result(db: AsyncSession, job: OutlineJob, rows: list[dict[str, str]], volume_title: str) -> dict:
    warnings: list[str] = []
    if not rows:
        raise JSONExtractError("未解析到任何章纲")
    if len(rows) > job.count:
        warnings.append(f"模型返回了 {len(rows)} 章，只保留前 {job.count} 章")
        rows = rows[: job.count]
    elif len(rows) < job.count:
        warnings.append(f"模型只返回了 {len(rows)} 章（要求 {job.count} 章）")
    next_no = await outline_service.next_chapter_no(db, job.project_id)
    last_no, last_title = await outline_service.last_volume(db, job.project_id)
    if job.new_volume is not None:
        volume_no = last_no + 1
        volume_title = (str(job.new_volume.get("title") or "").strip() or volume_title)[:200]
    else:
        volume_no, volume_title = max(last_no, 1), last_title
    drafts = [
        {
            "volume_no": volume_no,
            "volume_title": volume_title,
            "chapter_no": next_no + i,
            "title": r["title"],
            "summary": r["summary"],
        }
        for i, r in enumerate(rows)
    ]
    return {"drafts": drafts, "warnings": warnings}


async def expand_result(db: AsyncSession, job: OutlineJob, drafts: list[dict[str, Any]], warnings: list[str]) -> dict:
    assert job.outline_id is not None
    if not drafts:
        raise JSONExtractError("未解析到任何细纲")
    outline_ok = await db.scalar(
        select(Outline.id).where(Outline.id == job.outline_id, Outline.project_id == job.project_id)
    )
    if outline_ok is None:
        raise BadRequestError("大纲已被删除，展开结果未生成")
    characters = await story_service.list_items(db, story_service.CHARACTERS, job.project_id)
    for d in drafts:
        resolved = []
        for name in d.pop("characters"):
            cid, _ = resolve_name(name, characters)
            if cid is not None and any(r["character_id"] == cid for r in resolved):
                continue
            resolved.append({"name": name, "character_id": cid, "resolved": cid is not None})
            if cid is None:
                warnings.append(f"「{d['title']}」的出场角色「{name}」不在角色表中")
        d["characters"] = resolved
    plans = await plan_service.plan_outs(
        db, job.project_id, (await plan_service.plans_of(db, [job.outline_id])).get(job.outline_id, [])
    )
    existing = [
        {
            "id": p.id,
            "seq": p.seq,
            "title": p.title,
            "summary": p.summary,
            "chapter_id": p.chapter_id,
            "chapter_no": p.chapter_no,
            "has_content": p.has_content,
        }
        for p in plans
    ]
    return {"outline_id": job.outline_id, "drafts": drafts, "existing": existing, "warnings": warnings}


async def _stream(job: OutlineJob, messages: list[ChatMessage], pieces: list[str]) -> AsyncIterator[Event]:
    stream_iter = job.provider.generate_stream(messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking)
    async with aclosing(stream_iter) as stream:
        async for piece in stream:
            pieces.append(piece)
            yield "delta", {"text": piece}


async def run_outline_job(job: OutlineJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    try:
        if job.kind == "outline.continue":
            pieces: list[str] = []
            async with aclosing(_stream(job, job.messages, pieces)) as events:
                async for ev in events:
                    yield ev
            raw = "".join(pieces)
            if not raw.strip():
                raise AIProviderError("模型未返回任何内容")
            rows, volume_title = normalize_continue(extract_json(raw))
            async with sessionmaker() as db:
                result = await continue_result(db, job, rows, volume_title)
        else:
            drafts: list[dict[str, Any]] = []
            warnings: list[str] = []
            seen: set[str] = set()
            remaining = job.count
            first = True
            while remaining > 0:
                size = min(EXPAND_BATCH, remaining)
                if not first:
                    yield "delta", {"text": "\n\n"}
                first = False
                previous = [e for d in drafts for e in d["key_events"]]
                pieces = []
                async with aclosing(_stream(job, expand_messages(job, size, previous), pieces)) as events:
                    async for ev in events:
                        yield ev
                raw = "".join(pieces)
                if not raw.strip():
                    raise AIProviderError("模型未返回任何内容")
                batch = normalize_expand(extract_json(raw))
                if not batch:
                    raise JSONExtractError("未解析到任何细纲")
                if len(batch) > size:
                    warnings.append(f"模型返回了 {len(batch)} 章，只保留前 {size} 章")
                    batch = batch[:size]
                warnings += dedupe_key_events(batch, seen)
                drafts += batch
                # 模型少给时不再追加批次，避免死循环
                remaining -= size
            if len(drafts) < job.count:
                warnings.append(f"模型只返回了 {len(drafts)} 章细纲（要求 {job.count} 章）")
            async with sessionmaker() as db:
                result = await expand_result(db, job, drafts, warnings)
        yield "done", result
    except JSONExtractError as exc:
        yield "error", {"detail": f"AI 输出解析失败：{exc}，请重新生成"}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except BadRequestError as exc:
        yield "error", {"detail": exc.detail}
    except Exception:  # noqa: BLE001
        logger.exception("大纲生成失败 project=%s kind=%s", job.project_id, job.kind)
        yield "error", {"detail": "生成失败：服务器内部错误"}
