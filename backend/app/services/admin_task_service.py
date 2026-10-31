"""全站现存任务安全投影与取消请求受理；不读取生成内容，不扩大 owner 授权。"""

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import case, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import AdminAuditLog, Project, Task, User
from app.models.entities import ACTIVE_TASK_STATUSES, OPEN_TASK_STATUSES, TASK_KINDS, TASK_STATUSES
from app.schemas.admin_task import (
    AdminTaskBrief,
    AdminTaskCancelOut,
    AdminTaskCancelRequest,
    AdminTaskCounts,
    AdminTaskDetail,
    AdminTaskFilters,
    AdminTaskList,
)
from app.services import task_statistics_service
from app.services.admin_service import _escape_like
from app.services.audit_writer import AuditOperation
from app.services.task_service import STAGE_LABELS

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

KIND_LABELS = {
    "chapter.generate": "AI 生成", "chapter.polish": "AI 润色", "wizard.outline": "大纲向导",
    "wizard.characters": "角色向导", "wizard.world": "世界观向导", "character.generate": "AI 生成角色",
    "organization.generate": "AI 生成组织", "relations.parse": "解析旧关系", "relations.generate": "AI 生成关系",
    "career.generate": "AI 生成职业", "wizard.careers": "职业体系向导", "chapter.analyze": "章节分析",
    "chapter.revise": "按建议重写", "outline.continue": "AI 续写大纲", "outline.expand": "AI 展开细纲",
    "chapter.batch": "批量生成", "kb.index": "建索引", "book.import": "拆书反推", "agent.turn": "项目智能体",
}
# 只允许完全匹配的内部固定文案，绝不截断后透传任意原始阶段。
SAFE_STAGES = frozenset((*STAGE_LABELS.values(), "调用工具", "写入设定", "排队深度分析", "提取写作风格",
                         "汇总设定", "从中断处继续"))
