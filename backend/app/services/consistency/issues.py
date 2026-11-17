"""跨章比对与问题合并。

比对拆成三步，任务处理器可以在每步之间插入授权与模型绑定的重验：

1. ``load_compare_input`` → ``group_facts`` → ``plan_batches`` → ``build_compare_messages``：组装；
2. ``model_call.complete``：调用模型；
3. ``parse_comparison`` → ``merge_findings``：校验输出并按 C8 规则合并进问题表。

``compare_and_merge`` 是把三步串起来的便捷入口。本模块不做任何鉴权，``merge_findings`` 也不自行加锁：
调用方必须已经校验权限，并在合并事务内持有作品行锁（与状态 CAS 串行）。

读取端的派生字段 ``needs_recheck`` / ``not_reproduced`` 由 ``issue_flags`` 批量计算，不逐条查库。
"""

import hashlib
from dataclasses import dataclass, field
from itertools import batched
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts.consistency import COMPARE_KEY, build_consistency_compare_messages
from app.ai.providers.base import AIProviderError, ChatMessage
from app.ai.templating import PromptOverride
from app.models import Chapter, ConsistencyIssue, ConsistencyLedger, Foreshadow
from app.models.consistency import CONSISTENCY_CATEGORIES, CONSISTENCY_SEVERITIES
from app.schemas.consistency import MAX_EVIDENCE_PER_ISSUE, IssueEvidence
from app.services import prompt_service
from app.services.consistency.ledger import DEFAULT_CHAR_BUDGET, is_fresh
from app.services.consistency.model_call import ModelTarget, complete
from app.services.mentions.matcher import text_hash
from app.services.rich_document_ai import analysis_fingerprints

CATEGORY_LABELS = {
    "character_history": "人物经历",
    "character_state": "人物状态",
    "item": "物品去向",
    "timeline": "时间线索",
    "foreshadow": "伏笔相关",
}
FORESHADOW_STATUS_LABELS = {
    "pending": "待埋设",
    "planted": "已埋设",
    "resolved": "已回收",
    "partial": "部分回收",
    "abandoned": "已放弃",
}
# 以主体分组的三个类别；时间线与伏笔各自整体成组
SUBJECT_CATEGORIES = ("character_history", "character_state", "item")
TIMELINE_GROUP = "timeline"
FORESHADOW_GROUP = "foreshadow"

TITLE_LIMIT = 200
TEXT_LIMIT = 2000
MIN_BATCH_CHARS = 1000
MAX_FORESHADOWS = 200
MAX_EXISTING_PER_BATCH = 30
FORESHADOW_CONTENT_LIMIT = 300
EXISTING_DESCRIPTION_LIMIT = 200


# ---------------- 组装 ----------------


@dataclass(frozen=True)
class FactRef:
    """参与比对的一条事实及其所在章节（章号为读取时的当前值）。"""

    chapter_id: int
    chapter_no: int
    chapter_title: str
    # 该章台账记录的正文指纹；台账新鲜时即当前正文指纹，写入证据锚点
    text_hash: str
    in_scope: bool
    fact: dict[str, Any]

    @property
    def key(self) -> tuple[int, str]:
        return self.chapter_id, str(self.fact["id"])


@dataclass(frozen=True)
class ForeshadowRecord:
    id: int
    title: str
    content: str
    status: str
    actual_plant_chapter: int | None
    planned_plant_chapter: int | None
    actual_resolve_chapter: int | None
    planned_resolve_chapter: int | None


@dataclass(frozen=True)
class ExistingIssue:
    id: int
    category: str
    title: str
    description: str
    # 证据中仍有章节 id 的章节集合
    chapter_ids: frozenset[int]


@dataclass(frozen=True)
class CompareInput:
    project_id: int
    # 按 (当前章号, 章节 id, 章内顺序) 排序
    facts: tuple[FactRef, ...]
    scope_ids: frozenset[int]
    # 有新鲜台账、参与了本次比对的章节（范围内外都算）
    covered_ids: frozenset[int]
    # 范围外因台账过期而未使用的章数
    stale_outside: int
    foreshadows: tuple[ForeshadowRecord, ...]
    existing: tuple[ExistingIssue, ...]
    # 允许进入上下文的伏笔记录总数（foreshadows 最多取 MAX_FORESHADOWS 条，差额进入 warning）
    foreshadow_total: int = 0


