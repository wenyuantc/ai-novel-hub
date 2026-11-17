"""事实台账：新鲜度判断、单章抽取与落库。

单章抽取拆成三步，任务处理器可以在每步之间插入授权与模型绑定的重验：

1. ``load_source`` + ``build_extract_messages``：读取章节并组装消息（只含 AI 可见正文）；
2. ``model_call.complete``：调用模型；
3. ``parse_extraction`` + ``save_ledger``：规范化并落库，落库事务内重验章节仍存在且指纹未变。

``extract_chapter`` 是把三步串起来的便捷入口（各步使用独立的短事务），供不需要逐步重验的调用方
（例如改稿影响追踪）直接使用。本模块不做任何鉴权：调用方必须已经校验过作品权限与 AI 能力。
"""

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts.consistency import EXTRACT_KEY, build_consistency_extract_messages
from app.ai.providers.base import AIProviderError, ChatMessage
from app.ai.templating import PromptOverride
from app.core.errors import ConflictError
from app.models import Chapter, ConsistencyLedger
from app.models.consistency import LEDGER_SCHEMA_VERSION
from app.services import prompt_service
from app.services.consistency.facts import normalize_facts
from app.services.consistency.model_call import ModelTarget, complete
from app.services.mentions.matcher import text_hash
from app.services.rich_document_ai import (
    analysis_fingerprints,
    count_visible_chapter_sections,
    render_visible_sections,
    visible_sections,
)

DEFAULT_CHAR_BUDGET = 12_000
EMPTY_CONTENT = "章节正文为空"
CHANGED = "章节正文在检查期间发生变化，本次结果未保存"

LedgerState = Literal["fresh", "stale", "missing"]
ExtractState = Literal["extracted", "reused", "skipped_changed", "skipped_missing", "failed"]


@dataclass(frozen=True)
class ChapterSource:
    """一章的抽取输入快照。text 是发给模型的全文；source_hash / text_hash 用于落库前重验。"""

    project_id: int
    chapter_id: int
    chapter_no: int
    title: str
    # Chapter.content：引句定位与证据锚点的坐标系
    content: str
    # AI 可见正文：正文 + 开启 AI 可见的作者区块（与章节分析同口径）
    text: str
    source_hash: str
    text_hash: str


@dataclass(frozen=True)
class ExtractResult:
    chapter_id: int
    state: ExtractState
    ledger_id: int | None = None
    facts: int = 0
    warnings: tuple[str, ...] = ()
    error: str = ""


def is_fresh(ledger: ConsistencyLedger | None, fingerprint: str | None) -> bool:
    """台账可复用：来源指纹与当前分析指纹一致，且结构版本相同。"""
    return (
        ledger is not None
        and fingerprint is not None
        and ledger.schema_version == LEDGER_SCHEMA_VERSION
        and ledger.source_hash == fingerprint
    )


async def ledgers_by_chapter(
    db: AsyncSession, project_id: int, chapter_ids: list[int] | None = None
) -> dict[int, ConsistencyLedger]:
    statement = select(ConsistencyLedger).where(ConsistencyLedger.project_id == project_id)
    if chapter_ids is not None:
        if not chapter_ids:
            return {}
        statement = statement.where(ConsistencyLedger.chapter_id.in_(chapter_ids))
    return {row.chapter_id: row for row in await db.scalars(statement)}


async def ledger_states(db: AsyncSession, project_id: int, chapters: list[Chapter]) -> dict[int, LedgerState]:
    """批量判断各章台账状态（预览与覆盖统计用）。只为已有台账的章节计算指纹；空章由调用方自行排除。"""
    ledgers = await ledgers_by_chapter(db, project_id, [chapter.id for chapter in chapters])
    fingerprints = await analysis_fingerprints(db, [chapter for chapter in chapters if chapter.id in ledgers])
    return {
        chapter.id: (
            "missing"
            if chapter.id not in ledgers
            else "fresh"
            if is_fresh(ledgers[chapter.id], fingerprints.get(chapter.id))
            else "stale"
        )
        for chapter in chapters
    }


