"""一致性检查的读取与问题状态写入：概要、问题列表 / 详情、状态与备注的 CAS。

权限统一走 project_access.require_project：读取 content.read，改状态 content.edit（查看者 403、非协作者 404）。
派生字段 needs_recheck / not_reproduced 与证据的当前章号、标题、stale 标记都在读取时批量计算，不逐条查库。
"""

from datetime import UTC, datetime
from itertools import batched
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import Chapter, ConsistencyIssue, ConsistencyRun, Task
from app.models.consistency import CONSISTENCY_ISSUE_STATUSES
from app.models.entities import ACTIVE_TASK_STATUSES
from app.schemas.consistency import (
    ConsistencySummary,
    EvidenceOut,
    IssueOut,
    IssuePage,
    IssueUpdate,
    LedgerCoverage,
    RunOut,
)
from app.services.consistency import check, issues, ledger
from app.services.mentions.matcher import text_hash
from app.services.notification_service import consistency_run_visible
from app.services.project_access import require_project

ISSUE_NOT_FOUND = check.ISSUE_NOT_FOUND
_STOPPED_STATUSES = ("failed", "cancelled", "interrupted")
# 待处理在前，其次已解决、已忽略；同状态内按严重程度，再按新旧
_STATUS_ORDER = case(
    (ConsistencyIssue.status == "open", 0), (ConsistencyIssue.status == "resolved", 1), else_=2,
)
_SEVERITY_ORDER = case(
    (ConsistencyIssue.severity == "high", 0), (ConsistencyIssue.severity == "medium", 1), else_=2,
)


def _stale() -> ConflictError:
    return ConflictError(
        "问题已被修改，请刷新后重试；当前填写的内容可继续保留", extra={"code": "stale_revision"},
    )


def _evidence(issue: ConsistencyIssue) -> list[dict[str, Any]]:
    return [item for item in issue.evidence or [] if isinstance(item, dict)]


def _chapter_ids(rows: list[ConsistencyIssue]) -> set[int]:
    ids: set[int] = set()
    for issue in rows:
        ids |= issues._evidence_chapters(_evidence(issue))[0]  # noqa: SLF001
    return ids


async def _chapter_info(
    db: AsyncSession, project_id: int, chapter_ids: set[int], *, hashes: bool = True,
) -> dict[int, tuple[int, str, str]]:
    """一次（分块）查询取所涉章节的当前章号、标题与正文指纹；已删除或不属于本作品的章节不在结果里。"""
    result: dict[int, tuple[int, str, str]] = {}
    columns = (Chapter.id, Chapter.chapter_no, Chapter.title, *((Chapter.content,) if hashes else ()))
    for chunk in batched(sorted(chapter_ids), 200):
        rows = await db.execute(select(*columns).where(Chapter.project_id == project_id, Chapter.id.in_(chunk)))
        for row in rows:
            result[row.id] = (row.chapter_no, row.title, text_hash(row.content) if hashes else "")
    return result