NUMERIC_STAGES = (
    re.compile(r"字数不足，自动续写第 [1-2] 轮"),
    re.compile(r"第 [0-9]{1,8}/[0-9]{1,8} 章 · (?:生成中|分析中|生成失败，第 [0-9]{1,2} 次重试)"),
    re.compile(r"等待确认 [0-9]{1,8} 项修改"),
    re.compile(r"通读 第 [0-9]{1,8}/[0-9]{1,8} 批（第 [0-9]{1,8}(?:–[0-9]{1,8})? 章）"
               r"(?: · 失败，第 [0-9]{1,2} 次重试)?"),
    re.compile(r"(?:汇总设定|提取写作风格) · 失败，第 [0-9]{1,2} 次重试"),
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _task_key(value: str | None) -> str | None:
    try:
        return str(UUID(value)) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def safe_kind(kind: str) -> str:
    return kind if kind in TASK_KINDS else "unknown"


def safe_title(kind: str, task_id: int) -> str:
    return f"{KIND_LABELS.get(kind, '未知任务')} #{task_id}"


def _columns():
    # 不选择 Task 整行：title/input/result/output_text/checkpoint/配置均不进入读取面。
    return (
        Task.id, Task.statistics_key.label("task_key"), Task.kind, Task.status, Task.progress, Task.stage,
        Task.user_id.label("owner_user_id"), User.username.label("owner_username"), Task.project_id,
        Project.title.label("project_title"), Project.deleted_at, Project.deletion_started_at, Task.chapter_id,
        Task.created_at, Task.queued_at, Task.started_at, Task.finished_at, Task.admin_cancel_operation_id,
        Task.admin_cancel_requested_at,
    )


def _base_query():
    return select(*_columns()).select_from(Task).join(User, User.id == Task.user_id).outerjoin(
        Project, Project.id == Task.project_id
    )


def _project(row, engine: "TaskEngine", as_of: datetime) -> AdminTaskBrief:
    status = row["status"] if row["status"] in TASK_STATUSES else "unknown"
    kind = safe_kind(row["kind"])
    task_key = _task_key(row["task_key"])
    pending = row["admin_cancel_operation_id"] is not None
    progress, raw_stage = row["progress"], row["stage"]
    hub = engine.hubs.get(row["id"])
    if hub is not None and task_key is not None and hub.statistics_key == task_key and not hub.closed and (
        status in ACTIVE_TASK_STATUSES
    ):
        progress, raw_stage = hub.progress, hub.stage
    stage = raw_stage if raw_stage in SAFE_STAGES or any(p.fullmatch(raw_stage) for p in NUMERIC_STAGES) else (
        "调用工具" if kind == "agent.turn" and raw_stage.startswith("调用") else STAGE_LABELS.get(status, "未知状态")
    )
    error_code, error_message = None, None
    if pending:
        stage, error_code, error_message = "取消中", "cleanup_pending", "取消请求已受理，安全收尾尚未完成"
    elif status == "cancelled":
        error_code, error_message = "cancelled", "任务已取消"
    elif status == "interrupted":
        error_code, error_message = "interrupted", "任务已中断"
    elif status == "failed":
        error_code, error_message = "execution_failed", "执行失败"
    lifecycle = "none"
    if row["project_id"] is not None:
        lifecycle = "recycled" if row["deleted_at"] is not None else (
            "deleting" if row["deletion_started_at"] is not None else "active"
        )
    queued_at, started_at = _utc(row["queued_at"]), _utc(row["started_at"])
    return AdminTaskBrief(
        id=row["id"], task_key=task_key, kind=kind, title=safe_title(kind, row["id"]), status=status,
        progress=max(0, min(100, progress)), owner_user_id=row["owner_user_id"], owner_username=row["owner_username"],
        project_id=row["project_id"], project_title=row["project_title"], project_lifecycle=lifecycle,
        chapter_id=row["chapter_id"], stage=stage, error_code=error_code, error_message=error_message,
        created_at=_utc(row["created_at"]), queued_at=queued_at, started_at=started_at,
        finished_at=_utc(row["finished_at"]),
        queued_seconds=max(0, int((as_of - queued_at).total_seconds())) if status == "queued" and queued_at else None,
        running_seconds=(max(0, int((as_of - started_at).total_seconds()))
                         if status == "running" and started_at else None),
        cancellation_pending=pending, cancel_requested_at=_utc(row["admin_cancel_requested_at"]),
        can_cancel=status in OPEN_TASK_STATUSES and not pending and task_key is not None,
    )


async def list_tasks(db: AsyncSession, engine: "TaskEngine", filters: AdminTaskFilters) -> AdminTaskList:
    conditions = []
    for field, value in ((Task.user_id, filters.owner_user_id), (Task.project_id, filters.project_id),
                         (Task.id, filters.task_id), (Task.kind, filters.kind)):
        if value is not None:
            conditions.append(field == value)
    if filters.status:
        conditions.append(Task.status.in_(filters.status))
    if filters.from_at is not None:
        conditions.extend((Task.created_at >= filters.from_at, Task.created_at < filters.to_at))
    if filters.q and filters.q.strip():
        pattern = f"%{_escape_like(filters.q.strip())}%"
        conditions.append(or_(User.username.ilike(pattern, escape="\\"), Project.title.ilike(pattern, escape="\\")))
    candidate = _base_query().where(*conditions).cte("filtered_tasks")
    c = candidate.c
    aggregate = select(
        func.count().label("total"),
        *(func.count(case((c.status == status, 1))).label(f"count_{status}") for status in TASK_STATUSES),
        func.count(case((c.status.not_in(TASK_STATUSES), 1))).label("count_unknown"),
    ).select_from(candidate).subquery("task_counts")
    order = [c.created_at.desc(), c.id.desc()]
    if filters.sort != "created_desc":
        state, clock = ("queued", c.queued_at) if filters.sort == "queue_longest" else ("running", c.started_at)
        order = [case((c.status == state, 0), else_=1), case((clock.is_(None), 1), else_=0), clock.asc(), c.id.asc()]
    page = select(candidate).order_by(*order).offset((filters.page - 1) * filters.page_size).limit(
        filters.page_size
    ).subquery("task_page")
    page_order = [page.c.created_at.desc(), page.c.id.desc()]
    if filters.sort != "created_desc":
        clock = page.c.queued_at if filters.sort == "queue_longest" else page.c.started_at
        page_order = [case((page.c.status == state, 0), else_=1), case((clock.is_(None), 1), else_=0),
                      clock.asc(), page.c.id.asc()]
    as_of = task_statistics_service.now_utc()
    rows = (await db.execute(select(aggregate, page).select_from(aggregate.outerjoin(page, true())).order_by(
        *page_order
    ))).mappings().all()
    counts = AdminTaskCounts(**{status: rows[0][f"count_{status}"] for status in (*TASK_STATUSES, "unknown")})
    return AdminTaskList(items=[_project(row, engine, as_of) for row in rows if row["id"] is not None],
                         total=rows[0]["total"], counts=counts, as_of=as_of)


async def get_task(db: AsyncSession, engine: "TaskEngine", task_key: str) -> AdminTaskDetail:
    row = (await db.execute(_base_query().where(Task.statistics_key == task_key))).mappings().first()
    if row is None:
        raise NotFoundError("任务不存在或已移除")
    as_of = task_statistics_service.now_utc()
    return AdminTaskDetail(**_project(row, engine, as_of).model_dump(), as_of=as_of)


async def cancel_task(db: AsyncSession, engine: "TaskEngine", actor: User, task_key: str,
                      data: AdminTaskCancelRequest) -> AdminTaskCancelOut:
    # 真实写锁复核当前身份；不能让 AuditOperation 的 enabled=False 放行未审计业务。
    owner_id = await db.scalar(select(Task.user_id).where(Task.statistics_key == task_key))
    # 两个管理员互相处置任务时也按用户ID排序锁定，避免actor→owner反向取锁。
    for user_id in sorted({actor.id, owner_id} - {None}):
        conditions = [User.id == user_id]
        if user_id == actor.id:
            conditions.extend((User.is_admin.is_(True), User.is_active.is_(True),
                               User.token_version == actor.token_version))
        permission = await db.execute(update(User).where(*conditions).values(id=User.id).execution_options(
            synchronize_session=False
        ))
        if user_id == actor.id and permission.rowcount != 1:
            await db.rollback()
            raise ForbiddenError("需要管理员权限")
    async with AuditOperation(db, actor, "task.cancel_request", "task", include_own_project=True) as audit:
        task = await db.scalar(select(Task).where(Task.statistics_key == task_key).execution_options(
            populate_existing=True
        ))
        if task is None:
            raise NotFoundError("任务不存在或已移除")
        # 目标owner已通过上面的父用户写屏障与删号串行。
        audit.target(task, label=safe_title(task.kind, task.id))
        await audit.owner(task.user_id)
        audit.values.update(project_id=task.project_id, chapter_id=task.chapter_id)
        if task.project_id is not None:
            audit.values["project_title"] = await db.scalar(select(Project.title).where(Project.id == task.project_id))
        audit.summary.task_key = UUID(task_key)
        audit.summary.task_kind = safe_kind(task.kind)
        audit.summary.cancel_reason = data.reason
        # 条件写遇到状态竞争重读同UUID；最多null→一个pending，不跨目标重试。
        while True:
            if task.admin_cancel_operation_id is not None:
                operation_id = task.admin_cancel_operation_id
                log_id = await db.scalar(select(AdminAuditLog.id).where(AdminAuditLog.operation_id == operation_id))
                await db.rollback()
                return AdminTaskCancelOut(operation_id=operation_id, audit_log_id=log_id)
            if task.status not in OPEN_TASK_STATUSES:
                raise ConflictError("任务已结束，无法受理取消请求")
            previous = task.status
            changed = await db.execute(update(Task).where(
                Task.id == task.id, Task.statistics_key == task_key, Task.status == previous,
                Task.admin_cancel_operation_id.is_(None),
            ).values(admin_cancel_operation_id=audit.operation_id,
                     admin_cancel_requested_at=task_statistics_service.now_utc()).execution_options(
                         synchronize_session=False
                     ))
            if changed.rowcount == 1:
                break
            task = await db.scalar(select(Task).where(Task.statistics_key == task_key).execution_options(
                populate_existing=True
            ))
            if task is None:
                raise NotFoundError("任务不存在或已移除")
        audit.summary.accepted_from_status = previous
        await audit.commit()
        accepted = AdminTaskCancelOut(operation_id=audit.operation_id, audit_log_id=audit.row.id)
    # 到此只有安全primitive；不再refresh/读取已过期ORM。断开不能取消这个独立后台工作。
    engine.request_admin_cancel(task_key, accepted.operation_id)
    return accepted