@dataclass(frozen=True)
class CompareGroup:
    key: str
    facts: tuple[FactRef, ...]


@dataclass(frozen=True)
class CompareBatch:
    index: int
    group_keys: tuple[str, ...]
    facts: tuple[FactRef, ...]
    foreshadows: tuple[ForeshadowRecord, ...] = ()
    existing: tuple[ExistingIssue, ...] = ()
    # 本批只是某个超出预算的大组的一段：跨批的冲突无法检出
    partial: bool = False
    # 因数量上限或预算未能随本批发送的伏笔记录数、已有问题 id
    foreshadows_omitted: int = 0
    existing_omitted: tuple[int, ...] = ()

    @property
    def fact_index(self) -> dict[tuple[int, str], tuple[int, FactRef]]:
        return {ref.key: (order, ref) for order, ref in enumerate(self.facts)}


async def load_compare_input(db: AsyncSession, project_id: int, scope_chapter_ids: list[int]) -> CompareInput:
    """读取参与比对的台账：范围内章节加范围外已有的新鲜台账；过期台账一律不使用。"""
    scope = frozenset(scope_chapter_ids)
    ledgers = {
        row.chapter_id: row
        for row in await db.scalars(select(ConsistencyLedger).where(ConsistencyLedger.project_id == project_id))
    }
    chapters: list[Chapter] = []
    for chunk in batched(sorted(ledgers), 200):
        chapters.extend(
            await db.scalars(select(Chapter).where(Chapter.project_id == project_id, Chapter.id.in_(chunk)))
        )
    chapters.sort(key=lambda chapter: (chapter.chapter_no, chapter.id))
    fingerprints = await analysis_fingerprints(db, chapters)
    facts: list[FactRef] = []
    covered: set[int] = set()
    stale_outside = 0
    for chapter in chapters:
        ledger = ledgers[chapter.id]
        if not is_fresh(ledger, fingerprints.get(chapter.id)):
            stale_outside += chapter.id not in scope
            continue
        covered.add(chapter.id)
        for fact in ledger.facts or []:
            if isinstance(fact, dict) and fact.get("id") and fact.get("category") in CONSISTENCY_CATEGORIES:
                facts.append(
                    FactRef(
                        chapter_id=chapter.id,
                        chapter_no=chapter.chapter_no,
                        chapter_title=chapter.title,
                        text_hash=ledger.text_hash,
                        in_scope=chapter.id in scope,
                        fact=fact,
                    )
                )
    foreshadows = tuple(
        ForeshadowRecord(
            id=row.id,
            title=row.title,
            content=row.content,
            status=row.status,
            actual_plant_chapter=row.actual_plant_chapter,
            planned_plant_chapter=row.planned_plant_chapter,
            actual_resolve_chapter=row.actual_resolve_chapter,
            planned_resolve_chapter=row.planned_resolve_chapter,
        )
        for row in await db.scalars(
            select(Foreshadow)
            .where(Foreshadow.project_id == project_id, Foreshadow.include_in_context.is_(True))
            .order_by(Foreshadow.id)
            .limit(MAX_FORESHADOWS)
        )
    )
    foreshadow_total = (
        await db.scalar(
            select(func.count(Foreshadow.id)).where(
                Foreshadow.project_id == project_id, Foreshadow.include_in_context.is_(True)
            )
        )
        or 0
    )
    existing = tuple(
        ExistingIssue(
            id=row.id,
            category=row.category,
            title=row.title,
            description=row.description,
            chapter_ids=frozenset(_evidence_chapters(row.evidence)[0]),
        )
        for row in await db.scalars(
            select(ConsistencyIssue).where(ConsistencyIssue.project_id == project_id).order_by(ConsistencyIssue.id)
        )
    )
    return CompareInput(
        project_id=project_id,
        facts=tuple(facts),
        scope_ids=scope,
        covered_ids=frozenset(covered),
        stale_outside=stale_outside,
        foreshadows=foreshadows,
        existing=existing,
        foreshadow_total=max(foreshadow_total, len(foreshadows)),
    )


def group_key(fact: dict[str, Any]) -> str:
    category = fact["category"]
    if category == "timeline":
        return TIMELINE_GROUP
    if category == "foreshadow":
        return FORESHADOW_GROUP
    subject = fact.get("subject_key") or ""
    # 同一主体的经历、状态与物品去向放在一起比对；没有主体的按类别兜底
    return f"subject:{subject}" if subject else f"category:{category}"