def _issue_out(issue: ConsistencyIssue, info: dict[int, tuple[int, str, str]]) -> IssueOut:
    evidence = _evidence(issue)
    stale = issues.evidence_stale(evidence, {chapter_id: value[2] for chapter_id, value in info.items()})
    items = []
    for item, flag in zip(evidence, stale, strict=True):
        chapter_id = item.get("chapter_id")
        chapter_no, title, _hash = info.get(chapter_id, (None, None, ""))
        items.append(EvidenceOut(
            chapter_id=chapter_id,
            chapter_no=chapter_no,
            chapter_title=title,
            fact_id=str(item.get("fact_id") or ""),
            quote=str(item.get("quote") or ""),
            start=item.get("start"),
            end=item.get("end"),
            text_hash=str(item.get("text_hash") or ""),
            stale=flag,
        ))
    return IssueOut(
        id=issue.id,
        project_id=issue.project_id,
        category=issue.category,
        title=issue.title,
        description=issue.description,
        suggestion=issue.suggestion,
        severity=issue.severity,
        status=issue.status,
        note=issue.note,
        status_changed_by=issue.status_changed_by,
        status_changed_at=issue.status_changed_at,
        revision=issue.revision,
        evidence=items,
        reappeared=issue.reappeared,
        needs_recheck=any(stale),
        not_reproduced=issues.not_reproduced(issue),
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


async def list_issues(
    db: AsyncSession, user_id: int, project_id: int, *, status: str | None = None, category: str | None = None,
    chapter_id: int | None = None, needs_recheck: bool | None = None, page: int = 1, page_size: int = 20,
) -> IssuePage:
    await require_project(db, user_id, project_id, "content.read")
    conditions = [ConsistencyIssue.project_id == project_id]
    if status is not None:
        conditions.append(ConsistencyIssue.status == status)
    if category is not None:
        conditions.append(ConsistencyIssue.category == category)
    statement = select(ConsistencyIssue).where(*conditions).order_by(
        _STATUS_ORDER, _SEVERITY_ORDER, ConsistencyIssue.id.desc(),
    )
    offset = (page - 1) * page_size
    if chapter_id is None and needs_recheck is None:
        total = await db.scalar(select(func.count(ConsistencyIssue.id)).where(*conditions)) or 0
        rows = list(await db.scalars(statement.offset(offset).limit(page_size)))
        info = await _chapter_info(db, project_id, _chapter_ids(rows))
    else:
        # 证据在 JSON 里、needs_recheck 取决于当前正文：先取全部候选，批量取指纹后在内存过滤，再分页。
        rows = list(await db.scalars(statement))
        if chapter_id is not None:
            rows = [row for row in rows if chapter_id in issues._evidence_chapters(_evidence(row))[0]]  # noqa: SLF001
        info = None
        if needs_recheck is not None:
            info = await _chapter_info(db, project_id, _chapter_ids(rows))
            hashes = {key: value[2] for key, value in info.items()}
            rows = [row for row in rows if issues.needs_recheck(_evidence(row), hashes) is needs_recheck]
        total = len(rows)
        rows = rows[offset:offset + page_size]
        if info is None:
            info = await _chapter_info(db, project_id, _chapter_ids(rows))
    return IssuePage(items=[_issue_out(row, info) for row in rows], total=total, page=page, page_size=page_size)


async def _issue(db: AsyncSession, project_id: int, issue_id: int) -> ConsistencyIssue:
    issue = await db.scalar(
        select(ConsistencyIssue)
        .where(ConsistencyIssue.id == issue_id, ConsistencyIssue.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    if issue is None:
        raise NotFoundError(ISSUE_NOT_FOUND)
    return issue


async def _detail(db: AsyncSession, project_id: int, issue_id: int) -> IssueOut:
    issue = await _issue(db, project_id, issue_id)
    return _issue_out(issue, await _chapter_info(db, project_id, _chapter_ids([issue])))


async def get_issue(db: AsyncSession, user_id: int, project_id: int, issue_id: int) -> IssueOut:
    await require_project(db, user_id, project_id, "content.read")
    return await _detail(db, project_id, issue_id)


async def update_issue(
    db: AsyncSession, user_id: int, project_id: int, issue_id: int, body: IssueUpdate,
) -> IssueOut:
    """状态与备注的 CAS：持作品行锁（与检查的合并事务串行），修订不符 → 409 code=stale_revision。"""
    await require_project(db, user_id, project_id, "content.edit", for_update=True)
    issue = await _issue(db, project_id, issue_id)
    values: dict[str, Any] = {}
    if "note" in body.model_fields_set:
        values["note"] = body.note
    if "status" in body.model_fields_set and body.status != issue.status:
        # 作者亲自改了状态：「再次出现」的提示随之清除
        values.update(
            status=body.status, status_changed_by=user_id, status_changed_at=datetime.now(UTC), reappeared=False,
        )
    result = await db.execute(
        update(ConsistencyIssue)
        .where(ConsistencyIssue.id == issue_id, ConsistencyIssue.project_id == project_id,
               ConsistencyIssue.revision == body.revision)
        .values(**values, revision=ConsistencyIssue.revision + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _stale()
    await db.commit()
    return await _detail(db, project_id, issue_id)


async def _run_out(db: AsyncSession, run: ConsistencyRun) -> RunOut:
    out = RunOut.model_validate(run)
    if run.status != "succeeded":
        # 运行记录的终态由任务收尾时尽力写入；任务行仍在时以任务的真实终态为准，
        # 任务已不存在或早已结束而记录仍为 running（进程崩溃）时按中断显示。
        task_status = await db.scalar(select(Task.status).where(Task.id == run.task_id)) if run.task_id else None
        if task_status in _STOPPED_STATUSES:
            out.status = task_status
        elif run.status == "running" and task_status not in ACTIVE_TASK_STATUSES:
            out.status = "interrupted"
    if out.stats.skipped_chapters:
        info = await _chapter_info(
            db, run.project_id, {item.chapter_id for item in out.stats.skipped_chapters}, hashes=False,
        )
        for item in out.stats.skipped_chapters:
            item.chapter_no, item.chapter_title, _ = info.get(item.chapter_id, (None, None, ""))
    return out


async def get_run(
    db: AsyncSession, user_id: int, project_id: int, run_id: int, *, notification_id: UUID | None = None,
) -> RunOut:
    await require_project(db, user_id, project_id, "content.read")
    statement = select(ConsistencyRun).where(
        ConsistencyRun.project_id == project_id, ConsistencyRun.id == run_id, ConsistencyRun.status == "succeeded",
    )
    if notification_id is not None:
        statement = statement.where(consistency_run_visible(db, user_id, str(notification_id), project_id, run_id))
    run = await db.scalar(statement)
    if run is None:
        raise NotFoundError("检查记录不存在")
    out = await _run_out(db, run)
    # 历史概要可分享给作品读者。底层错误与比对告警可能包含模型服务响应，只保留计数与跳过章节。
    if out.stats.warnings:
        out.stats.warnings = ["本次检查有提醒，请结合检查范围和统计查看结果。"]
    for chapter in out.stats.skipped_chapters:
        chapter.error = ""
    if run.actor_id != user_id:
        out.task_id = None
    return out


async def summary(db: AsyncSession, user_id: int, project_id: int) -> ConsistencySummary:
    await require_project(db, user_id, project_id, "content.read")
    counts = dict.fromkeys(CONSISTENCY_ISSUE_STATUSES, 0)
    rows = await db.execute(
        select(ConsistencyIssue.status, func.count(ConsistencyIssue.id))
        .where(ConsistencyIssue.project_id == project_id).group_by(ConsistencyIssue.status)
    )
    for status, count in rows:
        if status in counts:
            counts[status] = count
    chapters = [
        chapter for chapter in await db.scalars(select(Chapter).where(Chapter.project_id == project_id))
        if chapter.content.strip()
    ]
    coverage = LedgerCoverage()
    for state in (await ledger.ledger_states(db, project_id, chapters)).values():
        setattr(coverage, state, getattr(coverage, state) + 1)
    running = await check.open_check(db, project_id)
    last_run = await db.scalar(select(ConsistencyRun).where(
        ConsistencyRun.project_id == project_id,
    ).order_by(ConsistencyRun.id.desc()).limit(1))
    return ConsistencySummary(
        last_run=await _run_out(db, last_run) if last_run else None,
        counts_by_status=counts,
        ledger_coverage=coverage,
        running_task_id=running[0] if running is not None else None,
        # 订阅进度需要 task_key；只交给任务的发起者本人
        running_task_key=running[1] if running is not None and running[2] == user_id else None,
    )
