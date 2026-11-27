"""后台任务的 API 入口：创建、列表、详情、取消、重试、继续 / 结束（待审核）、删除、清理与保留裁剪。

所有入口按 user_id 校验归属；他人任务与不存在统一返回 404。
handler 可选钩子（getattr 探测）：
- validate_retry(db, ctx, *, user_id, task) -> TaskSpec：重试时代替 validate_and_claim（多步任务沿用原范围与断点）；
- revalidate_resume(db, ctx, task)：待审核任务继续前重新校验，业务错误抛 AppError；
- finish_result(task) -> dict：待审核任务被结束时的最终 result；
- checkpoint_out(task) -> dict | None：TaskOut.checkpoint 的取值（默认原样返回断点；断点体积大的类型只给摘要）。
"""

import asyncio
import bisect
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import String, and_, cast, delete, event, exists, false, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

import app.services.task_engine.handlers  # noqa: F401  导入即注册内置任务类型
from app.core.errors import ConflictError, NotFoundError
from app.models import AgentChange, Chapter, Project, Task, TaskWorkRecord, User
from app.models.entities import ACTIVE_TASK_STATUSES, FINISHED_TASK_STATUSES, OPEN_TASK_STATUSES
from app.schemas.rich_document_review import ReviewIdentity
from app.schemas.scene_ai import SceneTarget, parse_target
from app.schemas.scene_restructure import RestructureTaskRequest
from app.schemas.skill import SkillRef
from app.schemas.task import TaskOut
from app.services import authorization_service as authz
from app.services import project_access, project_lifecycle, task_statistics_service
from app.services.collaboration_task_access import GRANT_KEY, task_grant_identity, task_visible_to_actor
from app.services.project_service import get_owned_project
from app.services.task_engine import cleanup_work
from app.services.task_engine.drain import require_task_admission_open
from app.services.task_engine.owner_cancellation import unproven_execution
from app.services.task_engine.registry import get_handler

if TYPE_CHECKING:
    from app.services.task_engine.admission import TaskAdmission
    from app.services.task_engine.engine import AbortSnapshot, TaskEngine

# 每个用户保留的已结束任务数
MAX_FINISHED_PER_USER = 200
RETRYABLE_STATUSES = ("failed", "cancelled", "interrupted")
STAGE_LABELS = {
    "queued": "排队中",
    "running": "生成中",
    "paused": "待审核",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}
ACTIVE_CONFLICT = "已有任务在进行中，请等待其完成后再试"
_UNSET = object()
_TASK_ACTOR_TOKEN_VERSION = "_task_actor_token_version"
_AGENT_ACTOR_TOKEN_VERSION = "_agent_actor_token_version"
_WIZARD_ACTOR_TOKEN_VERSION = "_wizard_actor_token_version"
_SERVER_TASK_KEYS = frozenset({
    _TASK_ACTOR_TOKEN_VERSION, _AGENT_ACTOR_TOKEN_VERSION, _WIZARD_ACTOR_TOKEN_VERSION,
    "_collaboration_grant", "_project_model", "_shared_embedding", "_chapter_claim", "_batch_attempt",
    "_batch_completion", "_batch_outcome_unknown", "_analysis_pending", "_analysis_completion",
    "_analysis_proposal_source", "_wizard_pending",
    "_wizard_completion", "_external_execution", "_external_snapshot_mac", "_admin_chapter_cancel",
    "_admin_chapter_receipt", "_manual_memory_ref_ids",
})


def _contains_reserved_task_key(value: Any) -> bool:
    if isinstance(value, dict):
        return (bool(value.keys() & _SERVER_TASK_KEYS)
                or any(_contains_reserved_task_key(item) for item in value.values()))
    if isinstance(value, list):
        return any(_contains_reserved_task_key(item) for item in value)
    return False


def _has_wizard_receipt(task: Task) -> bool:
    return any(isinstance(value, dict) and any(key in value for key in ("_wizard_pending", "_wizard_completion"))
               for value in (task.input, task.checkpoint))


def _next_queued_at(task: Task):
    stamp = task_statistics_service.now_utc()
    previous = task.queued_at
    if previous is not None:
        previous = previous.replace(tzinfo=UTC) if previous.tzinfo is None else previous.astimezone(UTC)
        stamp = max(stamp, previous + timedelta(microseconds=1))
    return stamp


def visible_project_condition():
    return or_(
        Task.project_id.is_(None),
        Project.id.is_not(None) & Project.deleted_at.is_(None) & Project.deletion_started_at.is_(None),
    )


def visible_task_project_exists():
    return or_(Task.project_id.is_(None), exists(
        select(Project.id).where(Project.id == Task.project_id, *project_lifecycle.active_conditions())
    ))


def _without_executing_agent_change(db: AsyncSession):
    """同一 DELETE CAS 中保留仍执行或结果未知的外部操作证据。"""
    keys = ("_external_outcome_unknown", "_external_claim_id", "_external_task_key", "_external_execution",
            "_external_snapshot_mac")
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        markers = [func.json_type(AgentChange.result, f'$."{key}"').is_not(None) for key in keys]
    elif dialect == "postgresql":
        markers = [func.json_typeof(AgentChange.result[key]).is_not(None) for key in keys]
    else:
        # 没验证 JSON 键存在性的数据库不能误删恢复证据。
        return false()
    linked = aliased(Task)
    proven_link = exists(select(linked.id).where(
        linked.project_id == AgentChange.project_id,
        or_(linked.statistics_key == AgentChange.turn_task_key,
            linked.statistics_key == AgentChange.result["_external_task_key"].as_string()),
    ).correlate(AgentChange))
    return ~exists(select(AgentChange.id).where(
        or_(AgentChange.status == "executing", *markers),
        or_(AgentChange.turn_task_id == Task.id, AgentChange.turn_task_key == Task.statistics_key,
            AgentChange.result["_external_task_key"].as_string() == Task.statistics_key,
            and_(AgentChange.project_id == Task.project_id, ~proven_link)),
    ).correlate(Task))


def _without_unresolved_task_receipt(db: AsyncSession):
    """默认保留所有内部占用和收据；成功凭据须走逐条验证才能放行。"""
    keys = ("_analysis_pending", "_analysis_completion", "_chapter_claim", "_batch_completion", "rich_analysis",
            "_external_execution", "_external_snapshot_mac", "_batch_outcome_unknown", "_wizard_pending",
            "_wizard_completion")
    input_keys = ("_chapter_claim", "_admin_chapter_cancel", "_admin_chapter_receipt", "_external_execution",
                  "_external_snapshot_mac", "_batch_outcome_unknown", "_wizard_pending", "_wizard_completion")
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        present = {key: func.json_type(Task.checkpoint, f'$."{key}"').is_not(None) for key in keys}
        input_present = [func.json_type(Task.input, f'$."{key}"').is_not(None) for key in input_keys]
        batch_current_type = func.json_type(Task.checkpoint, '$."current"')
        input_type, checkpoint_type = func.json_type(Task.input), func.json_type(Task.checkpoint)
    elif dialect == "postgresql":
        present = {key: func.json_typeof(Task.checkpoint[key]).is_not(None) for key in keys}
        input_present = [func.json_typeof(Task.input[key]).is_not(None) for key in input_keys]
        batch_current_type = func.json_typeof(Task.checkpoint["current"])
        input_type, checkpoint_type = func.json_typeof(Task.input), func.json_typeof(Task.checkpoint)
    else:
        return false()
    uncertain = or_(
        input_type.is_(None), input_type != "object",
        and_(checkpoint_type.is_not(None), checkpoint_type.not_in(("object", "null"))),
        *input_present,
        *present.values(),
        and_(Task.kind == "chapter.batch", batch_current_type.is_not(None), batch_current_type != "null"),
    )
    return ~uncertain


async def has_unresolved_receipt(db: AsyncSession, task: Task) -> bool:
    """调用方先持关联项目锁；有内部凭据时锁 Task 并验证原成功事实。"""
    try:
        if db.is_modified(task, include_collections=False):
            return True
        original = (task.id, task.statistics_key, task.user_id, task.project_id, task.chapter_id, task.kind,
                    task.status, task.queued_at, task.execution_key, task.admin_cancel_operation_id, task.resource_key,
                    task.step_cursor, json.dumps(task.result, sort_keys=True, separators=(",", ":")),
                    json.dumps(task.input, sort_keys=True, separators=(",", ":")),
                    json.dumps(task.checkpoint, sort_keys=True, separators=(",", ":")))
        with db.no_autoflush:
            row = await db.scalar(select(Task).where(
                Task.id == original[0], Task.statistics_key == original[1],
            ).with_for_update().execution_options(populate_existing=True))
        if row is None:
            return True
        current = (row.id, row.statistics_key, row.user_id, row.project_id, row.chapter_id, row.kind,
                   row.status, row.queued_at, row.execution_key, row.admin_cancel_operation_id, row.resource_key,
                   row.step_cursor, json.dumps(row.result, sort_keys=True, separators=(",", ":")),
                   json.dumps(row.input, sort_keys=True, separators=(",", ":")),
                   json.dumps(row.checkpoint, sort_keys=True, separators=(",", ":")))
        if current != original:
            return True
        if not isinstance(row.input, dict) or (row.checkpoint is not None and not isinstance(row.checkpoint, dict)):
            return True
        payload = row.input if isinstance(row.input, dict) else {}
        if any(key in payload for key in ("_chapter_claim", "_admin_chapter_cancel", "_admin_chapter_receipt",
                                          "_external_execution", "_external_snapshot_mac", "_batch_outcome_unknown",
                                          "_wizard_pending", "_wizard_completion")):
            return True
        checkpoint = row.checkpoint if isinstance(row.checkpoint, dict) else {}
        markers = {key for key in ("_analysis_pending", "_analysis_completion", "_chapter_claim",
                                   "_batch_completion", "rich_analysis", "_external_execution",
                                   "_external_snapshot_mac", "_batch_outcome_unknown", "_wizard_pending",
                                   "_wizard_completion") if key in checkpoint}
        if row.kind == "chapter.batch" and checkpoint.get("current") is not None:
            return True
        if not markers:
            return False
        if (row.status != "succeeded" or row.execution_key is not None or row.admin_cancel_operation_id is not None
                or row.project_id is None):
            return True
        if row.kind == "chapter.analyze" and markers == {"_analysis_completion"}:
            from app.services import analysis_service

            verified = await analysis_service.completed_terminal_analysis_receipt(db, row)
            return verified is None or json.dumps(verified[1], sort_keys=True, separators=(",", ":")) \
                != json.dumps(row.result, sort_keys=True, separators=(",", ":"))
        if row.kind in ("chapter.generate", "chapter.polish", "chapter.revise") and markers == {"_chapter_claim"}:
            from app.services import generation_service

            validate = getattr(generation_service, "completed_terminal_chapter_receipt", None)
            return validate is None or await validate(db, row) is None
        if row.kind.startswith("wizard.") and markers == {"_wizard_completion"}:
            from app.services import wizard_service

            validate = getattr(wizard_service, "verified_wizard_terminal_receipt", None)
            # 协作者任务（E4-g3c）按数据库中的作品作者核对原 grant 绑定；不能由任务输入自报作者。
            owner_id = await db.scalar(select(Project.user_id).where(Project.id == row.project_id))
            return validate is None or not validate(row, project_owner_id=owner_id)
        return True
    except Exception:  # noqa: BLE001  故障或损坏不证明完成，也不回显内部字段。
        return True