def group_facts(data: CompareInput) -> list[CompareGroup]:
    """按主体／类别分组（时间线、伏笔各自成组），组内保持章节顺序。

    只保留可能产出有效问题的组：至少一条事实在范围内，且事实跨两个不同章节
    （伏笔组在有伏笔记录时一个章节即可）。
    """
    buckets: dict[str, list[FactRef]] = {}
    for ref in data.facts:
        buckets.setdefault(group_key(ref.fact), []).append(ref)
    groups = []
    for key, refs in buckets.items():
        chapters = {ref.chapter_id for ref in refs}
        enough = len(chapters) >= 2 or (key == FORESHADOW_GROUP and bool(data.foreshadows))
        if enough and any(ref.in_scope for ref in refs):
            groups.append(CompareGroup(key=key, facts=tuple(refs)))
    groups.sort(key=lambda group: (group.facts[0].chapter_no, group.facts[0].chapter_id, group.key))
    return groups


def _inline(value: Any) -> str:
    return " ".join(str(value or "").split())


def fact_line(ref: FactRef) -> str:
    fact = ref.fact
    return (
        f"[chapter_id={ref.chapter_id} fact_id={fact['id']}] 第{ref.chapter_no}章｜"
        f"{CATEGORY_LABELS[fact['category']]}｜{_inline(fact.get('subject'))}｜"
        f"{_inline(fact.get('statement'))}｜{_inline(fact.get('time_hint'))}"
    )


def _chapter_label(actual: int | None, planned: int | None) -> str:
    if actual is not None:
        return f"第{actual}章"
    return f"计划第{planned}章" if planned is not None else "未定"


def foreshadow_line(record: ForeshadowRecord) -> str:
    return (
        f"#{record.id} {_inline(record.title)}｜{_inline(record.content)[:FORESHADOW_CONTENT_LIMIT]}｜"
        f"{FORESHADOW_STATUS_LABELS.get(record.status, record.status)}｜"
        f"埋设章：{_chapter_label(record.actual_plant_chapter, record.planned_plant_chapter)}｜"
        f"回收章：{_chapter_label(record.actual_resolve_chapter, record.planned_resolve_chapter)}"
    )


def existing_line(issue: ExistingIssue) -> str:
    chapters = "、".join(str(chapter_id) for chapter_id in sorted(issue.chapter_ids))
    return (
        f"#{issue.id} {CATEGORY_LABELS.get(issue.category, issue.category)}｜{_inline(issue.title)}｜"
        f"{_inline(issue.description)[:EXISTING_DESCRIPTION_LIMIT]}｜{chapters}"
    )


def _fit(items: list[Any], render, budget: int, limit: int) -> tuple[Any, ...]:
    kept, used = [], 0
    for item in items[:limit]:
        size = len(render(item)) + 1
        if used + size > budget:
            break
        kept.append(item)
        used += size
    return tuple(kept)