async def visible_text(db: AsyncSession, project_id: int, chapter: Chapter, char_budget: int) -> str:
    """与章节分析相同口径的 AI 可见正文：正文之后附开启的作者区块；关闭的区块根本不读取。

    预算算法与 analysis_service.build_analysis_job 保持一致（区块最多占预算的四分之一、上限 3000 字）。
    """
    author_context = ""
    author_budget = min(3_000, max(0, char_budget // 4))
    if author_budget >= 100:
        count = await count_visible_chapter_sections(db, project_id, chapter.id)
        if count:
            row_budget = min(32, count, max(1, author_budget // 256))
            visible = await visible_sections(
                db,
                project_id,
                chapter_ids=[chapter.id],
                max_rows=row_budget,
                text_limit=min(2_000, max(1, author_budget // row_budget)),
            )
            author_context = render_visible_sections(visible, author_budget)
    return "\n\n".join(part for part in (chapter.content, author_context) if part)


async def load_source(
    db: AsyncSession, project_id: int, chapter_id: int, *, char_budget: int = DEFAULT_CHAR_BUDGET
) -> ChapterSource | None:
    """读取一章的抽取输入；章节不存在或不属于该作品返回 None。不含任何结构化设定。"""
    chapter = await db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id))
    if chapter is None:
        return None
    text = await visible_text(db, project_id, chapter, char_budget)
    fingerprint = (await analysis_fingerprints(db, [chapter]))[chapter.id]
    return ChapterSource(
        project_id=project_id,
        chapter_id=chapter.id,
        chapter_no=chapter.chapter_no,
        title=chapter.title,
        content=chapter.content,
        text=text,
        source_hash=fingerprint,
        text_hash=text_hash(chapter.content),
    )


def build_extract_messages(source: ChapterSource, override: PromptOverride | None = None) -> list[ChatMessage]:
    return build_consistency_extract_messages(
        chapter_no=source.chapter_no, title=source.title, content=source.text, override=override
    )


def parse_extraction(text: str, source: ChapterSource) -> tuple[list[dict[str, Any]], list[str]]:
    """模型原文 → 台账事实与 warning。输出不是 JSON 时抛 JSONExtractError；条目级畸形只丢弃该条。"""
    return normalize_facts(extract_json(text), source.content)


async def save_ledger(
    db: AsyncSession,
    source: ChapterSource,
    facts: list[dict[str, Any]],
    *,
    model: str,
    task_id: int | None = None,
) -> ConsistencyLedger | None:
    """在调用方事务内保存台账（不 commit）。

    先锁定并重读章节，重验它仍存在、分析指纹与正文指纹都与抽取时一致；任一不符返回 None、不写任何数据。
    每章至多一行：已有台账原位覆盖；来源指纹不同时旧的事实与指纹移入 prev_*（只保留一版）。
    """
    chapter = await db.scalar(
        select(Chapter)
        .where(Chapter.id == source.chapter_id, Chapter.project_id == source.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if chapter is None:
        return None
    try:
        fingerprint = (await analysis_fingerprints(db, [chapter]))[chapter.id]
    except ConflictError:
        # 读取期间章节又被改动，或区块结构无效：都不能证明结果对应当前正文
        return None
    if fingerprint != source.source_hash or text_hash(chapter.content) != source.text_hash:
        return None
    ledger = await db.scalar(select(ConsistencyLedger).where(ConsistencyLedger.chapter_id == chapter.id))
    if ledger is None:
        ledger = ConsistencyLedger(project_id=source.project_id, chapter_id=chapter.id)
        db.add(ledger)
    elif ledger.source_hash != source.source_hash:
        # 正文（或 AI 可见区块）自上次抽取后改过：保留上一版，供「修改影响」比较改动前后的事实。
        # 指纹相同的重复保存不移动，否则上一版会被同一版本的台账顶掉。
        ledger.prev_facts = ledger.facts
        ledger.prev_source_hash = ledger.source_hash
        ledger.prev_text_hash = ledger.text_hash
    ledger.source_hash = source.source_hash
    ledger.text_hash = source.text_hash
    ledger.facts = facts
    ledger.model = model[:200]
    ledger.task_id = task_id
    ledger.schema_version = LEDGER_SCHEMA_VERSION
    await db.flush()
    return ledger


async def extract_chapter(
    sessionmaker: async_sessionmaker[AsyncSession],
    target: ModelTarget,
    *,
    user_id: int,
    project_id: int,
    chapter_id: int,
    char_budget: int = DEFAULT_CHAR_BUDGET,
    task_id: int | None = None,
    force: bool = False,
) -> ExtractResult:
    """单章抽取并落库（便捷入口，自行提交）。

    台账新鲜且未 force 时直接复用、不调用模型。模型或解析失败返回 state=failed（已有台账不动）；
    正文在调用期间变化返回 skipped_changed、不保存。user_id 只用于读取本人的提示词覆盖。
    """
    async with sessionmaker() as db:
        source = await load_source(db, project_id, chapter_id, char_budget=char_budget)
        if source is None:
            return ExtractResult(chapter_id, "skipped_missing")
        if not source.content.strip():
            return ExtractResult(chapter_id, "skipped_changed", error=EMPTY_CONTENT)
        ledger = (await ledgers_by_chapter(db, project_id, [chapter_id])).get(chapter_id)
        if not force and is_fresh(ledger, source.source_hash):
            return ExtractResult(chapter_id, "reused", ledger_id=ledger.id, facts=len(ledger.facts))
        messages = build_extract_messages(source, await prompt_service.get_override(db, user_id, EXTRACT_KEY))
    try:
        facts, warnings = parse_extraction(await complete(target, messages), source)
    except AIProviderError as exc:
        return ExtractResult(chapter_id, "failed", error=str(exc))
    except JSONExtractError as exc:
        return ExtractResult(chapter_id, "failed", error=f"AI 输出解析失败：{exc}")
    async with sessionmaker() as db:
        saved = await save_ledger(db, source, facts, model=target.model, task_id=task_id)
        if saved is None:
            await db.rollback()
            return ExtractResult(chapter_id, "skipped_changed", error=CHANGED)
        await db.commit()
        return ExtractResult(chapter_id, "extracted", ledger_id=saved.id, facts=len(facts), warnings=tuple(warnings))