async def _receipt_cleanup_condition(db: AsyncSession, task: Task):
    """仅原有效成功记录可越过默认阻断；CAS 锁定完整 input/checkpoint 原文。"""
    if await has_unresolved_receipt(db, task):
        return false()
    raw = (await db.execute(select(
        cast(Task.input, String).label("input_raw"), cast(Task.checkpoint, String).label("checkpoint_raw"),
        cast(Task.result, String).label("result_raw"),
    ).where(Task.id == task.id, Task.statistics_key == task.statistics_key).with_for_update())).one_or_none()
    if raw is None:
        return false()
    exact = and_(
        Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
        Task.project_id == task.project_id, Task.kind == task.kind, Task.status == task.status,
        Task.queued_at == task.queued_at, Task.execution_key == task.execution_key,
        Task.step_cursor == task.step_cursor, Task.resource_key == task.resource_key,
        cast(Task.input, String) == raw[0] if raw[0] is not None else Task.input.is_(None),
        cast(Task.checkpoint, String) == raw[1] if raw[1] is not None else Task.checkpoint.is_(None),
        cast(Task.result, String) == raw[2] if raw[2] is not None else Task.result.is_(None),
    )
    return or_(_without_unresolved_task_receipt(db), exact)


# ---------------- 序列化 ----------------


def _request_dict(task: Task) -> dict[str, Any]:
    """历史JSON不保证是对象；只读投影不能修写或把损坏target当合法目标。"""
    request = (task.input or {}).get("request") if isinstance(task.input, dict) else None
    return request if isinstance(request, dict) else {}


def _request_int(task: Task, key: str) -> int | None:
    value = _request_dict(task).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _subject_character_id(task: Task) -> int | None:
    return _request_int(task, "character_id")


def _revise_analysis_id(task: Task) -> int | None:
    return _request_int(task, "analysis_id") if task.kind == "chapter.revise" else None


def _skill_ref(task: Task) -> SkillRef | None:
    snap = (task.input or {}).get("skill") if isinstance(task.input, dict) else None
    if not isinstance(snap, dict):
        return None
    # model_copy(update=) 不做校验：这里先构造成 SkillRef，结构不对的旧数据直接不返回
    try:
        return SkillRef.model_validate(snap)
    except ValidationError:
        return None


def _live_output_hub(engine: "TaskEngine | None", task: Task):
    if engine is None or task.status != "running":
        return None
    return engine.live_output_hub(
        task.id, task_key=task.statistics_key, status=task.status, queued_at=task.queued_at,
        execution_key=task.execution_key, worker_key=task.worker_key,
        batch_cursor=task.step_cursor if task.kind == "chapter.batch" else None,
    )


async def result_unproven(db: AsyncSession, task: Task) -> bool:
    if task.status != "running":
        return False
    if task.recovered_at is not None:
        return True
    with db.no_autoflush:
        return await db.scalar(select(Task.id).where(
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.queued_at == task.queued_at,
            Task.execution_key == task.execution_key, Task.status == "running", unproven_execution(),
        )) is not None


def to_out(
    task: Task, *, queue_position: int | None = None, engine: "TaskEngine | None" = None, unproven: bool = False,
    use_hub: bool = True,
) -> TaskOut:
    out = TaskOut.model_validate(task)
    try:
        target = parse_target(_request_dict(task).get("target"))
    except ValidationError:
        target = None
    scene_id = (target.scene_id if isinstance(target, SceneTarget) and task.kind in (
        "chapter.generate", "chapter.polish", "chapter.revise",
    ) else None)
    if task.kind == "scene.restructure":
        try:
            source = RestructureTaskRequest.model_validate(_request_dict(task))
        except ValidationError:
            pass
        else:
            if (source.project_id, source.chapter_id) == (task.project_id, task.chapter_id):
                scene_id = source.scene_ids[0]
    update_fields: dict[str, Any] = {
        "queue_position": queue_position if task.status == "queued" else None,
        "character_id": _subject_character_id(task),
        "analysis_id": _revise_analysis_id(task),
        "scene_id": scene_id if type(scene_id) is int and scene_id > 0 else None,
        "outline_id": _request_int(task, "outline_id") if task.kind == "outline.expand" else None,
        "skill": _skill_ref(task),
    }
    review = (task.checkpoint or {}).get("rich_review") if isinstance(task.checkpoint, dict) else None
    if (isinstance(review, dict) and task.statistics_key is not None and review.get("phase") in
            ("awaiting_review", "applied", "discarded") and all(
                isinstance(review.get(key), str) for key in ("run_key", "token")
            ) and type(review.get("chapter_id")) is int):
        update_fields["rich_review"] = {"task_key": task.statistics_key, "run_key": review["run_key"],
                                        "token": review["token"], "chapter_id": review["chapter_id"],
                                        "phase": review["phase"]}
    blocked = getattr(get_handler(task.kind), "retry_blocked_reason", None)
    if blocked is not None:
        update_fields["retry_blocked_reason"] = blocked(task)
    checkpoint_out = getattr(get_handler(task.kind), "checkpoint_out", None)
    if checkpoint_out is not None:
        update_fields["checkpoint"] = checkpoint_out(task)
    hub = _live_output_hub(engine, task) if use_hub else None
    if hub is not None:
        # 进度 / 阶段以内存中的实时值为准（库里是节流刷新的）
        update_fields.update(progress=hub.progress, stage=hub.stored_stage)
    if task.status == "running" and (unproven or task.recovered_at is not None or (
        engine is not None and engine.execution_is_uncertain(task)
    )):
        update_fields.update(stage="结果待核对", error="任务保存结果尚未确认，请稍后核对")
    return out.model_copy(update=update_fields)


def out_json(task: Task, *, queue_position: int | None = None) -> dict[str, Any]:
    return to_out(task, queue_position=queue_position).model_dump(mode="json")


async def _queued_ids(db: AsyncSession) -> list[int]:
    with db.no_autoflush:
        return list(await db.scalars(select(Task.id).where(Task.status == "queued").order_by(Task.id)))


async def queue_position(db: AsyncSession, task: Task) -> int | None:
    if task.status != "queued":
        return None
    with db.no_autoflush:
        return await db.scalar(select(func.count(Task.id)).where(Task.status == "queued", Task.id < task.id))


async def task_out(db: AsyncSession, engine: "TaskEngine | None", task: Task) -> TaskOut:
    task_key = task.statistics_key
    active = task.status in OPEN_TASK_STATUSES
    grant = task_grant_identity(task.input) if active else _UNSET
    queued_at = task.queued_at if active else _UNSET
    use_hub = (
        engine is not None and _live_output_hub(engine, task) is not None and await engine.lease.confirms_output(db)
    )
    out = to_out(task, queue_position=await queue_position(db, task), engine=engine,
                 unproven=await result_unproven(db, task), use_hub=use_hub)
    return await project_task_out(
        db, task.user_id, out, active_grant_required=active, task_key=task_key,
        expected_grant=grant, expected_queued_at=queued_at,
    )


async def _require_visible_task_result(
    db: AsyncSession, user_id: int, task_id: int, project_id: int | None, *,
    active_grant_required: bool = False, task_key: str | None | object = _UNSET,
    expected_grant: Any = _UNSET, expected_queued_at: Any = _UNSET,
) -> None:
    statement = select(Task.id, Task.input, Task.queued_at).where(
        Task.id == task_id, Task.project_id == project_id,
        task_visible_to_actor(db, user_id, active_grant_required=active_grant_required),
    )
    if task_key is not _UNSET:
        statement = statement.where(Task.statistics_key == task_key)
    with db.no_autoflush:
        row = (await db.execute(statement)).first()
    if (row is None or (expected_grant is not _UNSET and task_grant_identity(row.input) != expected_grant)
            or (expected_queued_at is not _UNSET and row.queued_at != expected_queued_at)):
        # 协作撤销继续隐藏任务；登录凭据失效则按认证契约返回 401。
        await authz.require_authenticated(db, user_id)
        raise NotFoundError("任务不存在")


async def project_task_out(
    db: AsyncSession, user_id: int, out: TaskOut, *, active_grant_required: bool = False,
    task_key: str | None | object = _UNSET,
    expected_grant: Any = _UNSET, expected_queued_at: Any = _UNSET,
) -> TaskOut:
    """任务公共投影中的私人引用仅保留本人所有且属于同作品的记录。"""
    if out.project_id is not None:
        from app.services.personal_projection import project_payload

        safe = await project_payload(db, user_id, out.project_id, out.model_dump(mode="python"))
        out = TaskOut.model_validate(safe)
    # 投影本身需要 await；投影结束后按原活动状态与轮次再次验证，防止读取期间降级或换邀请。
    await _require_visible_task_result(
        db, user_id, out.id, out.project_id, active_grant_required=active_grant_required, task_key=task_key,
        expected_grant=expected_grant, expected_queued_at=expected_queued_at,
    )
    # 唯一可信值来自最终 SQL 复核的本人原绑定，不能采用投影中的其他任务引用。
    return out.model_copy(update={"task_key": task_key if isinstance(task_key, str) else None})


async def out_json_live(db: AsyncSession, engine: "TaskEngine", task: Task) -> dict[str, Any]:
    return (await task_out(db, engine, task)).model_dump(mode="json")


# ---------------- 创建 / 重试 ----------------


def _nested_sync(db: AsyncSession) -> object | None:
    nested = db.get_nested_transaction()
    return None if nested is None else nested.sync_transaction