def plan_batches(
    data: CompareInput, groups: list[CompareGroup], *, char_budget: int = DEFAULT_CHAR_BUDGET
) -> list[CompareBatch]:
    """在上下文预算内分批：小组整组合并进同一批，超过预算的大组按章节顺序切成连续几批。

    伏笔记录只附在含伏笔组的批次上；已有问题只附在「类别相同且证据章节都在本批内」的批次上。
    """
    budget = max(MIN_BATCH_CHARS, char_budget)
    chunks: list[tuple[list[str], list[FactRef], bool]] = []
    keys: list[str] = []
    refs: list[FactRef] = []
    used = 0

    def flush(partial: bool = False) -> None:
        nonlocal keys, refs, used
        if refs:
            chunks.append((keys, refs, partial))
        keys, refs, used = [], [], 0

    for group in groups:
        sizes = [len(fact_line(ref)) + 1 for ref in group.facts]
        total = sum(sizes)
        if total > budget:
            # 大组独占若干批；跨批的矛盾可能漏检，属已知取舍（批次标记 partial，由 batch_notices 告知作者）
            flush()
            for ref, size in zip(group.facts, sizes, strict=True):
                if refs and used + size > budget:
                    flush(partial=True)
                if not keys:
                    keys.append(group.key)
                refs.append(ref)
                used += size
            flush(partial=True)
            continue
        if refs and used + total > budget:
            flush()
        keys.append(group.key)
        refs.extend(group.facts)
        used += total
    flush()

    batches = []
    foreshadow_total = max(data.foreshadow_total, len(data.foreshadows))
    for index, (group_keys, facts, partial) in enumerate(chunks):
        chapter_ids = {ref.chapter_id for ref in facts}
        categories = {ref.fact["category"] for ref in facts}
        with_foreshadows = FORESHADOW_GROUP in group_keys
        foreshadows = (
            _fit(list(data.foreshadows), foreshadow_line, budget // 3, MAX_FORESHADOWS) if with_foreshadows else ()
        )
        related = [
            issue
            for issue in data.existing
            if issue.category in categories and issue.chapter_ids and issue.chapter_ids <= chapter_ids
        ]
        existing = _fit(related, existing_line, budget // 4, MAX_EXISTING_PER_BATCH)
        sent = {issue.id for issue in existing}
        batches.append(
            CompareBatch(
                index=index,
                group_keys=tuple(group_keys),
                facts=tuple(facts),
                foreshadows=foreshadows,
                existing=existing,
                partial=partial,
                foreshadows_omitted=foreshadow_total - len(foreshadows) if with_foreshadows else 0,
                existing_omitted=tuple(issue.id for issue in related if issue.id not in sent),
            )
        )
    return batches


def has_partial(batches: list[CompareBatch]) -> bool:
    """有大组被拆成多批：比对没有覆盖全部跨章组合，调用方不得据此标记「本次未复现」。"""
    return any(batch.partial for batch in batches)


def batch_notices(batches: list[CompareBatch]) -> list[str]:
    """分批时被拆分或被省略的输入：必须进入检查概要的 warnings，不能静默截断后声称完整。"""
    notices = []
    if has_partial(batches):
        notices.append("部分分组的事实超过单批上限，已拆成多批比对：跨批的冲突可能漏检，本次不标记「本次未复现」")
    foreshadows = max((batch.foreshadows_omitted for batch in batches), default=0)
    if foreshadows:
        notices.append(f"允许进入上下文的伏笔记录较多，有 {foreshadows} 条未能随比对发送，与它们相关的冲突可能漏检")
    existing = {issue_id for batch in batches for issue_id in batch.existing_omitted}
    if existing:
        notices.append(f"已有问题较多，有 {len(existing)} 条的摘要未能随比对发送；再次检出时只能按证据章节去重")
    return notices


def build_compare_messages(batch: CompareBatch, override: PromptOverride | None = None) -> list[ChatMessage]:
    """只含事实台账、允许进入上下文的伏笔记录与已有问题摘要；不含正文与结构化设定。"""
    return build_consistency_compare_messages(
        facts_text="\n".join(fact_line(ref) for ref in batch.facts),
        foreshadows_text="\n".join(foreshadow_line(record) for record in batch.foreshadows),
        existing_issues_text="\n".join(existing_line(issue) for issue in batch.existing),
        override=override,
    )


# ---------------- 输出校验 ----------------


@dataclass(frozen=True)
class Finding:
    """一条通过校验的潜在冲突。evidence 已是落库结构（引句与偏移取自台账，不取模型输出）。"""

    category: str
    title: str
    description: str
    suggestion: str
    severity: str
    evidence: tuple[dict[str, Any], ...]
    identity_key: str
    existing_issue_id: int | None = None


def identity_key(category: str, subject: str, chapter_ids: set[int] | frozenset[int] | list[int]) -> str:
    """去重身份：sha256(category | subject_key | 排序后的证据 chapter_id 集合)。"""
    chapters = ",".join(str(chapter_id) for chapter_id in sorted(set(chapter_ids)))
    return hashlib.sha256(f"{category}|{subject}|{chapters}".encode()).hexdigest()


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().lstrip("#").isdecimal():
        return int(value.strip().lstrip("#")) or None
    return None


def _clip(value: Any, limit: int) -> str:
    return value.strip()[:limit].strip() if isinstance(value, str) else ""


def parse_comparison(text: str, batch: CompareBatch) -> tuple[list[Finding], list[str]]:
    """模型原文 → 通过校验的问题与 warning。输出不是 JSON 时抛 JSONExtractError。

    - 证据必须引用本批输入中存在的 chapter_id + fact_id，否则丢弃该处证据并记 warning；
    - 只保留证据跨两个不同章节的问题，或只涉及一个章节但指认了本批伏笔记录的伏笔类问题；
    - 没有任何证据在检查范围内的问题不报告。
    existing_issue_id 只接受本批输入给出的已有问题编号；是否属于本作品由 merge_findings 再判定一次。
    """
    raw = extract_json(text)
    items = raw.get("issues") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return [], ["模型输出中没有问题列表"]
    index = batch.fact_index
    foreshadow_ids = {record.id for record in batch.foreshadows}
    offered_ids = {issue.id for issue in batch.existing}
    findings: list[Finding] = []
    warnings: list[str] = []
    missing = malformed = insufficient = unknown_refs = 0
    for item in items:
        if not isinstance(item, dict):
            malformed += 1
            continue
        picked: dict[tuple[int, str], tuple[int, FactRef]] = {}
        raw_evidence = item.get("evidence")
        for entry in raw_evidence if isinstance(raw_evidence, list) else []:
            chapter_id = _int(entry.get("chapter_id")) if isinstance(entry, dict) else None
            fact_id = entry.get("fact_id") if isinstance(entry, dict) else None
            key = (chapter_id, str(fact_id).strip()) if chapter_id is not None and fact_id is not None else None
            if key is None or key not in index:
                missing += 1
                continue
            picked[key] = index[key]
        refs = [ref for _order, ref in sorted(picked.values(), key=lambda pair: pair[0])][:MAX_EVIDENCE_PER_ISSUE]
        chapter_ids = {ref.chapter_id for ref in refs}
        foreshadow_id = _int(item.get("foreshadow_id"))
        if foreshadow_id not in foreshadow_ids:
            foreshadow_id = None
        against_record = len(chapter_ids) == 1 and foreshadow_id is not None
        if len(chapter_ids) < 2 and not against_record:
            insufficient += 1
            continue
        if not any(ref.in_scope for ref in refs):
            continue
        category = item.get("category") if isinstance(item.get("category"), str) else ""
        category = category.strip().lower()
        if against_record:
            category = "foreshadow"
        elif category not in CONSISTENCY_CATEGORIES:
            category = refs[0].fact["category"]
        description = _clip(item.get("description"), TEXT_LIMIT)
        title = _clip(item.get("title"), TITLE_LIMIT) or description[:TITLE_LIMIT]
        if not title:
            malformed += 1
            continue
        severity = item.get("severity") if item.get("severity") in CONSISTENCY_SEVERITIES else "medium"
        subject = ",".join(sorted({ref.fact.get("subject_key") or "" for ref in refs}))
        if against_record:
            subject = f"{subject}#foreshadow:{foreshadow_id}"
        existing_issue_id = _int(item.get("existing_issue_id"))
        if existing_issue_id is not None and existing_issue_id not in offered_ids:
            # 只能指认本批输入里给出的已有问题：编造或别处的编号不得覆盖无关问题，改按身份键处理
            unknown_refs += 1
            existing_issue_id = None
        findings.append(
            Finding(
                category=category,
                title=title,
                description=description,
                suggestion=_clip(item.get("suggestion"), TEXT_LIMIT),
                severity=severity,
                evidence=tuple(
                    {
                        "chapter_id": ref.chapter_id,
                        "fact_id": str(ref.fact["id"]),
                        "quote": ref.fact.get("quote") or "",
                        "start": ref.fact.get("start"),
                        "end": ref.fact.get("end"),
                        "text_hash": ref.text_hash,
                    }
                    for ref in refs
                ),
                identity_key=identity_key(category, subject, chapter_ids),
                existing_issue_id=existing_issue_id,
            )
        )
    if missing:
        warnings.append(f"有 {missing} 处证据引用了不存在的事实，已丢弃")
    if insufficient:
        warnings.append(f"有 {insufficient} 个问题的证据不足两个章节，已忽略")
    if malformed:
        warnings.append(f"有 {malformed} 个问题格式不正确，已忽略")
    if unknown_refs:
        warnings.append(f"有 {unknown_refs} 个问题指认了未提供的已有问题编号，已按证据章节去重")
    return findings, warnings


# ---------------- 合并（C8） ----------------


@dataclass
class MergeStats:
    created: int = 0
    # 已存在且本次再次检出（含保持 ignored / open 的）
    updated: int = 0
    # resolved → open
    reappeared: int = 0
    # open 且证据章节全在覆盖内、本次未检出
    not_reproduced: int = 0
    # 同一次检查里指向同一问题的重复条目
    duplicates: int = 0
    # 不存在或不属于本作品的 existing_issue_id（已忽略，按身份键处理）
    ignored_refs: int = 0


def _evidence_chapters(evidence: Any) -> tuple[set[int], bool]:
    """(证据中的章节 id 集合, 是否存在没有章节 id 的证据)。"""
    ids: set[int] = set()
    orphan = False
    for item in evidence if isinstance(evidence, list) else []:
        chapter_id = item.get("chapter_id") if isinstance(item, dict) else None
        if isinstance(chapter_id, int) and not isinstance(chapter_id, bool):
            ids.add(chapter_id)
        else:
            orphan = True
    return ids, orphan


async def merge_findings(
    db: AsyncSession,
    *,
    project_id: int,
    run_id: int,
    findings: list[Finding],
    covered_chapter_ids: set[int] | frozenset[int],
    scope_chapter_ids: set[int] | frozenset[int],
) -> MergeStats:
    """把本次检出的问题合并进问题表（不 commit；调用方持作品行锁并在同一事务提交）。

    - 模型给出有效且属于本作品的 existing_issue_id 时按 id 合并，否则按 identity_key；
    - 已存在：更新说明、建议、证据与 last_seen_run_id；ignored 保持，resolved → open 且 reappeared，open 保持；
    - 不存在：新建 open；
    - 未检出的 open 问题，证据章节全在本次覆盖内且至少一个在范围内：只更新 last_checked_run_id。
    revision 只属于作者对状态与备注的 CAS，这里一律不递增。
    covered_chapter_ids 必须是「比对确实完成」的章节：有批次失败时调用方应传空集合。
    """
    stats = MergeStats()
    rows = list(
        await db.scalars(
            select(ConsistencyIssue).where(ConsistencyIssue.project_id == project_id).order_by(ConsistencyIssue.id)
        )
    )
    by_id = {row.id: row for row in rows}
    by_key = {row.identity_key: row for row in rows}
    touched: set[int] = set()
    for finding in findings:
        evidence = [IssueEvidence.model_validate(item).model_dump() for item in finding.evidence]
        target = None
        if finding.existing_issue_id is not None:
            target = by_id.get(finding.existing_issue_id)
            if target is None:
                stats.ignored_refs += 1
        same_key = by_key.get(finding.identity_key)
        if target is None:
            target = same_key
        elif same_key is not None and same_key is not target:
            # 身份键对应的另一条是同一矛盾的旧记录：本次不再把它标成「未复现」
            touched.add(id(same_key))
        if target is None:
            issue = ConsistencyIssue(
                project_id=project_id,
                category=finding.category,
                title=finding.title,
                description=finding.description,
                suggestion=finding.suggestion,
                severity=finding.severity,
                status="open",
                identity_key=finding.identity_key,
                evidence=evidence,
                first_run_id=run_id,
                last_seen_run_id=run_id,
                last_checked_run_id=run_id,
            )
            db.add(issue)
            by_key[finding.identity_key] = issue
            touched.add(id(issue))
            stats.created += 1
            continue
        if id(target) in touched:
            stats.duplicates += 1
            continue
        touched.add(id(target))
        target.description = finding.description
        target.suggestion = finding.suggestion
        target.evidence = evidence
        target.last_seen_run_id = run_id
        target.last_checked_run_id = run_id
        if same_key is None and target.identity_key != finding.identity_key:
            # 按 id 指认且证据章节集合变了：身份键跟随新证据，之后不靠模型指认也能对上
            by_key.pop(target.identity_key, None)
            target.identity_key = finding.identity_key
            by_key[finding.identity_key] = target
        if target.status == "resolved":
            target.status = "open"
            target.reappeared = True
            stats.reappeared += 1
        stats.updated += 1
    covered, scope = set(covered_chapter_ids), set(scope_chapter_ids)
    for row in rows:
        if id(row) in touched or row.status != "open":
            continue
        chapter_ids, orphan = _evidence_chapters(row.evidence)
        if chapter_ids and not orphan and chapter_ids <= covered and chapter_ids & scope:
            row.last_checked_run_id = run_id
            stats.not_reproduced += 1
    await db.flush()
    return stats


# ---------------- 读取端派生字段 ----------------


@dataclass(frozen=True)
class IssueFlags:
    needs_recheck: bool
    not_reproduced: bool
    # 与 evidence 一一对应：章节已删除或当前正文指纹与存值不同
    stale: tuple[bool, ...]


async def chapter_text_hashes(db: AsyncSession, project_id: int, chapter_ids: set[int] | list[int]) -> dict[int, str]:
    """批量取章节当前正文指纹；不存在（已删除、不属于本作品）的章节不在结果里。"""
    result: dict[int, str] = {}
    for chunk in batched(sorted(set(chapter_ids)), 200):
        rows = await db.execute(
            select(Chapter.id, Chapter.content).where(Chapter.project_id == project_id, Chapter.id.in_(chunk))
        )
        result.update({chapter_id: text_hash(content) for chapter_id, content in rows})
    return result


def evidence_stale(evidence: Any, hashes: dict[int, str]) -> tuple[bool, ...]:
    flags = []
    for item in evidence if isinstance(evidence, list) else []:
        chapter_id = item.get("chapter_id") if isinstance(item, dict) else None
        flags.append(chapter_id is None or hashes.get(chapter_id) != item.get("text_hash"))
    return tuple(flags)


def needs_recheck(evidence: Any, hashes: dict[int, str]) -> bool:
    """任一证据章节的当前正文指纹与存值不同，或章节已不存在。"""
    return any(evidence_stale(evidence, hashes))


def not_reproduced(issue: ConsistencyIssue) -> bool:
    """待处理且最近一次覆盖它的检查晚于最近一次检出它的检查。"""
    if issue.status != "open" or issue.last_checked_run_id is None:
        return False
    return issue.last_seen_run_id is None or issue.last_checked_run_id > issue.last_seen_run_id


async def issue_flags(db: AsyncSession, project_id: int, issues: list[ConsistencyIssue]) -> dict[int, IssueFlags]:
    """一次查询取全部所涉章节的指纹，再在内存里计算每个问题的派生字段。"""
    chapter_ids: set[int] = set()
    for issue in issues:
        chapter_ids |= _evidence_chapters(issue.evidence)[0]
    hashes = await chapter_text_hashes(db, project_id, chapter_ids)
    result = {}
    for issue in issues:
        stale = evidence_stale(issue.evidence, hashes)
        result[issue.id] = IssueFlags(needs_recheck=any(stale), not_reproduced=not_reproduced(issue), stale=stale)
    return result


# ---------------- 便捷入口 ----------------


@dataclass
class CompareOutcome:
    stats: MergeStats
    batches: int = 0
    failed_batches: int = 0
    stale_outside: int = 0
    warnings: list[str] = field(default_factory=list)


async def compare_and_merge(
    sessionmaker: async_sessionmaker[AsyncSession],
    target: ModelTarget,
    *,
    user_id: int,
    project_id: int,
    run_id: int,
    scope_chapter_ids: list[int],
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> CompareOutcome:
    """比对并合并（便捷入口，自行提交；不加作品行锁，需要与状态 CAS 串行的调用方请分步调用）。

    单批模型或解析失败只记 warning、其余批次照常合并；有批次失败或大组被拆批时不标记「本次未复现」。
    user_id 只用于读取本人的提示词覆盖。
    """
    async with sessionmaker() as db:
        data = await load_compare_input(db, project_id, scope_chapter_ids)
        batches = plan_batches(data, group_facts(data), char_budget=char_budget)
        override = await prompt_service.get_override(db, user_id, COMPARE_KEY) if batches else None
    outcome = CompareOutcome(stats=MergeStats(), batches=len(batches), stale_outside=data.stale_outside)
    outcome.warnings.extend(batch_notices(batches))
    findings: list[Finding] = []
    for batch in batches:
        try:
            found, warnings = parse_comparison(await complete(target, build_compare_messages(batch, override)), batch)
        except AIProviderError as exc:
            outcome.failed_batches += 1
            outcome.warnings.append(f"第 {batch.index + 1} 批比对失败：{exc}")
            continue
        except JSONExtractError as exc:
            outcome.failed_batches += 1
            outcome.warnings.append(f"第 {batch.index + 1} 批比对失败：AI 输出解析失败：{exc}")
            continue
        findings.extend(found)
        outcome.warnings.extend(warnings)
    async with sessionmaker() as db:
        outcome.stats = await merge_findings(
            db,
            project_id=project_id,
            run_id=run_id,
            findings=findings,
            covered_chapter_ids=frozenset() if outcome.failed_batches or has_partial(batches) else data.covered_ids,
            scope_chapter_ids=data.scope_ids,
        )
        await db.commit()
    return outcome