class _CallerTransactionSession:
    """调用方持有根事务时的受理视图：提交降为 flush，失败由本次受理保存点整体回滚。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    def __getattr__(self, name: str) -> Any:
        return getattr(self.db, name)

    async def commit(self) -> None:
        await self.db.flush()

    async def rollback(self) -> None:
        # 不得回滚调用方的根事务；异常离开受理保存点时由保存点回滚本次写入。
        pass


@asynccontextmanager
async def _admission_validation_transaction(
    db: AsyncSession, actor: authz.AuthenticatedActor, *, savepoint: object | None = None,
):
    """受理 handler 不能提前提交锁内已接受的身份，或把锁换成新事务。

    savepoint 仅供调用方持有根事务的受理：必须恰为本次受理开启的保存点，期间任何提交均拒绝。
    """
    transaction = db.get_transaction()
    if (transaction is None or not transaction.is_active or _nested_sync(db) is not savepoint
            or db.info.get("authenticated_actor") != actor):
        raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})
    connection = await db.connection()
    physical = connection.sync_connection
    original = (transaction.sync_transaction, physical, physical.get_transaction(),
                physical.connection.dbapi_connection)
    if connection.closed or connection.invalidated or original[2] is None:
        raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})

    def reject_session_commit(session) -> None:
        if savepoint is not None or session.get_nested_transaction() is None:
            raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})

    def reject_connection_commit(_connection) -> None:
        raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})

    event.listen(db.sync_session, "before_commit", reject_session_commit)
    event.listen(physical, "commit", reject_connection_commit)
    try:
        yield
    finally:
        event.remove(db.sync_session, "before_commit", reject_session_commit)
        event.remove(physical, "commit", reject_connection_commit)
    transaction = db.get_transaction()
    if (transaction is None or not transaction.is_active or transaction.sync_transaction is not original[0]
            or _nested_sync(db) is not savepoint or db.info.get("authenticated_actor") != actor):
        raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})
    connection = await db.connection()
    physical = connection.sync_connection
    if (connection.closed or connection.invalidated or physical is not original[1]
            or physical.get_transaction() is not original[2]
            or physical.connection.dbapi_connection is not original[3]):
        raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})


async def create_task(
    db: AsyncSession,
    engine: "TaskEngine",
    user_id: int,
    kind: str,
    params: dict[str, Any],
    *,
    retry_of: Task | None = None,
    admission: "TaskAdmission | None" = None,
    caller_transaction: bool = False,
) -> TaskOut:
    """同步校验并抢占资源，与任务插入同一事务提交；同资源已有活动任务 → 409。

    caller_transaction=True 供已持根事务的智能体提案确认和一键分析：携带锁外准备的受理证明，
    在根事务中消费并重选模型后，于本次保存点内校验、插入并绑定占用；不提交调用方事务，
    失败只回滚该保存点。模型重选失败已回滚根事务时直接抛出，由调用方整体失败关闭。
    """
    token_key = (_AGENT_ACTOR_TOKEN_VERSION if kind == "agent.turn" else _WIZARD_ACTOR_TOKEN_VERSION
                 if kind in ("wizard.outline", "wizard.characters", "wizard.world", "wizard.careers") else None)
    # 必须来自实际认证请求或已核验的可信派生，不能由当前 User 行或客户端参数补造。
    actor = authz.actor_snapshot(db, user_id)
    if (_contains_reserved_task_key(params) or not isinstance(actor, authz.AuthenticatedActor)
            or actor.id != user_id or type(actor.token_version) is not int or actor.token_version < 0):
        raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    if caller_transaction and (
        retry_of is not None or admission is None or not isinstance(db, AsyncSession)
        or db.get_transaction() is None or db.get_nested_transaction() is not None
        or getattr(get_handler(kind), "admission_preflight_aware", False) is not True
    ):
        raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})
    actor_version = actor.token_version
    if retry_of is not None:
        old = retry_of.input.get(_TASK_ACTOR_TOKEN_VERSION) if isinstance(retry_of.input, dict) else None
        alias = retry_of.input.get(token_key) if token_key is not None and isinstance(retry_of.input, dict) else None
        if (type(old) is not int or old < 0 or old != actor_version
                or (token_key is not None and (type(alias) is not int or alias != old))):
            raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    await authz.require_authenticated(db, actor, write=True)
    await require_task_admission_open(db)
    handler = get_handler(kind)
    preflight = accepted_model = None
    admission_aware = getattr(handler, "admission_preflight_aware", False) is True
    if admission_aware or admission is not None:
        from app.services import project_ai_config
        from app.services.task_engine.admission import consume_task_admission

        if not admission_aware:
            raise ConflictError("任务类型尚未接入受控受理")
        preflight = await consume_task_admission(
            db, admission, actor=actor, kind=kind, params=params, retry_of=retry_of,
        )
        if getattr(project_ai_config, "is_original_model_preflight", lambda _: False)(preflight):
            accept_original = getattr(project_ai_config, "accept_original_preflight_project_model", None)
            if accept_original is None:
                raise ConflictError("原模型重试尚未就绪，请重新发起")
            accepted_model = await accept_original(
                db, preflight, admission=admission, cipher=engine.ctx.cipher,
                allow_private=engine.ctx.settings.allow_private_ai_endpoints,
            )
        else:
            accepted_model = await project_ai_config.accept_preflight_project_model(
                db, preflight, cipher=engine.ctx.cipher, allow_private=engine.ctx.settings.allow_private_ai_endpoints,
            )
    async def admit(db, savepoint: object | None = None) -> Task:
        validate_retry = getattr(handler, "validate_retry", None) if retry_of is not None else None
        # 原来源证明由中央在本事务中恰好消费一次；handler 接收该已受理绑定，
        # 并与原 Task 的服务端绑定逐项比较，不得再次消费或重新选择默认模型。
        options = {"admission": preflight, "accepted_model": accepted_model} if admission_aware else {}
        async def validate():
            if validate_retry is not None:
                return await validate_retry(db, engine.ctx, user_id=user_id, task=retry_of, **options)
            return await handler.validate_and_claim(
                db, engine.ctx, user_id=user_id, kind=kind, params=params, **options,
            )

        if admission_aware:
            async with _admission_validation_transaction(raw, actor, savepoint=savepoint):
                spec = await validate()
        else:
            spec = await validate()
        if spec.project_id is not None:
            if accepted_model is not None and spec.project_id != accepted_model.project_id:
                await db.rollback()
                raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})
            if (accepted_model is not None and getattr(handler, "execution_identity_aware", False) is True
                    and getattr(handler, "collaboration_execution_ready", False) is True):
                await project_access.require_project(
                    db, user_id, spec.project_id, "ai.use", for_update=True, visibility_write=True,
                    grant_identity=accepted_model.grant_identity,
                )
            elif (accepted_model is None
                  and (retry_of is None or (GRANT_KEY in (retry_of.input or {})
                                            and task_grant_identity(retry_of.input) == task_grant_identity(spec.input)))
                  and getattr(handler, "shared_embedding_admission", False) is True
                  and isinstance(spec.input, dict) and GRANT_KEY in spec.input):
                # 不依赖文本模型的协作受理（kb.index，E6-a）：轮次由 handler 锁内重读写入，此处按同一轮次再锁作品。
                spec_grant = task_grant_identity(spec.input)
                if spec_grant is None:
                    await db.rollback()
                    raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})
                await project_access.require_project(
                    db, user_id, spec.project_id, "ai.use", for_update=True, visibility_write=True,
                    grant_identity=spec_grant,
                )
            else:
                await get_owned_project(db, user_id, spec.project_id, lock=True)
        checkpoint = deepcopy(retry_of.checkpoint) if retry_of is not None and retry_of.checkpoint is not None else None
        if isinstance(checkpoint, dict):
            # 重试是新的 task UUID/执行轮次，不能复制旧提案身份。
            checkpoint.pop("rich_review", None)
        task_input = deepcopy(spec.input)
        if accepted_model is not None:
            from app.services import project_ai_config
            from app.services.collaboration_task_access import bind_task_grant

            binding = project_ai_config.serialize_project_model(accepted_model)
            if "_project_model" in task_input and task_input["_project_model"] != binding:
                await db.rollback()
                raise ConflictError("任务模型绑定已变化，请重新发起", extra={"code": "task_admission_changed"})
            task_input = bind_task_grant(task_input, accepted_model.grant_identity)
            task_input["_project_model"] = binding
        task_input[_TASK_ACTOR_TOKEN_VERSION] = actor_version
        if token_key is not None:
            task_input[token_key] = actor_version
        task = Task(
            user_id=user_id,
            project_id=spec.project_id,
            chapter_id=spec.chapter_id,
            kind=kind,
            title=spec.title[:200],
            resource_key=spec.resource_key,
            serial_key=spec.serial_key,
            status="queued",
            input=task_input,
            stage=STAGE_LABELS["queued"],
            retry_of_id=retry_of.id if retry_of is not None else spec.retry_of_id,
            step_cursor=retry_of.step_cursor if retry_of is not None else 0,
            checkpoint=checkpoint,
        )
        db.add(task)
        try:
            await task_statistics_service.record_created(db, task)
            if kind in ("chapter.generate", "chapter.polish", "chapter.revise"):
                from app.services.task_engine.handlers import chapter as chapter_handler

                # 只有 Task 的 UUID 和入队代次已经持久化到本事务后才建立章节占用。
                await chapter_handler.bind_queued_claim(db, task)
            if kind == "agent.turn" and retry_of is None:
                from app.services.task_engine.handlers import agent as agent_handler

                # 用户消息与实际接收的 Task 身份必须在同一事务绑定。
                await agent_handler.bind_queued_turn(db, task)
            if kind == "kb.index" and _request_dict(task).get("mode") == "document":
                from app.services import kb_index_service

                await kb_index_service.bind_queued_document_claim(
                    db, task.id, task.statistics_key, queue_claim_key=str(uuid4()),
                )
            await db.commit()
        except IntegrityError as exc:
            # 部分唯一索引 uq_tasks_active_resource 兜底：同资源并发提交（章节抢占随回滚撤销）
            await db.rollback()
            if _active_resource_conflict(exc):
                raise ConflictError(ACTIVE_CONFLICT) from exc
            raise
        except BaseException:
            await db.rollback()
            raise
        return task

    raw = db
    if caller_transaction:
        async with db.begin_nested() as nested:
            task = await admit(_CallerTransactionSession(db), nested.sync_transaction)
    else:
        task = await admit(db)
    await refresh_same_task(db, task)
    if not caller_transaction and task.status in ACTIVE_TASK_STATUSES:
        engine.ensure_hub(
            task.id, statistics_key=task.statistics_key, status=task.status,
            text=task.output_text, progress=task.progress, stage=task.stage, queued_at=task.queued_at,
            execution_key=task.execution_key,
        )
    if not caller_transaction:
        engine.notify_queue_changed()
    return await task_out(db, engine, task)


async def retry_task(
    db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int, *, expected_task_key: str | None = None,
    admission: "TaskAdmission | None" = None,
) -> TaskOut:
    task = await get_owned_task(db, user_id, task_id, expected_task_key=expected_task_key)
    grant, queued_at = task_grant_identity(task.input), task.queued_at
    await _lock_user_task_project(
        db, user_id, task, active_grant_required=True, expected_grant=grant, expected_queued_at=queued_at,
        visibility_write=(task.kind in ("chapter.generate", "chapter.polish", "chapter.revise")
                          or getattr(get_handler(task.kind), "admission_preflight_aware", False) is True
                          # 协作 kb.index 重试在同一事务重新受理（需发送写门禁），门禁须先于作品父行锁。
                          or getattr(get_handler(task.kind), "shared_embedding_admission", False) is True),
    )
    if task.admin_cancel_operation_id is not None or task.execution_key is not None:
        raise ConflictError("取消请求正在安全收尾，不能重试")
    if task.status not in RETRYABLE_STATUSES:
        raise ConflictError("只有失败、已取消或已中断的任务可以重试")
    if (task.kind in ("chapter.generate", "chapter.polish", "chapter.revise")
            and isinstance(task.checkpoint, dict) and "_chapter_claim" in task.checkpoint):
        raise ConflictError("章节保存结果尚未核对，不能重试")
    if (task.kind == "chapter.analyze" and isinstance(task.checkpoint, dict)
            and any(key in task.checkpoint for key in ("_analysis_pending", "_analysis_completion"))):
        raise ConflictError("分析保存结果尚未核对，不能重试")
    if any(isinstance(value, dict) and "_batch_outcome_unknown" in value for value in (task.input, task.checkpoint)):
        raise ConflictError("批量执行结果尚未核对，不能重试")
    if _has_wizard_receipt(task):
        raise ConflictError("向导执行结果尚未核对，不能重试")
    request = task.input.get("request") if isinstance(task.input, dict) else None
    # 破损的原请求交由 handler 重试判定（kb.index：retry_blocked_reason=request_invalid → 409），不在此处抛 500。
    params = dict(request) if isinstance(request, dict) else {}
    return await create_task(db, engine, user_id, task.kind, params, retry_of=task, admission=admission)


async def resume_task(
    db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int, *, expected_task_key: str | None = None,
) -> TaskOut:
    """待审核 → 排队：同一任务（id、断点不变）从下一步继续；下一步的上下文在运行时按最新数据重新组装。"""
    task = await get_owned_task(db, user_id, task_id, expected_task_key=expected_task_key)
    grant, queued_at = task_grant_identity(task.input), task.queued_at
    await refresh_same_task(db, task)
    if task.admin_cancel_operation_id is not None or task.execution_key is not None:
        raise ConflictError("取消请求正在安全收尾，不能继续")
    await _lock_user_task_project(
        db, user_id, task, active_grant_required=True, expected_grant=grant, expected_queued_at=queued_at,
        visibility_write=(task.kind == "agent.turn"
                          or getattr(get_handler(task.kind), "admission_preflight_aware", False) is True),
    )
    if task.admin_cancel_operation_id is not None or task.execution_key is not None:
        raise ConflictError("取消请求正在安全收尾，不能继续")
    if task.status != "paused":
        raise ConflictError("只有待审核的任务可以继续")
    if (task.kind in ("chapter.generate", "chapter.polish", "chapter.revise")
            and isinstance(task.checkpoint, dict) and "_chapter_claim" in task.checkpoint):
        raise ConflictError("章节保存结果尚未核对，不能继续")
    if (task.kind == "chapter.analyze" and isinstance(task.checkpoint, dict)
            and any(key in task.checkpoint for key in ("_analysis_pending", "_analysis_completion"))):
        raise ConflictError("分析保存结果尚未核对，不能继续")
    if any(isinstance(value, dict) and "_batch_outcome_unknown" in value for value in (task.input, task.checkpoint)):
        raise ConflictError("批量执行结果尚未核对，不能继续")
    if _has_wizard_receipt(task):
        raise ConflictError("向导执行结果尚未核对，不能继续")
    await require_task_admission_open(db)
    from app.services.rich_document_review import pending_for_task

    if await pending_for_task(db, task) is not None:
        raise ConflictError("请先核对并采纳或放弃富文档提案", extra={"code": "rich_review_pending"})
    hook = getattr(get_handler(task.kind), "revalidate_resume", None)
    if hook is not None:
        await hook(db, engine.ctx, task)
    try:
        result = await db.execute(
            update(Task)
            .where(Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == "paused",
                   Task.admin_cancel_operation_id.is_(None), Task.execution_key.is_(None))
            .values(status="queued", error="", result=None, finished_at=None, stage=STAGE_LABELS["queued"],
                    queued_at=_next_queued_at(task), worker_key=None, recovered_at=None, cancel_requested_at=None)
        )
        if result.rowcount == 1:
            await task_statistics_service.record_state_change(db, task, "queued")
        await db.commit()
    except IntegrityError as exc:
        # 部分唯一索引兜底：待审核期间同资源已有新的活动任务
        await db.rollback()
        if _active_resource_conflict(exc):
            raise ConflictError(ACTIVE_CONFLICT) from exc
        raise
    if result.rowcount != 1:
        raise ConflictError("任务状态已变化，请刷新后重试")
    await refresh_same_task(db, task)
    if task.status in ACTIVE_TASK_STATUSES:
        engine.ensure_hub(
            task.id, statistics_key=task.statistics_key, status=task.status,
            text=task.output_text, progress=task.progress, stage=task.stage, queued_at=task.queued_at,
            execution_key=task.execution_key,
        )
    engine.notify_queue_changed()
    return await task_out(db, engine, task)


async def finish_paused_task(
    db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int, expected_review: ReviewIdentity | None = None,
    *, expected_task_key: str | None = None,
) -> TaskOut:
    """待审核 → 已完成（提前结束）：剩余步骤不再执行。"""
    task = await get_owned_task(db, user_id, task_id, expected_task_key=expected_task_key)
    task_key = task.statistics_key
    expected_grant, expected_queued_at = task_grant_identity(task.input), task.queued_at
    await refresh_same_task(db, task)
    if task.admin_cancel_operation_id is not None or task.execution_key is not None:
        raise ConflictError("取消请求正在安全收尾，不能提前结束")
    await _lock_user_task_project(
        db, user_id, task, active_grant_required=True,
        expected_grant=expected_grant, expected_queued_at=expected_queued_at,
    )
    if (task.kind == "chapter.analyze" and isinstance(task.checkpoint, dict)
            and any(key in task.checkpoint for key in ("_analysis_pending", "_analysis_completion"))):
        raise ConflictError("分析保存结果尚未核对，不能结束")
    if any(isinstance(value, dict) and "_batch_outcome_unknown" in value for value in (task.input, task.checkpoint)):
        raise ConflictError("批量执行结果尚未核对，不能结束")
    if _has_wizard_receipt(task):
        raise ConflictError("向导执行结果尚未核对，不能结束")
    if task.status != "paused":
        raise ConflictError("只有待审核的任务可以结束")
    from app.services.rich_document_review import discard_pending, pending_for_task, require_review_identity

    await require_review_identity(db, task, expected_review)
    proposal = await pending_for_task(db, task)
    hook = getattr(get_handler(task.kind), "finish_result", None)
    final = hook(task) if hook is not None else {**(task.result or {}), "ended_early": True}
    if proposal is not None:
        final = {**final, "ended_early": True, "review_discarded": True, "applied": False}
    status = "cancelled" if proposal is not None and task.kind != "chapter.batch" else "succeeded"
    stamp = task_statistics_service.now_utc()
    result = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == "paused",
               Task.queued_at == task.queued_at,
               Task.admin_cancel_operation_id.is_(None), Task.execution_key.is_(None))
        .values(status=status, result=final, progress=100 if status == "succeeded" else task.progress,
                error="候选未采纳，任务已结束" if status == "cancelled" else "",
                stage=STAGE_LABELS[status], finished_at=stamp)
    )
    if result.rowcount == 1:
        if proposal is not None:
            await discard_pending(db, task)
        await task_statistics_service.record_state_change(db, task, status, finished_at=stamp)
    await db.commit()
    if result.rowcount != 1:
        raise ConflictError("任务状态已变化，请刷新后重试")
    await refresh_same_task(db, task)
    await prune_finished(db, user_id)
    return await project_task_out(
        db, user_id, to_out(task), active_grant_required=True, task_key=task_key,
        expected_grant=expected_grant, expected_queued_at=expected_queued_at,
    )


# ---------------- 查询 ----------------


async def get_owned_task(
    db: AsyncSession, user_id: int, task_id: int, *, expected_task_key: str | None = None,
) -> Task:
    if expected_task_key is not None:
        try:
            if type(expected_task_key) is not str or str(UUID(expected_task_key)) != expected_task_key:
                raise ValueError
        except ValueError:
            raise NotFoundError("任务不存在") from None
    statement = select(Task).where(Task.id == task_id, task_visible_to_actor(db, user_id))
    if expected_task_key is not None:
        statement = statement.where(Task.statistics_key == expected_task_key)
    with db.no_autoflush:
        task = await db.scalar(statement.execution_options(populate_existing=True))
    if task is None:
        await authz.require_authenticated(db, user_id)
        raise NotFoundError("任务不存在")
    return task


async def _lock_user_task_project(
    db: AsyncSession, user_id: int, task: Task, *, active_grant_required: bool = False,
    expected_grant: Any = _UNSET, expected_queued_at: Any = _UNSET, visibility_write: bool = False,
) -> None:
    """锁内重新校验任务的个人归属和作品授权；不能借管理员人工权限控制私人任务。"""
    task_id, task_key, project_id = task.id, task.statistics_key, task.project_id
    with db.no_autoflush:
        if project_id is None:
            # 个人任务没有作品父锁，也必须锁定用户并验证请求原令牌，不能在撤销后继续写入。
            await authz.require_authenticated(db, user_id, write=True)
            await refresh_same_task(db, task)
            if task.id != task_id or task.statistics_key != task_key or task.user_id != user_id \
                    or task.project_id is not None:
                raise NotFoundError("任务不存在")
            if ((expected_grant is not _UNSET and task_grant_identity(task.input) != expected_grant)
                    or (expected_queued_at is not _UNSET and task.queued_at != expected_queued_at)):
                raise ConflictError("任务执行轮次已变化，请刷新后重试", extra={"code": "task_round_changed"})
            return
        project, _, member = await project_access.resolve_access(
            db, user_id, project_id, for_update=True, visibility_write=visibility_write,
        )
        await refresh_same_task(db, task)
        if task.id != task_id or task.statistics_key != task_key or task.user_id != user_id \
                or task.project_id != project_id:
            raise NotFoundError("任务不存在")
        if ((expected_grant is not _UNSET and task_grant_identity(task.input) != expected_grant)
                or (expected_queued_at is not _UNSET and task.queued_at != expected_queued_at)):
            raise ConflictError("任务执行轮次已变化，请刷新后重试", extra={"code": "task_round_changed"})
        if project.user_id == user_id:
            return
        if member is None or member.status != "accepted":
            raise NotFoundError("任务不存在")
        if active_grant_required or task.status in OPEN_TASK_STATUSES:
            if member.role != "editor" or task_grant_identity(task.input) != (member.invitation_id, member.revision):
                raise NotFoundError("任务不存在")


async def refresh_same_task(db: AsyncSession, task: Task) -> Task:
    """只重读原UUID，不能让旧请求因SQLite整数ID复用改为操作另一个任务。"""
    if task.statistics_key is None:
        raise NotFoundError("任务不存在")
    with db.no_autoflush:
        row = await db.scalar(select(Task).where(
            Task.id == task.id, Task.statistics_key == task.statistics_key
        ).execution_options(populate_existing=True))
    if row is None:
        raise NotFoundError("任务不存在")
    return row


async def get_task(
    db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int, *, expected_task_key: str | None = None,
) -> TaskOut:
    return await task_out(db, engine, await get_owned_task(db, user_id, task_id, expected_task_key=expected_task_key))


async def get_output(
    db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int, *, expected_task_key: str | None = None,
) -> str:
    task = await get_owned_task(db, user_id, task_id, expected_task_key=expected_task_key)
    task_key, project_id = task.statistics_key, task.project_id
    active_grant_required = task.status in OPEN_TASK_STATUSES
    expected_grant = task_grant_identity(task.input) if active_grant_required else _UNSET
    expected_queued_at = task.queued_at if active_grant_required else _UNSET
    hub = None
    if _live_output_hub(engine, task) is not None and await engine.lease.confirms_output(db):
        hub = _live_output_hub(engine, task)
    output = hub.text if hub is not None else task.output_text
    if project_id is not None:
        from app.services.personal_projection import project_payload

        projected = await project_payload(db, user_id, project_id, {"text": output})
        output = projected["text"]
    await _require_visible_task_result(
        db, user_id, task_id, project_id, active_grant_required=active_grant_required, task_key=task_key,
        expected_grant=expected_grant, expected_queued_at=expected_queued_at,
    )
    return output


async def list_tasks(
    db: AsyncSession,
    engine: "TaskEngine",
    user_id: int,
    *,
    project_id: int | None = None,
    chapter_id: int | None = None,
    kind: str | None = None,
    active: bool | None = None,
    limit: int = 50,
) -> list[TaskOut]:
    stmt = select(Task, and_(Task.status == "running", unproven_execution())).where(task_visible_to_actor(db, user_id))
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)
    if chapter_id is not None:
        stmt = stmt.where(Task.chapter_id == chapter_id)
    if kind is not None:
        stmt = stmt.where(Task.kind == kind)
    if active is True:
        # 「进行中」包含待审核：面板据此展示继续 / 结束
        stmt = stmt.where(Task.status.in_(OPEN_TASK_STATUSES))
    elif active is False:
        stmt = stmt.where(Task.status.in_(FINISHED_TASK_STATUSES))
    with db.no_autoflush:
        rows = (await db.execute(stmt.order_by(Task.id.desc()).limit(limit))).all()
    tasks = [t for t, _ in rows]
    bindings = [
        (task.id, task.statistics_key, task.project_id, task.status in OPEN_TASK_STATUSES,
         task_grant_identity(task.input), task.queued_at)
        for task in tasks
    ]
    queued = await _queued_ids(db) if any(t.status == "queued" for t in tasks) else []
    use_hub = any(_live_output_hub(engine, t) is not None for t in tasks) and await engine.lease.confirms_output(db)
    outputs = [
        to_out(t, queue_position=bisect.bisect_left(queued, t.id) if t.status == "queued" else None,
               engine=engine, unproven=unproven, use_hub=use_hub)
        for t, unproven in rows
    ]
    # 同一作品批量投影，避免对列表中的每条任务重复检查私人引用。
    from app.services.personal_projection import project_payload

    by_project: dict[int, list[int]] = {}
    for index, output in enumerate(outputs):
        if output.project_id is not None:
            by_project.setdefault(output.project_id, []).append(index)
    hidden: set[int] = set()
    for pid, indexes in by_project.items():
        try:
            payloads = [outputs[index].model_dump(mode="python") for index in indexes]
            safe = await project_payload(db, user_id, pid, payloads)
        except NotFoundError:
            # 查询列表与投影间可能撤权；不返回已失效项目的缓存内容。
            hidden.update(indexes)
            continue
        for index, item in zip(indexes, safe, strict=True):
            outputs[index] = TaskOut.model_validate(item)
    active_ids = [task_id for task_id, _, _, active, _, _ in bindings if active]
    other_ids = [task_id for task_id, _, _, active, _, _ in bindings if not active]
    # 身份错误与撤权后的空列表区分；最终任务查询仍须在它之后，避免新增 await 窗口泄露旧权限。
    await authz.require_authenticated(db, user_id)
    with db.no_autoflush:
        final_rows = (await db.execute(select(Task.id, Task.statistics_key, Task.project_id, Task.status,
                                              Task.input, Task.queued_at).where(or_(
            and_(Task.id.in_(active_ids), task_visible_to_actor(db, user_id, active_grant_required=True)),
            and_(Task.id.in_(other_ids), Task.status.in_(FINISHED_TASK_STATUSES), task_visible_to_actor(db, user_id)),
        )))).all()
    final_by_id = {row.id: row for row in final_rows}
    result = []
    for index, (binding, output) in enumerate(zip(bindings, outputs, strict=True)):
        task_id, task_key, pid, active, grant, queued_at = binding
        row = final_by_id.get(task_id)
        if (index in hidden or row is None or row.statistics_key != task_key or row.project_id != pid
                or (active and (task_grant_identity(row.input) != grant or row.queued_at != queued_at))):
            continue
        result.append(output.model_copy(update={"task_key": task_key}))
    if not result:
        # 最终 SQL 可因刚撤销令牌而排除所有行；空结果再次区分认证失效，不会泄露旧任务。
        await authz.require_authenticated(db, user_id)
    return result


# ---------------- 取消 / 删除 / 清理 ----------------


async def cancel_task(
    db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int, expected_review: ReviewIdentity | None = None,
    *, expected_task_key: str | None = None,
) -> TaskOut:
    task = await get_owned_task(db, user_id, task_id, expected_task_key=expected_task_key)
    expected_grant, expected_queued_at = task_grant_identity(task.input), task.queued_at
    await _lock_user_task_project(
        db, user_id, task, active_grant_required=True,
        expected_grant=expected_grant, expected_queued_at=expected_queued_at,
    )
    task_key = task.statistics_key
    async with cleanup_work.request_scope(db, engine):
        out = await _cancel_loaded_task(db, engine, task, expected_review=expected_review, verify_review=True)
        try:
            return await project_task_out(
                db, user_id, out, active_grant_required=True, task_key=task_key,
                expected_grant=expected_grant, expected_queued_at=expected_queued_at,
            )
        except (NotFoundError, ConflictError):
            recovered = await _author_cancelled_during_deletion(
                db, user_id, out, task_key=task_key, queued_at=expected_queued_at,
            )
            if recovered is not None:
                return recovered
            raise


async def _author_cancelled_during_deletion(
    db: AsyncSession, user_id: int, out: TaskOut, *, task_key: str | None, queued_at: Any,
) -> TaskOut | None:
    """仅原作者在删除排空期间确认已提交的取消结果；不携带私人断点。"""
    actor = db.info.get("authenticated_actor")
    if (type(actor) is not authz.AuthenticatedActor or actor.id != user_id or task_key is None
            or out.project_id is None or out.status != "cancelled"):
        return None
    await db.rollback()
    await authz.require_authenticated(db, user_id, write=True)
    try:
        await project_lifecycle.lock_record(db, out.project_id)
    except NotFoundError:
        return None
    row = await db.scalar(select(Task.id).join(Project, Project.id == Task.project_id).join(
        User, User.id == Task.user_id,
    ).where(
        Task.id == out.id, Task.statistics_key == task_key, Task.user_id == user_id,
        Task.project_id == out.project_id, Task.kind == out.kind, Task.queued_at == queued_at,
        Task.status == "cancelled",
        Task.execution_key.is_(None), Task.admin_cancel_operation_id.is_(None),
        Project.user_id == user_id, Project.deletion_started_at.is_not(None), Project.deleted_at.is_(None),
        User.is_active.is_(True), User.token_version == actor.token_version,
    ).with_for_update(of=Task))
    if row is None:
        return None
    return out.model_copy(update={
        "task_key": task_key, "checkpoint": None, "skill": None, "rich_review": None, "retry_of_id": None,
        "chapter_id": None, "scene_id": None, "analysis_id": None, "character_id": None, "outline_id": None,
    })


async def _cancel_loaded_task(
    db: AsyncSession, engine: "TaskEngine", task: Task, *, expected_review: ReviewIdentity | None = None,
    verify_review: bool = False,
) -> TaskOut:
    """调用者已核实归属或删除项目范围；只共享取消，不伪装成目标 owner。"""
    async with cleanup_work.request_scope(db, engine):
        return await _cancel_loaded_impl(db, engine, task, expected_review=expected_review, verify_review=verify_review)


async def _cancel_legacy_document_task(
    db: AsyncSession, engine: "TaskEngine", task: Task, expected_queued_at: Any,
) -> TaskOut:
    """0045 前入队、文档侧无占用的旧排队任务：取消与作品删除共用受限系统清理。"""
    from app.services import kb_index_service

    unproven = ConflictError("知识库原任务占用无法验证", extra={"code": "task_cleanup_unproven"})
    legacy = kb_index_service.capture_legacy_document_task(
        task, task_key=task.statistics_key, queued_at=expected_queued_at,
    )
    if legacy is None:
        raise unproven
    status = "cancelled"
    try:
        # 用户取消已在 resolve_access 内持有作品父行锁（此处为同事务重入）；删除排空路径在此首次取锁。
        # 锁序与领取一致：作品 → Task → Document。删除中作品同样可清理，故不要求 active。
        await project_lifecycle.lock_record(db, legacy.project_id)
        cleaned = await kb_index_service.cleanup_legacy_unclaimed_document_task(db, legacy, status=status)
        if cleaned is None:
            raise unproven
        await cleanup_work.register(db, engine, task, settled=True)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            async with engine.ctx.sessionmaker() as proof:
                if not await kb_index_service.legacy_cleanup_committed(proof, legacy, status=status):
                    raise
    except BaseException:
        await db.rollback()
        raise
    await refresh_same_task(db, task)
    await prune_finished(db, task.user_id)
    engine.close_hub(
        task.id, "error", {"task": out_json(task), "detail": task.error},
        statistics_key=task.statistics_key, queued_at=expected_queued_at,
    )
    engine.notify()
    return to_out(task)


async def _cancel_loaded_impl(
    db: AsyncSession, engine: "TaskEngine", task: Task, *, expected_review: ReviewIdentity | None = None,
    verify_review: bool = False,
) -> TaskOut:
    expected_queued_at = task.queued_at
    await refresh_same_task(db, task)
    if task.queued_at != expected_queued_at:
        raise ConflictError("任务执行轮次已变化，请刷新后重试", extra={"code": "task_round_changed"})
    if verify_review:
        from app.services.rich_document_review import require_review_identity

        await require_review_identity(db, task, expected_review)
    if task.admin_cancel_operation_id is not None:
        return await _wait_admin_cancel(db, engine, task)
    if (task.kind == "chapter.analyze" and task.status == "paused" and isinstance(task.checkpoint, dict)
            and any(key in task.checkpoint for key in ("_analysis_pending", "_analysis_completion"))):
        raise ConflictError("分析执行结果尚未核对，不能取消")
    if task.kind.startswith("wizard.") and task.status in ("queued", "paused") and _has_wizard_receipt(task):
        raise ConflictError("向导执行结果尚未核对，不能取消")
    if task.status == "queued":
        if task.execution_key is not None:
            raise ConflictError("任务正在安全收尾，请稍后重试")
        from app.services import kb_index_service

        is_document = task.kind == "kb.index" and _request_dict(task).get("mode") == "document"
        is_chapter = task.kind in ("chapter.generate", "chapter.polish", "chapter.revise")
        is_batch = task.kind == "chapter.batch"
        task_project_id = task.project_id
        if is_batch and task.checkpoint is not None:
            checkpoint = task.checkpoint
            review = checkpoint.get("rich_review") if isinstance(checkpoint, dict) else None
            if (not isinstance(checkpoint, dict) or checkpoint.get("current") is not None
                    or checkpoint.get("phase") == "awaiting_review"
                    or (isinstance(review, dict) and review.get("phase") == "awaiting_review")):
                raise ConflictError("批量任务仍有未完成的章节占用或提案，不能取消")
        queued_claim = await kb_index_service.queued_document_claim(db, task) if is_document else None
        if is_document and queued_claim is None:
            # 缺旧绑定不能将 None 当作已释放；仅在锁内可证明无执行体时受限清理（E4-e），否则失败关闭。
            return await _cancel_legacy_document_task(db, engine, task, expected_queued_at)
        chapter_queued_claim = None
        if is_chapter:
            from app.services.task_engine.handlers import chapter as chapter_handler

            # 原 queued Task 自带的服务端 claim 才能释放；旧记录缺证据时不猜测。
            chapter_queued_claim = chapter_handler.capture_queued_claim(task)
        original_cleanup = engine.capture_abort_snapshot(
            task, claim=chapter_queued_claim if is_chapter else queued_claim,
        ) if is_chapter or is_document or is_batch else None
        prior_failure = engine._failed_cleanups.get(task.id)
        if prior_failure is not None and prior_failure != original_cleanup:
            raise ConflictError("原任务清理轮次已变化，请刷新后重试", extra={"code": "task_round_changed"})
        stamp = task_statistics_service.now_utc()
        # 文档和章节占用可与终态一同原子释放；其他资源先保留 queued 唯一索引，
        # 直到原异步 abort 返回，以免旧清理覆盖新任务。
        atomic_cleanup = is_document or is_chapter or is_batch
        cleanup_key = None if atomic_cleanup else str(uuid4())
        result = await db.execute(
            update(Task)
            .where(Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == "queued",
                   Task.queued_at == expected_queued_at,
                   Task.admin_cancel_operation_id.is_(None), Task.execution_key.is_(None))
            .values(**({"status": "cancelled", "error": "任务已取消", "stage": STAGE_LABELS["cancelled"],
                        "finished_at": stamp} if atomic_cleanup else {"execution_key": cleanup_key}))
        )
        try:
            if result.rowcount == 1:
                await cleanup_work.register(db, engine, task, settled=atomic_cleanup, phase="queued")
            if result.rowcount == 1 and atomic_cleanup:
                await cleanup_work.clear_proven_failure(db, original_cleanup)
                if is_document and queued_claim is not None \
                        and not await kb_index_service.release_document_claim(db, queued_claim):
                    raise ConflictError("原知识库占用已变化，请刷新后重试")
                if is_chapter:
                    from app.services import chapter_claim

                    if not await chapter_claim.release_chapter_claim(db, chapter_queued_claim):
                        raise ConflictError("原章节占用已变化，请刷新后重试")
                await task_statistics_service.record_state_change(db, task, "cancelled", finished_at=stamp)
            try:
                # 没有赢得 CAS 时，后续 running 登记仍须使用原来的鉴权与作品锁。
                if result.rowcount == 1:
                    await db.commit()
            except Exception:
                # 只在独立事务确认本次 queued claim 已随同原 Task 终态释放后接受确认丢失。
                await db.rollback()
                if chapter_queued_claim is None or result.rowcount != 1:
                    raise
                async with engine.ctx.sessionmaker() as proof:
                    fact = await proof.scalar(select(Task).where(
                        Task.id == chapter_queued_claim.task_id,
                        Task.statistics_key == chapter_queued_claim.task_key,
                        Task.user_id == chapter_queued_claim.actor_id,
                        Task.project_id == chapter_queued_claim.project_id,
                        Task.chapter_id == chapter_queued_claim.chapter_id,
                        Task.queued_at == chapter_queued_claim.queued_at, Task.status == "cancelled",
                        Task.execution_key.is_(None), Task.admin_cancel_operation_id.is_(None),
                    ))
                    held = await proof.scalar(select(Chapter.id).where(
                        Chapter.id == chapter_queued_claim.chapter_id,
                        Chapter.project_id == chapter_queued_claim.project_id,
                        Chapter.generation_claim_key == chapter_queued_claim.claim_key,
                        Chapter.generation_claim_task_key == chapter_queued_claim.task_key,
                    ))
                    if (fact is None or not isinstance(fact.input, dict) or "_chapter_claim" in fact.input
                            or held is not None):
                        raise
                task = await refresh_same_task(db, fact)
        except BaseException:
            await db.rollback()
            if chapter_queued_claim is not None or queued_claim is not None or is_batch:
                # 任务与原 queued claim 均回滚；项目排空时只能按这个仍持久的原身份重试。
                claim_project = original_cleanup.project_id if original_cleanup is not None else task_project_id
                if claim_project is not None:
                    await engine.record_abort_failure(claim_project, original_cleanup, registration_rolled_back=True)
            raise
        if result.rowcount == 1:
            engine.forget_abort_failure(original_cleanup)
            if not atomic_cleanup:
                await refresh_same_task(db, task)
                await _abort_loaded_task(engine, task)
                _, committed = await engine.complete_state(
                    task, expected_status="queued", status="cancelled", result=None, error="任务已取消",
                    text=task.output_text, progress=task.progress, abort_ok=True,
                )
                if not committed:
                    raise ConflictError("任务执行轮次已变化，请刷新后重试", extra={"code": "task_round_changed"})
            await refresh_same_task(db, task)
            await prune_finished(db, task.user_id)
            engine.close_hub(
                task.id, "error", {"task": out_json(task), "detail": task.error},
                statistics_key=task.statistics_key, queued_at=expected_queued_at,
            )
            engine.notify()
            return to_out(task)
        await refresh_same_task(db, task)
        if task.queued_at != expected_queued_at:
            raise ConflictError("任务执行轮次已变化，请刷新后重试", extra={"code": "task_round_changed"})
        if task.admin_cancel_operation_id is not None:
            return await _wait_admin_cancel(db, engine, task)
    if task.status == "paused":
        if task.execution_key is not None:
            raise ConflictError("任务正在安全收尾，请稍后重试")
        stamp = task_statistics_service.now_utc()
        discarded_review = False
        had_released_review = ((task.checkpoint or {}).get("rich_review") or {}).get("phase") == "awaiting_review"
        result = await db.execute(
            update(Task)
            .where(Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == "paused",
                   Task.queued_at == task.queued_at,
                   Task.admin_cancel_operation_id.is_(None), Task.execution_key.is_(None))
            .values(status="cancelled", error="任务已取消", stage=STAGE_LABELS["cancelled"], finished_at=stamp)
        )
        if result.rowcount == 1:
            from app.services.rich_document_review import discard_pending

            discarded_review = await discard_pending(db, task)
            await cleanup_work.register(
                db, engine, task, settled=discarded_review or had_released_review, phase="paused",
            )
            await task_statistics_service.record_state_change(db, task, "cancelled", finished_at=stamp)
            await db.commit()
        if result.rowcount == 1:
            # 待审核时没有执行体，资源也已释放；on_abort 幂等，照常调用
            # 富文档提案已在暂停前释放章抢占，不能误回滚后来另一个任务的 generating。
            if not discarded_review and not had_released_review:
                await _abort_loaded_task(engine, task)
                await cleanup_work.settle(db, engine)
                await db.commit()
            await refresh_same_task(db, task)
            await prune_finished(db, task.user_id)
            return to_out(task)
        await refresh_same_task(db, task)
        if verify_review and expected_review is not None:
            raise ConflictError("原提案轮次已变化，请刷新后重试", extra={"code": "rich_review_task_changed"})
        if task.queued_at != expected_queued_at:
            raise ConflictError("任务执行轮次已变化，请刷新后重试", extra={"code": "task_round_changed"})
        if task.admin_cancel_operation_id is not None:
            return await _wait_admin_cancel(db, engine, task)
        if task.status == "queued":
            # 与继续竞争：已重新排队，按排队中取消
            return await _cancel_loaded_task(db, engine, task)
    if task.status == "running":
        from app.services.task_engine import owner_cancellation

        row = await owner_cancellation.request_running(db, engine, task)
        return to_out(row, queue_position=await queue_position(db, row), engine=engine)
    raise ConflictError("任务已结束，无法取消")


async def _wait_admin_cancel(db: AsyncSession, engine: "TaskEngine", task: Task) -> TaskOut:
    from app.services.task_engine import owner_cancellation

    task_id, key, operation_id = task.id, task.statistics_key, task.admin_cancel_operation_id
    queued_at, execution = task.queued_at, task.execution_key
    await db.rollback()
    if key is None or operation_id is None:
        raise ConflictError("任务取消状态已变化，请刷新后重试")
    engine.request_admin_cancel(key, operation_id)
    deadline = asyncio.get_running_loop().time() + engine.cancel_wait_seconds
    row = await owner_cancellation.wait_for_result(db, engine, task_id, key, queued_at, execution,
                                                   admin_operation=operation_id, deadline=deadline)
    return to_out(row, queue_position=await queue_position(db, row), engine=engine)


async def delete_task(
    db: AsyncSession, user_id: int, task_id: int, expected_review: ReviewIdentity | None = None,
    *, expected_task_key: str | None = None,
) -> None:
    task = await get_owned_task(db, user_id, task_id, expected_task_key=expected_task_key)
    grant, queued_at = task_grant_identity(task.input), task.queued_at
    await refresh_same_task(db, task)
    if task.admin_cancel_operation_id is not None:
        raise ConflictError("取消请求正在安全收尾，不能删除任务")
    await _lock_user_task_project(db, user_id, task, expected_grant=grant, expected_queued_at=queued_at)
    if task.status == "paused":
        raise ConflictError("任务待审核，无法删除，请先结束或取消")
    if task.status not in FINISHED_TASK_STATUSES:
        raise ConflictError("任务进行中，无法删除，请先取消")
    if task.execution_key is not None:
        raise ConflictError("任务正在安全收尾，不能删除任务")
    from app.services.rich_document_review import require_finished_review_identity

    require_finished_review_identity(task, expected_review)
    receipt_condition = await _receipt_cleanup_condition(db, task)
    if await db.scalar(select(Task.id).where(
        Task.id == task.id, Task.statistics_key == task.statistics_key,
        or_(~_without_executing_agent_change(db), ~receipt_condition),
    )) is not None:
        raise ConflictError("任务关联的操作仍在处理，不能删除任务")
    await task_statistics_service.mark_removed(
        db, Task.id == task.id, Task.statistics_key == task.statistics_key, _without_executing_agent_change(db),
        receipt_condition,
    )
    result = await db.execute(delete(Task).where(
        Task.id == task.id, Task.statistics_key == task.statistics_key, Task.admin_cancel_operation_id.is_(None),
        Task.execution_key.is_(None), Task.queued_at == queued_at, _without_executing_agent_change(db),
        receipt_condition,
    ))
    if result.rowcount != 1:
        raise ConflictError("任务状态或关联操作已变化，请刷新后重试")
    await db.commit()


async def _lock_accessible_history_projects(db: AsyncSession, user_id: int, project_ids: set[int]) -> set[int]:
    locked = set()
    # 跨作品批量清理先一次锁定所有目标作者；仅有个人任务时也须锁 actor 并重验原令牌。
    with db.no_autoflush:
        owners = (
            set(await db.scalars(select(Project.user_id).where(Project.id.in_(project_ids)))) if project_ids else set()
        )
        await authz.require_authenticated(db, user_id, write=True, targets=tuple(sorted(owners)))
    for project_id in sorted(project_ids):
        try:
            project, _, member = await project_access.resolve_access(db, user_id, project_id, for_update=True)
            if project.user_id != user_id and (member is None or member.status != "accepted"):
                continue
        except (NotFoundError, ConflictError):
            # 查询候选行后可能刚建立删除屏障；跳过该项目而不是破坏回收站历史。
            continue
        locked.add(project_id)
    return locked


async def _lock_history_projects(db: AsyncSession, user_id: int, project_ids: set[int]) -> set[int]:
    """系统裁剪仅锁生命周期，不需要任务本人继续持有协作授权。"""
    locked = set()
    for project_id in sorted(project_ids):
        try:
            await project_lifecycle.lock_active(db, project_id)
        except (NotFoundError, ConflictError):
            continue
        locked.add(project_id)
    return locked


async def _remove_finished_with_receipt_proofs(db: AsyncSession, *conditions) -> int:
    """逐条验证带收据的成功任务；以小批 CAS 执行，损坏记录保持不变。"""
    marked = list(await db.scalars(select(Task).where(
        *conditions, ~_without_unresolved_task_receipt(db),
    ).order_by(Task.id).with_for_update()))
    verified = [await _receipt_cleanup_condition(db, task) for task in marked]
    total = 0
    batches = [_without_unresolved_task_receipt(db)]
    batches.extend(or_(*verified[offset:offset + 40]) for offset in range(0, len(verified), 40))
    for proof in batches:
        keys = await task_statistics_service.mark_removed(db, *conditions, proof)
        if not keys:
            continue
        result = await db.execute(delete(Task).where(Task.statistics_key.in_(keys), *conditions, proof))
        total += result.rowcount or 0
    return total


async def clear_finished(db: AsyncSession, user_id: int, *, project_id: int | None = None) -> int:
    statement = select(Task.project_id).where(
        task_visible_to_actor(db, user_id), Task.status.in_(FINISHED_TASK_STATUSES),
        Task.admin_cancel_operation_id.is_(None), Task.execution_key.is_(None),
    )
    if project_id is not None:
        statement = statement.where(Task.project_id == project_id)
    with db.no_autoflush:
        candidates = set(await db.scalars(statement))
    locked = await _lock_accessible_history_projects(db, user_id, candidates - {None})
    allowed = or_(Task.project_id.in_(locked), Task.project_id.is_(None) if None in candidates else False)
    conditions = (
        task_visible_to_actor(db, user_id), Task.status.in_(FINISHED_TASK_STATUSES), allowed,
        Task.admin_cancel_operation_id.is_(None), Task.execution_key.is_(None), _without_executing_agent_change(db),
    )
    removed = await _remove_finished_with_receipt_proofs(db, *conditions)
    await db.commit()
    return removed


async def prune_finished(db: AsyncSession, user_id: int, *, source: Task | None = None) -> None:
    """回收站历史不参与普通用户已结束任务裁剪；锁住将被裁剪的父项目直到提交。"""
    stale = (await db.execute(
        select(Task.id, Task.statistics_key, Task.project_id)
        .where(Task.user_id == user_id, Task.status.in_(FINISHED_TASK_STATUSES), visible_task_project_exists(),
               Task.admin_cancel_operation_id.is_(None), Task.execution_key.is_(None))
        .order_by(Task.id.desc()).offset(MAX_FINISHED_PER_USER)
    )).all()
    if not stale:
        return
    projects = {pid for _, _, pid in stale if pid is not None}
    if source is not None and source.project_id is not None:
        projects.add(source.project_id)
    locked = await _lock_history_projects(db, user_id, projects)
    eligible = [(tid, key) for tid, key, pid in stale if pid is None or pid in locked]
    if eligible:
        if source is not None:
            from app.services.task_engine.workers import lock_live_worker

            # 终态提交后才触发的裁剪仍属于原执行。作品、任务和租约锁覆盖下面的统计修改及删除。
            origin = update(Task).where(
                Task.id == source.id, Task.statistics_key == source.statistics_key, Task.user_id == user_id,
                Task.project_id == source.project_id, Task.kind == source.kind, Task.queued_at == source.queued_at,
                Task.worker_key == source.worker_key, Task.status.in_((*FINISHED_TASK_STATUSES, "paused")),
                or_(Task.execution_key.is_(None), Task.execution_key == source.execution_key),
                Task.admin_cancel_operation_id.is_(None),
            ).values(updated_at=Task.updated_at).execution_options(synchronize_session=False)
            if (await db.execute(origin)).rowcount != 1 or (source.worker_key is not None
                    and not await lock_live_worker(db, source.worker_key)):
                await db.rollback()
                return
        identities = or_(
            Task.statistics_key.in_([key for _, key in eligible if key is not None]),
            and_(Task.id.in_([tid for tid, key in eligible if key is None]), Task.statistics_key.is_(None)),
        )
        conditions = (Task.user_id == user_id, Task.status.in_(FINISHED_TASK_STATUSES), visible_task_project_exists(),
                      Task.admin_cancel_operation_id.is_(None), Task.execution_key.is_(None),
                      _without_executing_agent_change(db))
        await _remove_finished_with_receipt_proofs(db, identities, *conditions)
        await db.commit()


async def _retry_failed_cleanup(db: AsyncSession, engine: "TaskEngine", snapshot: "AbortSnapshot", *,
                                work_key: str | None = None, worker_key: str | None = None,
                                effect_applied: bool = False) -> bool:
    """重试最初失败的身份；任何 Task/轮次/资源快照变化都保留项目屏障。"""
    if snapshot.project_id is not None:
        await project_lifecycle.lock_record(db, snapshot.project_id)
    allowed_admin = (Task.status == "queued" if snapshot.status == "queued" and snapshot.execution_key
                     else Task.admin_cancel_operation_id.is_(None))
    task = await db.scalar(select(Task).where(
        Task.id == snapshot.task_id, Task.statistics_key == snapshot.task_key, Task.user_id == snapshot.actor_id,
        Task.project_id == snapshot.project_id, Task.kind == snapshot.kind, Task.queued_at == snapshot.queued_at,
        Task.execution_key == snapshot.execution_key, Task.worker_key == snapshot.worker_key,
        (Task.admin_cancel_operation_id.is_(None) | allowed_admin),
    ).with_for_update())
    if task is None or task.chapter_id != snapshot.chapter_id or task.resource_key != snapshot.resource_key \
            or task.step_cursor != snapshot.step_cursor:
        raise ConflictError("原任务清理身份已变化，作品未删除", extra={"code": "project_tasks_not_stopped"})
    expected_input, expected_checkpoint = json.loads(snapshot.input_json), json.loads(snapshot.checkpoint_json)
    actual_input = json.dumps(task.input, sort_keys=True, separators=(",", ":"))
    actual_checkpoint = json.dumps(task.checkpoint, sort_keys=True, separators=(",", ":"))
    if (snapshot.kind in ("chapter.generate", "chapter.polish", "chapter.revise")
            and actual_input != snapshot.input_json and isinstance(expected_input, dict)):
        # 执行体 finally 可能已释放本章而后续回调失败；只接受精确去掉原指针的状态。
        expected_input.pop("_chapter_claim", None)
        held = await db.scalar(select(Chapter.id).where(
            Chapter.id == snapshot.claim.chapter_id, Chapter.project_id == snapshot.project_id,
            Chapter.generation_claim_key == snapshot.claim.claim_key,
            Chapter.generation_claim_task_key == snapshot.task_key,
        ))
        if held is not None:
            raise ConflictError("原章节占用尚未清理，作品未删除")
    if (snapshot.kind == "chapter.batch" and actual_checkpoint != snapshot.checkpoint_json
            and isinstance(expected_checkpoint, dict) and snapshot.claim.claim is not None):
        # _batch_attempt 仍与原快照一致时，仅允许 current 从本次 claim 变为 null。
        expected_checkpoint["current"] = None
    if (actual_input != json.dumps(expected_input, sort_keys=True, separators=(",", ":"))
            or actual_checkpoint != json.dumps(expected_checkpoint, sort_keys=True, separators=(",", ":"))):
        raise ConflictError("原任务清理身份已变化，作品未删除", extra={"code": "project_tasks_not_stopped"})
    if engine.admin_cancellations.completed_round(task):
        engine.forget_abort_failure(snapshot)
        return True
    if task.status == "queued" and task.execution_key is None:
        if snapshot.kind in ("chapter.generate", "chapter.polish", "chapter.revise"):
            from app.services.task_engine.handlers import chapter as chapter_handler

            if chapter_handler.capture_queued_claim(task) != snapshot.claim:
                raise ConflictError("原章节占用已变化，作品未删除")
        elif snapshot.kind == "kb.index" and _request_dict(task).get("mode") == "document":
            from app.services import kb_index_service

            if await kb_index_service.queued_document_claim(db, task) != snapshot.claim:
                raise ConflictError("原索引占用已变化，作品未删除")
        return False  # 原排队身份将在以下主循环里同事务取消。
    if task.status == "running":
        return False  # 主循环先请求原执行者或由 C1 接管；本机没有执行体不能证明已经退出。
    if task.status not in {"queued", "running", "cancelled", "failed", "interrupted"}:
        raise ConflictError("原任务状态已变化，作品未删除")
    status, text, progress = task.status, task.output_text, task.progress
    operation = task.admin_cancel_operation_id
    await db.rollback()  # 原清理使用独立事务，不持有请求侧读/写锁等待它。
    original = snapshot.task()
    terminal = status if status not in {"queued", "running"} else None

    async def abort(ctx):
        if snapshot.kind in ("chapter.generate", "chapter.polish", "chapter.revise"):
            await get_handler(snapshot.kind).on_abort(ctx, original, claim=snapshot.claim, terminal_status=terminal)
        elif snapshot.kind == "chapter.batch":
            from app.services import batch_service

            return await batch_service.restore_abort_snapshot(ctx, snapshot.claim, terminal_status=terminal)
        elif snapshot.kind == "kb.index" and snapshot.claim is not None:
            from app.services import kb_index_service

            async with ctx.sessionmaker() as cleanup:
                await kb_index_service.release_document_claim(cleanup, snapshot.claim, terminal_status=terminal)
                await cleanup.commit()
        else:
            await get_handler(snapshot.kind).on_abort(ctx, original)

    if effect_applied:
        restored_batch = None
        if snapshot.kind == "chapter.batch":
            checkpoint = snapshot.claim.checkpoint
            if snapshot.claim.claim is not None:
                checkpoint["current"] = None
            restored_batch = replace(snapshot.claim, claim=None, _checkpoint_json=json.dumps(
                checkpoint, ensure_ascii=False, sort_keys=True, allow_nan=False,
            ))
    else:
        restored_batch = await cleanup_work.apply_effect(engine, abort, work_key=work_key, worker_key=worker_key)
    if status in {"queued", "running"}:
        expected_input = json.loads(snapshot.input_json)
        if snapshot.kind in ("chapter.generate", "chapter.polish", "chapter.revise"):
            expected_input.pop("_chapter_claim", None)
        expected_checkpoint = restored_batch.checkpoint if restored_batch is not None else json.loads(
            snapshot.checkpoint_json,
        )
        completion_snapshot = replace(
            snapshot, input_json=json.dumps(expected_input, sort_keys=True, separators=(",", ":")),
            checkpoint_json=json.dumps(expected_checkpoint, sort_keys=True, separators=(",", ":")),
            step_cursor=restored_batch.cursor if restored_batch is not None else snapshot.step_cursor,
        )
        _, committed = await engine.complete_state(
            original, expected_status=status, status="cancelled", result=None, error="任务已取消",
            text=text, progress=progress, abort_ok=True, batch_snapshot=restored_batch,
            abort_snapshot=completion_snapshot, operation_id=operation,
        )
        if not committed:
            raise ConflictError("原任务轮次已变化，作品未删除")
    else:
        # 清理与终态分事务时，锁后重新证明原轮次及原资源，再按同身份释放执行标记。
        await project_lifecycle.lock_record(db, snapshot.project_id)
        current = await db.scalar(select(Task).where(
            Task.id == snapshot.task_id, Task.statistics_key == snapshot.task_key, Task.user_id == snapshot.actor_id,
            Task.project_id == snapshot.project_id, Task.kind == snapshot.kind, Task.queued_at == snapshot.queued_at,
            Task.execution_key == snapshot.execution_key, Task.status == status, Task.worker_key == snapshot.worker_key,
            Task.admin_cancel_operation_id.is_(None),
        ).with_for_update())
        if current is None:
            raise ConflictError("原任务执行已变化，作品未删除")
        expected_input = json.loads(snapshot.input_json)
        expected_checkpoint = restored_batch.checkpoint if restored_batch is not None else json.loads(
            snapshot.checkpoint_json,
        )
        expected_cursor = restored_batch.cursor if restored_batch is not None else snapshot.step_cursor
        if snapshot.kind in ("chapter.generate", "chapter.polish", "chapter.revise"):
            expected_input.pop("_chapter_claim", None)
            held = await db.scalar(select(Chapter.id).where(
                Chapter.id == snapshot.claim.chapter_id, Chapter.project_id == snapshot.project_id,
                Chapter.generation_claim_key == snapshot.claim.claim_key,
                Chapter.generation_claim_task_key == snapshot.task_key,
            ))
            if held is not None or not isinstance(current.input, dict) or "_chapter_claim" in current.input:
                raise ConflictError("原章节占用尚未清理，作品未删除")
        elif snapshot.kind == "kb.index" and snapshot.claim is not None:
            from app.models import KbDocument

            held = await db.scalar(select(KbDocument.id).where(
                KbDocument.id == snapshot.claim.document_id, KbDocument.project_id == snapshot.project_id,
                KbDocument.index_task_key == snapshot.task_key, KbDocument.index_claim_key == snapshot.claim.claim_key,
            ))
            if held is not None:
                raise ConflictError("原索引占用尚未清理，作品未删除")
        if (json.dumps(current.input, sort_keys=True, separators=(",", ":"))
                != json.dumps(expected_input, sort_keys=True, separators=(",", ":"))
                or json.dumps(current.checkpoint, sort_keys=True, separators=(",", ":"))
                != json.dumps(expected_checkpoint, sort_keys=True, separators=(",", ":"))
                or current.step_cursor != expected_cursor):
            raise ConflictError("原任务断点已变化，作品未删除")
        if snapshot.execution_key is not None:
            conditions = [
                Task.id == snapshot.task_id, Task.statistics_key == snapshot.task_key,
                Task.queued_at == snapshot.queued_at, Task.execution_key == snapshot.execution_key,
                Task.status == status, Task.worker_key == snapshot.worker_key, Task.admin_cancel_operation_id.is_(None),
            ]
            if restored_batch is not None:
                raw_checkpoint = await db.scalar(select(cast(Task.checkpoint, String).label("checkpoint_raw")).where(
                    Task.id == snapshot.task_id, Task.statistics_key == snapshot.task_key,
                ))
                conditions.extend((Task.step_cursor == expected_cursor,
                                   cast(Task.checkpoint, String) == raw_checkpoint if raw_checkpoint is not None
                                   else Task.checkpoint.is_(None)))
            changed = await db.execute(update(Task).where(*conditions).values(execution_key=None))
            if changed.rowcount != 1:
                raise ConflictError("原任务执行已变化，作品未删除")
        if snapshot.execution_key is not None or work_key is not None:
            await cleanup_work.finish_retry(db, engine)
            await db.commit()
        else:
            await db.rollback()
    engine.forget_abort_failure(snapshot)
    return True


async def project_work_exists(db: AsyncSession, project_id: int) -> bool:
    return await db.scalar(select(TaskWorkRecord.work_key).where(
        TaskWorkRecord.project_id == project_id, ~cleanup_work.settled_request_is_releasable(),
    ).limit(1)) is not None


async def retry_project_cleanup_failures(
    db: AsyncSession, engine: "TaskEngine", project_id: int | None, *, queued_admin=None,
) -> None:
    """项目排空重试原失败，管理员仅接回同一带清理标记、原 HTTP 已退出的排队任务。"""
    from app.services.task_engine import workers

    target = ()
    local = {}
    if queued_admin is not None:
        tid, key, queued_at, execution, operation = queued_admin
        if not execution or not await db.scalar(select(Task.id).where(
            Task.id == tid, Task.statistics_key == key, Task.queued_at == queued_at, Task.execution_key == execution,
            Task.project_id == project_id, Task.status == "queued", Task.admin_cancel_operation_id == operation,
        )):
            return
        target = (TaskWorkRecord.task_id == tid, TaskWorkRecord.task_key == key,
                  TaskWorkRecord.queued_at == queued_at, TaskWorkRecord.execution_key == execution)
    else:
        await cleanup_work.reap_settled_requests(db, project_id)
    await cleanup_work.recover_exited_requests(db, engine, project_id, target=target)
    if queued_admin is None:
        if not await cleanup_work.has_failure(db, engine, project_id):
            await db.commit()
            return
        local = {item.task_id: item for item in engine._failed_cleanups.values() if item.project_id == project_id}
    records = (await db.execute(select(TaskWorkRecord.work_key, TaskWorkRecord.task_id).where(
        TaskWorkRecord.kind == "abort_failure", TaskWorkRecord.project_id == project_id, *target,
    ))).all()
    if queued_admin is None and (project_id in engine._unproven_abort_failures or (not local and not records)):
        raise cleanup_work.blocked()
    if len({tid for _, tid in records}) != len(records):
        raise cleanup_work.blocked()
    for key, task_id in records:
        if project_id is not None:
            await project_lifecycle.lock_record(db, project_id)
        row = await db.get(TaskWorkRecord, key, populate_existing=True)
        if row is None:
            local.pop(task_id, None)
            continue
        snapshot = await cleanup_work.restore_snapshot(db, engine, row)
        if (original := local.pop(task_id, None)) is not None and original != snapshot:
            raise cleanup_work.blocked()
        previous, previous_owner = row.state, row.worker_key
        effect_applied = previous in ("failed_applied", "retry_applied")
        claimable = (TaskWorkRecord.state.in_(("failed", "failed_applied"))
                     | (TaskWorkRecord.state.in_(("retrying", "retry_applied"))
                        & workers.confirmed_terminal(TaskWorkRecord.worker_key)))
        worker = await engine.request_worker.require(db)
        attempt = str(uuid4())
        owned = (TaskWorkRecord.kind == "abort_failure", TaskWorkRecord.work_key == key)
        changed = await db.execute(update(TaskWorkRecord).where(
            *owned, TaskWorkRecord.state == previous, TaskWorkRecord.worker_key == previous_owner, claimable,
        ).values(state="retry_applied" if effect_applied else "retrying", worker_key=worker, operation_id=attempt)
          .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise cleanup_work.blocked()
        owned = (*owned, TaskWorkRecord.worker_key == worker, TaskWorkRecord.operation_id == attempt)
        try:
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                if not await db.scalar(select(TaskWorkRecord.work_key).where(*owned)):
                    raise
                await db.rollback()
            with cleanup_work.finishing_retry(key, worker, attempt):
                resolved = await _retry_failed_cleanup(db, engine, snapshot, work_key=key, worker_key=worker,
                                                       effect_applied=effect_applied)
            if resolved:
                # 管理员已核实同一轮端态时也可消除原屏障。
                await db.execute(delete(TaskWorkRecord).where(*owned))
                await db.commit()
        finally:
            await cleanup_work.relinquish_retry(db, engine, key, worker, attempt, snapshot.worker_key)
    for snapshot in local.values():
        await _retry_failed_cleanup(db, engine, snapshot)
    await db.commit()


async def stop_project_tasks(db: AsyncSession, engine: "TaskEngine", project_id: int) -> int:
    """冻结准入后向本作品全部原执行者登记取消，再等待它们及请求侧清理退出。"""
    from app.services.task_engine import owner_cancellation

    stopped = set()
    intent = db.info.get("project_deletion_intent")
    pending = (Task.project_id == project_id, or_(Task.status.in_(OPEN_TASK_STATUSES),
               Task.admin_cancel_operation_id.is_not(None), Task.execution_key.is_not(None)))
    try:
        async with asyncio.timeout(engine.cancel_wait_seconds):
            if intent is not None:
                if intent.project_id != project_id:
                    raise ConflictError("作品删除请求与排空目标不一致", extra={"code": "project_tasks_not_stopped"})
                await intent.lock(db, engine)
            await retry_project_cleanup_failures(db, engine, project_id)
            await db.rollback()
            while True:
                tasks = list(await db.scalars(select(Task).where(*pending).order_by(Task.id)
                                              .execution_options(populate_existing=True)))
                running = [task for task in tasks if task.status == "running"
                           and task.admin_cancel_operation_id is None and task.cancel_requested_at is None]
                recoveries = [(task.id, task.statistics_key, task.queued_at, task.execution_key) for task in tasks
                              if task.status == "running" and task.cancel_requested_at is not None]
                if any(task.status == "running" and task.recovered_at is not None for task in tasks):
                    raise ConflictError("任务保存结果尚未确认，作品未删除", extra={"code": "project_tasks_not_stopped"})
                ready = [(task.id, task.statistics_key, task.queued_at) for task in tasks
                         if task.status in {"queued", "paused"} and task.admin_cancel_operation_id is None]
                admins = [(task.statistics_key, task.admin_cancel_operation_id) for task in tasks
                          if task.statistics_key is not None and task.admin_cancel_operation_id is not None]
                # 已受理的本人/管理员意图也加入本次排空，审计按原轮次去重。
                stopped.update((task.id, task.statistics_key, task.queued_at) for task in tasks
                               if (task.admin_cancel_operation_id is not None or task.cancel_requested_at is not None)
                               and not (task.status in FINISHED_TASK_STATUSES and task.execution_key is not None))
                if running:
                    # 旧收尾可凭自己的资源快照重试；停止其他 running 的新授权必须来自本作品的冻结请求。
                    if intent is None:
                        raise ConflictError("缺少本次作品删除凭据，不能取消新的运行中任务",
                                            extra={"code": "project_tasks_not_stopped"})
                    await intent.lock(db, engine)
                    signals = []
                    owners = set()
                    for task in running:
                        if await owner_cancellation.record_request(db, task):
                            owners.add(task.worker_key)
                            stopped.add((task.id, task.statistics_key, task.queued_at))
                            recoveries.append((task.id, task.statistics_key, task.queued_at, task.execution_key))
                            if task.worker_key == engine.lease.worker_key and engine.lease.valid:
                                signals.append((task.id, task.statistics_key, task.queued_at, task.execution_key))
                    try:
                        await db.commit()
                    except (asyncio.CancelledError, Exception):
                        for identity in recoveries:
                            engine.request_worker.recover_cancel(*identity)
                        raise
                    for worker_key in owners:
                        engine.peer_wakes.schedule(worker_key)
                    for tid, key, stamp, execution in signals:
                        engine.signal_running_cancel(tid, statistics_key=key, expected_queued_at=stamp,
                                                     expected_execution_key=execution)
                    engine.notify()
                else:
                    await db.rollback()
                for identity in recoveries:
                    engine.request_worker.recover_cancel(*identity)
                for key, operation in admins:
                    engine.request_admin_cancel(key, operation)
                for tid, key, stamp in ready:
                    if intent is not None:
                        await intent.lock(db, engine)
                    task = await db.scalar(select(Task).where(
                        Task.id == tid, Task.statistics_key == key, Task.queued_at == stamp,
                        Task.project_id == project_id, Task.status.in_(("queued", "paused")),
                        Task.admin_cancel_operation_id.is_(None),
                    ).execution_options(populate_existing=True))
                    if task is not None:
                        try:
                            await _cancel_loaded_task(db, engine, task)
                            stopped.add((tid, key, stamp))
                        except ConflictError as exc:
                            if (exc.extra or {}).get("code") == "task_cleanup_unproven":
                                raise ConflictError("任务收尾缺少原始身份，作品未删除",
                                                    extra={"code": "project_tasks_not_stopped"}) from None
                    await db.rollback()
                if await cleanup_work.has_failure(db, engine, project_id):
                    raise ConflictError("任务收尾失败，作品未删除", extra={"code": "project_tasks_not_stopped"})
                work = engine.project_work(project_id)
                active = await project_work_exists(db, project_id)
                remaining = (await db.execute(select(Task.status, Task.execution_key).where(*pending).limit(1))).first()
                if not work and not active:
                    if remaining is None:
                        break
                    if remaining.status in FINISHED_TASK_STATUSES and remaining.execution_key is not None:
                        raise ConflictError("任务仍有未完成的安全收尾，作品未删除",
                                            extra={"code": "project_tasks_not_stopped"})
                await db.rollback()
                if work:
                    await asyncio.wait(work, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(0.05)
            generating = await db.scalar(select(Chapter.id).where(
                Chapter.project_id == project_id, Chapter.status == "generating"
            ).limit(1))
            if generating is not None:
                raise ConflictError("章节生成状态尚未安全收尾，作品未删除", extra={"code": "project_tasks_not_stopped"})
    except TimeoutError as exc:
        raise ConflictError(
            "等待任务停止超时，作品未删除；已取消任务不会自动恢复，请稍后重试",
            extra={"code": "project_tasks_not_stopped"},
        ) from exc
    except ConflictError:
        raise
    except Exception:
        raise ConflictError(
            "任务收尾失败，作品未删除；已取消任务不会自动恢复",
            extra={"code": "project_tasks_not_stopped"},
        ) from None
    return len(stopped)


async def _abort_loaded_task(engine: "TaskEngine", task: Task) -> None:
    snapshot = engine.capture_abort_snapshot(task)
    async def abort(ctx):
        handler = get_handler(task.kind)
        if task.kind in ("chapter.generate", "chapter.polish", "chapter.revise"):
            from app.services.task_engine.handlers import chapter as chapter_handler

            execution = engine._running.get(task.id)
            if execution is not None and not execution.done():
                raise ConflictError("章节任务仍在执行，不能清理")
            # 只在原执行体已停止且持久字段完整时恢复；缺失身份的旧记录不得猜测。
            claim = chapter_handler.capture_running_claim(task)
            await handler.on_abort(ctx, task, claim=claim)
        else:
            await handler.on_abort(ctx, task)

    try:
        await cleanup_work.apply_effect(engine, abort)
    except Exception:
        if task.project_id is not None:
            await engine.record_abort_failure(task.project_id, snapshot)
        raise


def _active_resource_conflict(exc: IntegrityError) -> bool:
    original = exc.orig
    cause = getattr(original, "__cause__", None)
    name = getattr(original, "constraint_name", None) or getattr(cause, "constraint_name", None)
    name = name or getattr(getattr(cause, "diag", None), "constraint_name", None)
    return name == "uq_tasks_active_resource" or str(original) == "UNIQUE constraint failed: tasks.resource_key"
