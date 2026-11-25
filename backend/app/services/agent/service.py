"""会话与审核入口：业务效果和提案状态始终在同一事务中持久化。"""

import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import String, cast, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from app.models import AgentChange, AgentConversation, AgentMessage, GenerationLog, Task, TaskWorkRecord
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.agent import (
    ChangeAction,
    ChangeExpectation,
    ChangeOut,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
    MessageAccepted,
    MessageOut,
    SendMessage,
    TurnAction,
    TurnChangesOut,
)
from app.schemas.chapter_version import GenerationLogOut
from app.services import authorization_service as authz
from app.services import (
    generation_log_service,
    personal_projection,
    project_ai_config,
    project_lifecycle,
    task_service,
)
from app.services.agent import access as agent_access
from app.services.agent import conversation_operations, effects, history, mcp_tools, transactions
from app.services.agent.external_activity import ExternalActivity, drain
from app.services.agent.tools import get_tool
from app.services.agent.tools import tasks as tasks_tool
from app.services.agent.tools.base import ToolCtx, TransactionSession
from app.services.ai_dispatch import VisibilityPolicyError, read_visibility_stamp, require_visibility_stamp
from app.services.system_settings_snapshot import SettingsUnavailable
from app.services.task_engine import workers
from app.services.task_engine.drain import TaskAdmissionClosed, require_task_admission_open
from app.services.task_engine.registry import TaskContext

if TYPE_CHECKING:
    from app.services.collaboration_task_access import GrantIdentity
    from app.services.setting_ai_context import AiScope
    from app.services.task_engine.engine import TaskEngine

logger = logging.getLogger(__name__)


# 外部（MCP）调用先提交 executing 再发起网络请求；进程在调用期间崩溃或请求被取消时提案会停在 executing。
# 工作已退出或属于旧格式时，至少等待两分钟再按原凭据恢复；可证明仍在途的健康调用继续执行。
EXTERNAL_STALE_AFTER = timedelta(minutes=2)
EXTERNAL_INTERRUPTED = "外部调用结果未知，请核对"
EXTERNAL_RETURNED_NOT_APPLIED = "外部调用已返回，结果未应用"
EXTERNAL_NOT_SENT = "外部调用未发送"
_EXTERNAL_TASK_KEY = "_external_task_key"
_EXTERNAL_OUTCOME_UNKNOWN = "_external_outcome_unknown"
_EXTERNAL_EXECUTION = "_external_execution"
_EXTERNAL_SNAPSHOT_MAC = "_external_snapshot_mac"
_AGENT_ACTOR_TOKEN_VERSION = "_agent_actor_token_version"
_TASK_ACTOR_TOKEN_VERSION = "_task_actor_token_version"
# 协作轮次受限收尾（E4-i 裁决 11）作废待确认提案的固定文案；会话提案列表与模型回执只见此文案。
COLLABORATION_CHANGE_VOIDED = "协作授权已失效，提案已作废"
_EXTERNAL_PRIVATE_KEYS = frozenset({
    transactions.EXTERNAL_CLAIM_KEY, _EXTERNAL_TASK_KEY, _EXTERNAL_OUTCOME_UNKNOWN, _EXTERNAL_EXECUTION,
    _EXTERNAL_SNAPSHOT_MAC,
})


async def void_revoked_turn_changes(db: AsyncSession, *, task_id: int, task_key: str, project_id: int) -> list[int]:
    """协作轮次授权失效后的受限收尾：作废本轮（原 Task id + UUID 精确匹配）仍待确认的提案。

    调用方已按 gate → actor/作者 User 升序 → 发送写门禁（可选）→ 作品父行 → Task 持锁，并已在锁内确认原协作
    轮次失效；与任务终态同事务提交（运行中轮次由 on_abort 在终态之前单独提交，失效不可逆，先后不影响结论）。

    处置：仅 ``status='proposed'`` 且没有任何外部执行内部标记的行改为 ``failed``、``error``/``result.error`` 为
    固定文案（start_task 的来源证明随之清除，不能再被确认或受理）。``executing``、带外部标记（未知结果屏障）或
    其他状态的行一律不动，``has_unsettled_external_change`` / ``require_no_unknown_external`` 的结论不变。
    不写消息、不改作品、不发送。返回被作废的提案 ID。
    """
    rows = list(await db.scalars(select(AgentChange).where(
        AgentChange.turn_task_id == task_id, AgentChange.turn_task_key == task_key,
        AgentChange.project_id == project_id, AgentChange.status == "proposed",
    ).order_by(AgentChange.id).with_for_update().execution_options(populate_existing=True)))
    voided: list[int] = []
    for row in rows:
        result = row.result
        if result is not None and not isinstance(result, dict):
            continue
        if isinstance(result, dict) and _EXTERNAL_PRIVATE_KEYS.intersection(result):
            continue
        changed = await db.execute(update(AgentChange).where(
            AgentChange.id == row.id, AgentChange.change_key == row.change_key, AgentChange.status == "proposed",
            AgentChange.turn_task_id == task_id, AgentChange.turn_task_key == task_key,
        ).values(status="failed", error=COLLABORATION_CHANGE_VOIDED,
                 result={"error": COLLABORATION_CHANGE_VOIDED}).execution_options(synchronize_session=False))
        if changed.rowcount == 1:
            voided.append(row.id)
    return voided


def _unknown_external_result(claim_id: str, task_key: str, *, snapshot_mac: str | None = None) -> dict:
    result = {"error": EXTERNAL_INTERRUPTED, _EXTERNAL_OUTCOME_UNKNOWN: True,
              transactions.EXTERNAL_CLAIM_KEY: claim_id, _EXTERNAL_TASK_KEY: task_key}
    if snapshot_mac is not None:
        result[_EXTERNAL_SNAPSHOT_MAC] = snapshot_mac
    return result


def _aware(value: datetime) -> datetime:
    # SQLite 读出的是 naive UTC
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _json_equals(column, value, dialect: str):
    if value is None:
        json_type = func.jsonb_typeof(cast(column, JSONB)) if dialect == "postgresql" else func.json_type(column)
        return or_(column.is_(None), json_type == "null")
    if dialect == "postgresql":
        return cast(column, JSONB) == value
    # SQLite 原始 JSON 来自 SQLAlchemy 的默认 ASCII 转义序列化；json() 只压缩空白，
    # 不会把转义 Unicode 与字面 Unicode 视作同一文本。
    return func.json(column) == func.json(json.dumps(value))


async def recover_external(
    db: AsyncSession, *, task_id: int | None = None, task_key: str | None = None, stale_only: bool = True,
    engine: "TaskEngine | None" = None,
) -> int:
    """把中断的外部调用（kind=external、仍为 executing）标为 failed，返回处理的条数；不 commit。

    新调用依原 worker 的停止决定或明确退出且超时的记录恢复；旧调用保留两分钟保护。
    收尾只标记结果未知；重新调用外部服务可能重复已经完成的操作。
    """
    if stale_only:
        from app.services.task_engine.engine import TaskEngine

        if not isinstance(engine, TaskEngine) or engine.ctx.task_engine is not engine:
            return 0
    stmt = select(
        AgentChange.id, AgentChange.change_key, AgentChange.project_id, AgentChange.conversation_id,
        AgentChange.turn_task_id,
        AgentChange.turn_task_key,
        AgentChange.tool_call_id, AgentChange.tool_name, AgentChange.arguments, AgentChange.preview,
        AgentChange.executed_at, AgentChange.result, AgentChange.external_worker_key,
        Task.statistics_key, Task.user_id.label("task_user_id"), Task.project_id.label("task_project_id"),
        Task.queued_at.label("task_queued_at"), Task.execution_key.label("task_execution_key"),
        Task.input.label("task_input"), Task.checkpoint.label("task_checkpoint"),
        Task.step_cursor.label("task_step_cursor"), Task.chapter_id.label("task_chapter_id"),
        Task.retry_of_id.label("task_retry_of_id"), Task.resource_key.label("task_resource_key"),
        Task.serial_key.label("task_serial_key"), Task.kind.label("task_kind"),
        AgentConversation.user_id.label("conversation_user_id"),
        AgentConversation.project_id.label("conversation_project_id"),
    ).outerjoin(Task, Task.id == AgentChange.turn_task_id).outerjoin(
        AgentConversation, AgentConversation.id == AgentChange.conversation_id,
    ).where(AgentChange.kind == "external", AgentChange.status == "executing")
    if task_id is not None:
        stmt = stmt.where(AgentChange.turn_task_id == task_id)
    if task_key is not None:
        stmt = stmt.where(Task.statistics_key == task_key,
                          AgentChange.result[_EXTERNAL_TASK_KEY].as_string() == task_key)
    cutoff = datetime.now(UTC) - EXTERNAL_STALE_AFTER
    recovered = []
    for row in await db.execute(stmt):
        automatic = isinstance(row.result, dict) and _EXTERNAL_EXECUTION in row.result
        if not mcp_tools.valid_external_claim_result(row.result, automatic=automatic):
            continue
        claim = row.result.get(transactions.EXTERNAL_CLAIM_KEY) if isinstance(row.result, dict) else None
        try:
            proven = (isinstance(claim, str) and str(UUID(claim)) == claim
                      and isinstance(row.statistics_key, str) and str(UUID(row.statistics_key)) == row.statistics_key)
        except ValueError:
            proven = False
        # 旧记录若没有完整 claim/任务 UUID/起始时间，不能证明当前行仍是扫描时的那次执行。
        if (not proven or row.executed_at is None or row.turn_task_id is None or row.task_queued_at is None
                or row.result.get(_EXTERNAL_TASK_KEY) != row.statistics_key or row.turn_task_key != row.statistics_key
                or row.task_project_id != row.project_id or row.conversation_project_id != row.project_id
                or row.task_user_id is None or row.task_user_id != row.conversation_user_id
                or row.task_kind != "agent.turn" or not isinstance(row.task_input, dict)
                or type(row.task_input.get(_AGENT_ACTOR_TOKEN_VERSION)) is not int
                or type(row.task_input.get(_TASK_ACTOR_TOKEN_VERSION)) is not int
                or row.task_input[_AGENT_ACTOR_TOKEN_VERSION] < 0
                or row.task_input[_AGENT_ACTOR_TOKEN_VERSION] != row.task_input[_TASK_ACTOR_TOKEN_VERSION]):
            continue
        if row.external_worker_key is None and stale_only and (
            _aware(row.executed_at) > cutoff or engine.project_work(row.project_id)
            or any(not work.done() for work in engine._cleanup_work.get(row.project_id, {}))
        ):
            # cleanup_scope 登记执行中的 HTTP 请求；project_work 自身会排除当前协程，故还须包含自身。
            continue
        task_conditions = [
            Task.id == row.turn_task_id, Task.statistics_key == row.statistics_key,
            Task.project_id == row.project_id, Task.user_id == row.task_user_id,
        ]
        marker = row.result.get(_EXTERNAL_EXECUTION)
        dialect = db.get_bind().dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            # 无法验证 JSON 键是否缺失时不能把自动调用误认为旧手动调用。
            continue
        snapshot_mac = row.result.get(_EXTERNAL_SNAPSHOT_MAC)
        try:
            task_evidence = mcp_tools.task_snapshot_values({
                "input": row.task_input, "checkpoint": row.task_checkpoint, "step_cursor": row.task_step_cursor,
                "chapter_id": row.task_chapter_id, "retry_of_id": row.task_retry_of_id,
                "resource_key": row.task_resource_key, "serial_key": row.task_serial_key,
            })
            change_evidence = mcp_tools._json({"tool_name": row.tool_name, "arguments": row.arguments,
                                               "preview": row.preview})
            expected_mac = mcp_tools.external_snapshot_mac(
                db.info.get("agent_view_hmac_key"), actor_id=row.task_user_id,
                token_version=row.task_input[_AGENT_ACTOR_TOKEN_VERSION], project_id=row.project_id,
                conversation_id=row.conversation_id, task_id=row.turn_task_id, task_key=row.statistics_key,
                queued_at=row.task_queued_at,
                execution_key=row.task_execution_key if _EXTERNAL_EXECUTION in row.result else None,
                change_id=row.id, change_key=row.change_key, claim_id=claim, tool_call_id=row.tool_call_id,
                started_at=row.executed_at, task_evidence=task_evidence, change_evidence=change_evidence,
                worker_key=row.external_worker_key,
            )
        except (TypeError, ValueError, RecursionError, VisibilityPolicyError):
            continue
        if (not isinstance(snapshot_mac, str) or len(snapshot_mac) != 64
                or any(char not in "0123456789abcdef" for char in snapshot_mac)
                or not hmac.compare_digest(snapshot_mac, expected_mac)):
            continue
        task_conditions.extend((
            Task.queued_at == row.task_queued_at, Task.kind == row.task_kind,
            Task.step_cursor == row.task_step_cursor, Task.chapter_id == row.task_chapter_id,
            Task.retry_of_id == row.task_retry_of_id, Task.resource_key == row.task_resource_key,
            Task.serial_key == row.task_serial_key, _json_equals(Task.input, row.task_input, dialect),
            _json_equals(Task.checkpoint, row.task_checkpoint, dialect),
        ))
        if _EXTERNAL_EXECUTION in row.result:
            try:
                if (not isinstance(marker, dict)
                        or set(marker) != {"queued_at", "execution_key", "actor_token_version"}
                        or not isinstance(marker["queued_at"], str)
                        or not isinstance(marker["execution_key"], str)
                        or type(marker["actor_token_version"]) is not int or marker["actor_token_version"] < 0
                        or not isinstance(row.task_input, dict)
                        or type(row.task_input.get(_AGENT_ACTOR_TOKEN_VERSION)) is not int
                        or row.task_input[_AGENT_ACTOR_TOKEN_VERSION] != marker["actor_token_version"]
                        or type(row.task_input.get(_TASK_ACTOR_TOKEN_VERSION)) is not int
                        or row.task_input[_TASK_ACTOR_TOKEN_VERSION] != marker["actor_token_version"]
                        or str(UUID(marker["execution_key"])) != marker["execution_key"]
                        or row.task_queued_at is None
                        or marker["queued_at"] != _aware(row.task_queued_at).astimezone(UTC).isoformat()
                        or marker["execution_key"] != row.task_execution_key):
                    continue
            except ValueError:
                continue
            task_conditions.extend((
                Task.queued_at == row.task_queued_at, Task.execution_key == row.task_execution_key,
                Task.input[_AGENT_ACTOR_TOKEN_VERSION].as_integer() == marker["actor_token_version"],
                Task.input[_TASK_ACTOR_TOKEN_VERSION].as_integer() == marker["actor_token_version"],
            ))
            if dialect == "postgresql":
                task_conditions.extend(
                    cast(Task.input[key], String) == str(marker["actor_token_version"])
                    for key in (_AGENT_ACTOR_TOKEN_VERSION, _TASK_ACTOR_TOKEN_VERSION)
                )
                marker_condition = cast(AgentChange.result[_EXTERNAL_EXECUTION], JSONB) == marker
            else:
                task_conditions.extend(
                    func.json_type(Task.input, f'$."{key}"') == "integer"
                    for key in (_AGENT_ACTOR_TOKEN_VERSION, _TASK_ACTOR_TOKEN_VERSION)
                )
                marker_condition = func.json(AgentChange.result[_EXTERNAL_EXECUTION]) == func.json(json.dumps(marker))
        elif dialect == "postgresql":
            task_conditions.append(Task.execution_key.is_(None))
            marker_condition = ~cast(AgentChange.result, JSONB).has_key(_EXTERNAL_EXECUTION)  # noqa: W601
        else:
            task_conditions.append(Task.execution_key.is_(None))
            marker_condition = func.json_type(AgentChange.result, f'$."{_EXTERNAL_EXECUTION}"').is_(None)
        activity = None
        recovery_conditions = [AgentChange.external_worker_key == row.external_worker_key]
        if row.external_worker_key is not None:
            activity = ExternalActivity(claim, row.external_worker_key, row.project_id, row.turn_task_id,
                                        row.statistics_key, row.task_queued_at,
                                        row.task_execution_key if automatic else None, row.change_key)
            eligible = workers.confirmed_terminal(row.external_worker_key)
            if _aware(row.executed_at) <= cutoff:
                registered = workers.registered_worker_exists(row.external_worker_key)
                eligible |= (TaskWorkRecord.state == "exited") & registered
            recovering = select(TaskWorkRecord.work_key).where(
                *activity.conditions(), TaskWorkRecord.state.in_(("active", "exited")), eligible,
            ).exists()
            if not await db.scalar(select(recovering)):
                continue
            recovery_conditions.append(recovering)
        # 锁住扫描到的原任务直至提交；单条 UPDATE 的 EXISTS 本身不能阻止另一事务先提交任务替换。
        if await db.scalar(select(Task.id).where(*task_conditions).with_for_update()) is None:
            continue
        original_task = exists(select(Task.id).where(*task_conditions))
        original_conversation = exists(select(AgentConversation.id).where(
            AgentConversation.id == row.conversation_id, AgentConversation.project_id == row.project_id,
            AgentConversation.user_id == row.task_user_id,
        ))
        recovered_result = _unknown_external_result(claim, row.statistics_key, snapshot_mac=snapshot_mac)
        if _EXTERNAL_EXECUTION in row.result:
            recovered_result[_EXTERNAL_EXECUTION] = marker
        result = await db.execute(update(AgentChange).where(
            AgentChange.id == row.id, AgentChange.change_key == row.change_key,
            AgentChange.project_id == row.project_id,
            AgentChange.conversation_id == row.conversation_id, AgentChange.turn_task_id == row.turn_task_id,
            AgentChange.turn_task_key == row.turn_task_key,
            AgentChange.tool_call_id == row.tool_call_id, AgentChange.kind == "external",
            AgentChange.status == "executing", AgentChange.executed_at == row.executed_at,
            AgentChange.tool_name == row.tool_name,
            _json_equals(AgentChange.arguments, row.arguments, dialect),
            _json_equals(AgentChange.preview, row.preview, dialect),
            _json_equals(AgentChange.result, row.result, dialect),
            AgentChange.result[transactions.EXTERNAL_CLAIM_KEY].as_string() == claim,
            AgentChange.result[_EXTERNAL_TASK_KEY].as_string() == row.statistics_key,
            AgentChange.result[_EXTERNAL_SNAPSHOT_MAC].as_string() == snapshot_mac,
            original_task, original_conversation, marker_condition, *recovery_conditions,
        ).values(status="failed", error=EXTERNAL_INTERRUPTED,
                 result=recovered_result, external_worker_key=None)
            .execution_options(synchronize_session=False))
        if result.rowcount == 1:
            if activity is not None:
                await activity.finish(db)
            recovered.append(row.id)
    if recovered:
        logger.info("已暂存 %s 项中断外部调用的恢复，待事务提交：%s", len(recovered), recovered)
    return len(recovered)


async def _lock_owned_project(
    db: AsyncSession, user_id: int, project_id: int, *, grant: "GrantIdentity | None" = None,
):
    """先重验原始令牌并持有 User 写屏障，再取得作品锁并锁后判定 Agent 能力（作者或原轮次 editor）。

    E4i-d：协作者按原轮次 grant 放行；授权失效为 409 `permission_denied`（裁决 14）。仅用于已经过入口
    归属核对（会话 / 轮次属于本人）之后的提交后复核与续跑，不能作为首个入口判定。
    """
    return await agent_access.lock_agent_write(
        db, user_id, project_id, grant=grant, visibility_write=False, allow_collaborator=True, on_denied="changed",
    )


async def _conversation_entry(
    db: AsyncSession, user_id: int, project_id: int, conversation_id: int, *, lock: bool,
    visibility_write: bool = False,
) -> tuple[agent_access.AgentEntry, AgentConversation]:
    # 私人会话作用域：作者或 accepted 成员（含 viewer，裁决 4）；仅管理员人工来源、已撤权一律 404。
    entry = await agent_access.lock_agent_entry(
        db, user_id, project_id, lock=lock, visibility_write=visibility_write,
    )
    row = await db.scalar(
        select(AgentConversation)
        .where(
            AgentConversation.id == conversation_id,
            AgentConversation.project_id == project_id,
            AgentConversation.user_id == user_id,
            AgentConversation.instance_key != "",
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundError("会话不存在")
    return entry, row


async def owned_conversation(
    db: AsyncSession, user_id: int, project_id: int, conversation_id: int, *, lock: bool = False,
    visibility_write: bool = False,
) -> AgentConversation:
    """本人会话。`lock=True` 是会话级写入（改名、删除、发送）：锁后要求当前轮次 Agent 写能力且会话属于当前
    邀请轮次，失败 409 `permission_denied`；不属于本人、无私人作用域仍 404。

    visibility_write：随后同事务要经中央受理取发送写门禁时，门禁须先于作品父行（E4i-b 锁序）。
    """
    entry, row = await _conversation_entry(
        db, user_id, project_id, conversation_id, lock=lock, visibility_write=visibility_write,
    )
    if lock:
        agent_access.require_conversation_write(entry, user_id, row)
    return row


async def _precheck_conversation(db: AsyncSession, user_id: int, project_id: int, conversation_id: int) -> str:
    entry, row = await _conversation_entry(db, user_id, project_id, conversation_id, lock=False)
    agent_access.require_conversation_write(entry, user_id, row)
    conversation_key = row.instance_key
    await db.rollback()
    return conversation_key


async def _conversation_key(db: AsyncSession, user_id: int, project_id: int, conversation_id: int) -> str:
    key = await db.scalar(select(AgentConversation.instance_key).where(
        AgentConversation.id == conversation_id, AgentConversation.project_id == project_id,
        AgentConversation.user_id == user_id,
    ))
    if not key:
        raise NotFoundError("会话不存在")
    return key


async def active_turn(db: AsyncSession, conversation_id: int) -> Task | None:
    return await db.scalar(
        select(Task)
        .where(
            Task.resource_key == f"agent:{conversation_id}",
            Task.status.in_(OPEN_TASK_STATUSES),
        )
        .order_by(Task.id.desc())
        .limit(1)
    )


async def public_changes(
    db: AsyncSession, user_id: int, project_id: int, rows: list[AgentChange],
) -> list[ChangeOut]:
    """对外只投影当前用户会话，绝不修改持久 effects（完整快照用于撤销）。"""
    with db.no_autoflush:
        payload, proofs = await _prepare_committed_changes(db, user_id, project_id, rows, require_task_proof=False)
        proofs["owner_only"] = False
        projected = await personal_projection.project_agent_payload(db, user_id, project_id, payload, proofs)
        return _public_change_models(projected, proofs, db)


def _view_version(item: dict, original: dict, proofs: dict, key: bytes) -> str:
    # guard 中的完整旧行 hash 可能被枚举；用域分离密钥认证它，不能公开普通哈希。
    visible = {key: item[key] for key in (
        "tool_name", "kind", "arguments", "preview", "turn_task_id", "turn_task_key",
    )}
    preview = original.get("preview")
    guard = {"present": isinstance(preview, dict) and "guards" in preview,
             "value": preview.get("guards") if isinstance(preview, dict) else None}
    signed = {"actor": proofs["actor"], "project": proofs["project"],
              "change_key": original["change_key"], "visible": visible, "guard": guard}
    data = json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(key, b"ainovel/agent-view/v1\x00" + data, hashlib.sha256).hexdigest()


def _public_change_models(projected: list[dict], proofs: dict, db: AsyncSession) -> list[ChangeOut]:
    signing_key = db.info.get("agent_view_hmac_key")
    if not isinstance(signing_key, bytes) or len(signing_key) != 32:
        # 没有启动时的派生密钥则拒绝投影，不能退化成可枚举的无密钥摘要。
        raise transactions.AgentTransactionError("服务器内部错误")
    result = []
    for item in projected:
        original = proofs["changes"].get(item.get("id"))
        key = original.get("change_key") if original else None
        try:
            if not isinstance(key, str) or str(UUID(key)) != key or UUID(key).version != 4:
                raise ValueError
        except ValueError:
            raise NotFoundError("修改提案不存在") from None
        item["change_key"] = key
        item["view_version"] = _view_version(item, original, proofs, signing_key)
        result.append(ChangeOut.model_validate(item))
    return result


async def _require_change_action(
    db: AsyncSession, user_id: int, project_id: int, row: AgentChange, expected: ChangeAction, *,
    require_visible_arguments: bool = False,
) -> ChangeOut:
    public = (await public_changes(db, user_id, project_id, [row]))[0]
    if public.change_key != str(expected.change_key) or public.view_version != expected.view_version:
        raise ConflictError("提案身份或预览已变化，请刷新后重新确认", extra={"code": "agent_change_changed"})
    safe_preview = {key: value for key, value in row.preview.items() if key != "guards"} \
        if isinstance(row.preview, dict) else row.preview
    if require_visible_arguments and (public.arguments != row.arguments or public.preview != safe_preview):
        # 不能仅让用户确认被隐藏了关键参数的提案，更不能通过对私人字段散列绕开这一条件。
        raise ConflictError("提案包含当前不可见的参数，无法确认", extra={"code": "agent_change_not_visible"})
    if row.turn_task_id is not None and (public.turn_task_id is None or public.turn_task_key is None):
        raise ConflictError("原任务身份无法确认，请刷新后重试", extra={"code": "agent_task_identity_required"})
    return public


async def _require_turn_actions(
    db: AsyncSession, user_id: int, project_id: int, rows: list[AgentChange], expected: TurnAction, *,
    require_visible_arguments: bool = False,
) -> dict[int, ChangeExpectation]:
    wanted = {item.id: item for item in expected.changes}
    if len(wanted) != len(expected.changes) or set(wanted) != {row.id for row in rows}:
        raise ConflictError("本轮提案集合已变化，请刷新后重新确认", extra={"code": "agent_change_set_changed"})
    for row in rows:
        public = await _require_change_action(
            db, user_id, project_id, row, wanted[row.id], require_visible_arguments=require_visible_arguments,
        )
        if public.turn_task_key != str(expected.task_key):
            raise ConflictError("原任务身份无法确认，请刷新后重试", extra={"code": "agent_task_identity_required"})
    return wanted


async def _prepare_committed_changes(
    db: AsyncSession, user_id: int, project_id: int, rows: list[AgentChange], *, require_task_proof: bool = True,
) -> tuple[list[dict], dict]:
    """在异步续跑之前冻结原提案，供最终新快照比对完整内容和身份。"""
    if any(row.project_id != project_id for row in rows):
        raise NotFoundError("修改提案不存在")
    conversation_ids = {row.conversation_id for row in rows}
    conversations = (await db.execute(select(AgentConversation.id, AgentConversation.instance_key).where(
        AgentConversation.id.in_(conversation_ids), AgentConversation.project_id == project_id,
        AgentConversation.user_id == user_id, AgentConversation.instance_key != "",
    ))).all() if conversation_ids else []
    if {row.id for row in conversations} != conversation_ids:
        raise NotFoundError("修改提案不存在")
    originals = [ChangeOut.model_validate(row).model_dump(mode="json") for row in rows]
    task_ids = {row["turn_task_id"] for row in originals if row["turn_task_id"] is not None}
    tasks = (await db.execute(select(Task.id, Task.statistics_key).where(
        Task.id.in_(task_ids), Task.project_id == project_id, Task.user_id == user_id,
    ))).all() if task_ids else []
    if require_task_proof and ({row.id for row in tasks if row.statistics_key} != task_ids or any(
        row["turn_task_id"] is not None and not row["turn_task_key"] for row in originals
    )):
        raise NotFoundError("修改提案不存在")
    proofs = {
        "actor": user_id, "project": project_id,
        "changes": {row["id"]: row for row in originals},
        "conversations": {row.id: row.instance_key for row in conversations},
        "tasks": {row.id: row.statistics_key for row in tasks},
        "require_task_proof": require_task_proof,
        # E4i-d：Agent 入口已对 accepted 成员开放；最终快照仍要求作者或 accepted 成员且会话 / 轮次属于本人。
        "owner_only": False,
    }
    payload = []
    for row in originals:
        safe = dict(transactions.public_change_payload(row))
        if isinstance(safe.get("preview"), dict):
            safe["preview"] = {key: value for key, value in safe["preview"].items() if key != "guards"}
        payload.append(safe)
    return payload, proofs


async def list_conversations(db: AsyncSession, user_id: int, project_id: int) -> list[ConversationOut]:
    # 作者或 accepted 成员（含 viewer 只读，裁决 4）只看本人会话；管理员人工来源不能旁路私人数据。
    entry = await agent_access.lock_agent_entry(db, user_id, project_id, lock=False)
    epoch = (await read_visibility_stamp(db, project_id)).epoch
    rows = await db.execute(
        select(AgentConversation, history.has_history_expression())
        .where(
            AgentConversation.project_id == project_id,
            AgentConversation.user_id == user_id,
            AgentConversation.instance_key != "",
        )
        .order_by(AgentConversation.last_message_at.desc(), AgentConversation.id.desc())
    )
    return [
        ConversationOut.model_validate(row).model_copy(
            update={"history_sealed": row.ai_history_epoch != epoch and bool(has_history),
                    "writable": agent_access.conversation_writable(entry, user_id, row)},
        )
        for row, has_history in rows
    ]


async def create_conversation(
    db: AsyncSession, user_id: int, project_id: int, req: ConversationCreate
) -> ConversationOut:
    entry = await agent_access.lock_agent_entry(db, user_id, project_id)
    grant = entry.current_grant(user_id)

    def readonly() -> None:
        # 尚无本人会话：与其它 AI 入口（require_project ai.use）一致，accepted viewer 为 403。
        raise ForbiddenError("当前作品没有执行该操作的权限", extra={"code": "project_readonly"})

    agent_access.check_agent_write(entry, user_id, grant, deny=readonly)
    epoch = (await read_visibility_stamp(db, project_id)).epoch
    previous = await db.scalar(
        select(AgentConversation)
        .where(
            AgentConversation.project_id == project_id,
            AgentConversation.user_id == user_id,
            AgentConversation.instance_key != "",
        )
        .order_by(AgentConversation.id.desc())
        .limit(1)
    )
    row = AgentConversation(
        user_id=user_id,
        project_id=project_id,
        title=req.title.strip(),
        auto_execute=previous.auto_execute if previous else False,
        ai_history_epoch=epoch,
        collaboration_invitation_id=None if grant is None else grant[0],
    )
    db.add(row)
    await db.flush()
    conversation_key = row.instance_key
    await db.commit()
    await db.refresh(row)
    if row.instance_key != conversation_key:
        raise NotFoundError("会话不存在")
    # 锁内已判定写能力，新会话属于当前轮次。
    return ConversationOut.model_validate(row).model_copy(
        update={"writable": agent_access.conversation_writable(entry, user_id, row)},
    )


async def get_conversation(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, conversation_id: int
) -> ConversationDetail:
    entry, row = await _conversation_entry(db, user_id, project_id, conversation_id, lock=False)
    messages = await db.scalars(
        select(AgentMessage).where(AgentMessage.conversation_id == row.id).order_by(AgentMessage.id)
    )
    changes = await db.scalars(
        select(AgentChange).where(AgentChange.conversation_id == row.id).order_by(AgentChange.id)
    )
    task = await active_turn(db, row.id)
    if task is not None:
        from app.services.collaboration_task_access import task_visible_to_actor

        # 只读查看（viewer、旧邀请轮次，裁决 4、7）：本人未结束的旧轮次对其已不可见（不能再运行 / 订阅），
        # 详情照常返回历史与提案，只是不附带该活动任务。
        with db.no_autoflush:
            visible = await db.scalar(select(Task.id).where(Task.id == task.id, task_visible_to_actor(db, user_id)))
        if visible is None:
            task = None
    epoch = (await read_visibility_stamp(db, project_id)).epoch
    # writable 随快照进入最终投影核验：核验在新快照中按同一 SQL 事实重算，授权在两次读取间变化即失败关闭。
    conversation_snapshot = ConversationOut.model_validate(row).model_copy(
        update={"writable": agent_access.conversation_writable(entry, user_id, row)},
    ).model_dump(mode="json")
    public = dict(conversation_snapshot)
    public["history_sealed"] = await history.is_sealed(db, row, epoch)
    message_payload = [MessageOut.model_validate(message).model_dump(mode="json") for message in messages]
    change_payload, proofs = await _prepare_committed_changes(
        db, user_id, project_id, list(changes), require_task_proof=False,
    )
    proofs["conversation"] = {"id": row.id, "instance_key": row.instance_key,
                              "snapshot": conversation_snapshot, "sealed": public["history_sealed"]}
    proofs["messages"] = {message["id"]: message for message in message_payload}
    task_output = None
    if task is not None:
        proofs["turn"] = {"id": task.id, "statistics_key": task.statistics_key, "queued_at": task.queued_at,
                          "snapshot": personal_projection.agent_task_evidence(task)}
        task_output = (await task_service.task_out(db, engine, task)).model_dump(mode="json")
    projected = await personal_projection.project_agent_payload(
        db, user_id, project_id,
        {"conversation": public, "messages": message_payload, "changes": change_payload, "task": task_output},
        proofs,
    )
    return ConversationDetail(
        **projected["conversation"],
        messages=[MessageOut.model_validate(message) for message in projected["messages"]],
        changes=_public_change_models(projected["changes"], proofs, db), active_task=projected["task"],
    )


async def update_conversation(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, conversation_id: int,
    req: ConversationUpdate,
) -> ConversationOut:
    conversation_key = await _precheck_conversation(db, user_id, project_id, conversation_id)
    async with conversation_operations.hold(
        db, engine.request_worker, conversation_id, user_id, project_id, conversation_key=conversation_key,
    ):
        return await _update_conversation(db, user_id, project_id, conversation_id, req)


async def _update_conversation(
    db: AsyncSession, user_id: int, project_id: int, conversation_id: int, req: ConversationUpdate,
) -> ConversationOut:
    entry, row = await _conversation_entry(db, user_id, project_id, conversation_id, lock=True)
    agent_access.require_conversation_write(entry, user_id, row)
    conversation_key = row.instance_key
    await conversation_operations.guard(db, conversation_id, conversation_key=conversation_key)
    if req.title is not None:
        if not req.title.strip():
            raise BadRequestError("会话标题不能为空")
        row.title = req.title.strip()
    if req.auto_execute is not None:
        row.auto_execute = req.auto_execute
    if req.mcp_plugin_ids is not None:
        from app.models import McpPlugin

        ids = list(dict.fromkeys(req.mcp_plugin_ids))
        if ids and row.collaboration_invitation_id is not None:
            # 裁决 2：编辑者 V1 不开放 MCP（外部效果不可撤、未知结果屏障会阻挡作者删除作品）。
            raise ForbiddenError(
                "协作者暂不能在作品智能体中使用 MCP 插件", extra={"code": "collaborator_mcp_unavailable"},
            )
        owned = set(await db.scalars(select(McpPlugin.id).where(McpPlugin.id.in_(ids), McpPlugin.user_id == user_id)))
        if len(owned) != len(ids):
            raise NotFoundError("插件不存在")
        row.mcp_plugin_ids = ids or None
    await db.commit()
    await db.refresh(row)
    if row.instance_key != conversation_key:
        raise NotFoundError("会话不存在")
    epoch = (await read_visibility_stamp(db, project_id)).epoch
    return ConversationOut.model_validate(row).model_copy(
        update={"history_sealed": await history.is_sealed(db, row, epoch),
                "writable": agent_access.conversation_writable(entry, user_id, row)},
    )


async def list_generation_logs(
    db: AsyncSession, user_id: int, project_id: int, conversation_id: int
) -> list[GenerationLogOut]:
    # 这里只放行本人的历史日志；owned_conversation 仍保留严格作者语义，
    # 不会连带开放发送、提案或任务受理。
    with db.no_autoflush:
        await personal_projection.require_personal_project(db, user_id, project_id)
        await _conversation_key(db, user_id, project_id, conversation_id)
        rows = list(await db.scalars(
            select(GenerationLog)
            .where(
                GenerationLog.project_id == project_id,
                GenerationLog.actor_user_id == user_id,
                GenerationLog.kind == "agent.turn",
                GenerationLog.params["conversation_id"].as_integer() == conversation_id,
            )
            .order_by(GenerationLog.id.desc())
        ))
        result = await generation_log_service.public_logs(db, user_id, project_id, rows)

    def public_calls(calls: list[dict]) -> list[dict]:
        return [{key: call[key] for key in ("id", "name", "arguments") if key in call} for call in calls]

    for log in result:
        # 完整原生签名只用于 provider 重放；生成记录与消息详情一样隐藏 thinking 等元数据。
        log.params = {**log.params}
        if isinstance(log.params.get("tool_calls"), list):
            log.params["tool_calls"] = public_calls(log.params["tool_calls"])
        log.messages = [
            {**message, "tool_calls": public_calls(message["tool_calls"])} if message.get("tool_calls") else message
            for message in log.messages
        ]
    return result


async def delete_conversation(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, conversation_id: int,
) -> None:
    conversation_key = await _precheck_conversation(db, user_id, project_id, conversation_id)
    async with conversation_operations.hold(
        db, engine.request_worker, conversation_id, user_id, project_id, conversation_key=conversation_key,
    ):
        row = await owned_conversation(db, user_id, project_id, conversation_id, lock=True)
        await conversation_operations.guard(db, conversation_id)
        if await active_turn(db, row.id):
            raise ConflictError("会话有进行中或待确认的轮次，无法删除")
        # 会话删除会级联删除提案；未决或损坏的外部证据必须随会话保留。
        from app.services.agent.runner import require_no_unknown_external

        await require_no_unknown_external(db, project_id=project_id, conversation_id=row.id)
        invalid = await db.scalar(select(exists().where(
            AgentChange.conversation_id == row.id, AgentChange.kind == "external",
            AgentChange.status.not_in(("proposed", "rejected", "executed", "failed")),
        )))
        if invalid:
            raise ConflictError(EXTERNAL_INTERRUPTED, extra={"code": "external_outcome_unknown"})
        await db.delete(row)
        await db.commit()


async def send_message(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, conversation_id: int, req: SendMessage
) -> MessageAccepted:
    from app.services.task_engine.admission import prepare_task_admission

    actor = db.info.get("authenticated_actor")
    if not isinstance(actor, authz.AuthenticatedActor) or actor.id != user_id:
        raise UnauthorizedError("未登录或登录已过期")
    # 会话初检只读；先结束原事务，模型预检短会话关闭后才解析 DNS。
    # 锁外初检同样核对会话级写能力：viewer / 旧轮次会话在模型预检前即 409（锁内仍重验）。
    conversation_key = await _precheck_conversation(db, user_id, project_id, conversation_id)
    model_preflight = await project_ai_config.preflight_project_model(
        engine.ctx.sessionmaker, actor, project_id, "text", cipher=engine.ctx.cipher, config_id=req.ai_config_id,
        allow_private=engine.ctx.settings.allow_private_ai_endpoints, for_task_admission=True,
    )
    params = {**req.model_dump(mode="json"), "project_id": project_id, "conversation_id": conversation_id}
    admission = await prepare_task_admission(engine.ctx, actor, "agent.turn", params, model_preflight=model_preflight)
    async with conversation_operations.hold(
        db, engine.request_worker, conversation_id, user_id, project_id, conversation_key=conversation_key,
    ):
        # AgentHandler 已声明协作就绪：中央受理按 ai.use + 发送写门禁重入锁定；门禁须在作品父行之前取得。
        row = await owned_conversation(db, user_id, project_id, conversation_id, lock=True, visibility_write=True)
        await conversation_operations.guard(db, conversation_id)
        epoch = (await read_visibility_stamp(db, project_id)).epoch
        if await history.is_sealed(db, row, epoch):
            raise history.HistorySealedError
        # handler 内再次持锁校验，避免其他创建入口绕过约束。
        if await active_turn(db, row.id):
            raise ConflictError("当前会话已有进行中或待确认的轮次")
        task = await task_service.create_task(
            db,
            engine,
            user_id,
            "agent.turn",
            params,
            admission=admission,
        )
        # create_task 提交后又读取了任务（PostgreSQL REPEATABLE READ 已取快照）；引擎随即领取会更新
        # 授权门禁行，旧快照上的门禁 UPDATE 会得到序列化失败。先结束只读事务，门禁成为新事务首条语句。
        await db.rollback()
        entry = await agent_access.lock_agent_entry(db, user_id, project_id)
        stored = await db.get(Task, task.id)
        if (stored is None or not task.task_key or stored.statistics_key != task.task_key
                or stored.user_id != user_id or stored.project_id != project_id):
            raise ConflictError("原任务身份无法确认，请刷新后重试", extra={"code": "agent_task_identity_required"})
        # 本轮受理绑定的 grant（作者为 None）锁后仍须有效；受理后已撤权则不返回私人消息。
        agent_access.check_agent_write(entry, user_id, agent_access.task_grant(stored), deny=agent_access.deny_changed)
        message = await db.get(AgentMessage, stored.input["message_id"])
        if (message is None or message.conversation_id != conversation_id or message.role != "user"
                or message.turn_task_id != task.id or message.turn_task_key != stored.statistics_key):
            raise NotFoundError("会话不存在")
        await db.refresh(message)
        payload = {
            "message": MessageOut.model_validate(message).model_dump(mode="json"),
            "task": task.model_dump(mode="json"),
        }
        projected = await personal_projection.project_payload(db, user_id, project_id, payload)
        return MessageAccepted.model_validate(projected)


async def _change_entry(
    db: AsyncSession, user_id: int, project_id: int, change_id: int, *, lock: bool,
) -> tuple[agent_access.AgentEntry, AgentChange]:
    entry = await agent_access.lock_agent_entry(db, user_id, project_id, lock=lock, visibility_write=lock)
    change = await db.scalar(
        select(AgentChange)
        .join(
            AgentConversation,
            AgentChange.conversation_id == AgentConversation.id,
        )
        .where(AgentChange.id == change_id, AgentChange.project_id == project_id, AgentConversation.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if change is None:
        raise NotFoundError("修改提案不存在")
    return entry, change


async def owned_change(
    db: AsyncSession, user_id: int, project_id: int, change_id: int, *, lock: bool = False
) -> AgentChange:
    """本人会话中的提案；只核对私人作用域（不属于本人 404），写能力由调用方按原轮次 grant 判定。"""
    return (await _change_entry(db, user_id, project_id, change_id, lock=lock))[1]


async def _change_grant(db: AsyncSession, change: AgentChange) -> "GrantIdentity | None":
    """提案所属轮次绑定的 grant；无轮次（旧记录）视为作者轮次，协作者因此失败关闭。"""
    with db.no_autoflush:
        task = await db.get(Task, change.turn_task_id) if change.turn_task_id is not None else None
    return agent_access.task_grant(task)


async def _require_change_write(
    db: AsyncSession, entry: agent_access.AgentEntry, user_id: int, change: AgentChange,
) -> "GrantIdentity | None":
    """确认 / 拒绝 / 撤销提案：Agent 写能力 + 原轮次 grant（裁决 8、14、15），失败 409 permission_denied。

    在提案投影核对之前判定，避免降级后先以 `agent_change_changed` 拒绝而错误码不一致。
    """
    grant = await _change_grant(db, change)
    agent_access.check_agent_write(entry, user_id, grant, deny=agent_access.deny_changed)
    return grant


async def _turn_entry(
    db: AsyncSession, user_id: int, project_id: int, task_id: int, *, lock: bool,
) -> tuple[agent_access.AgentEntry, Task]:
    entry = await agent_access.lock_agent_entry(db, user_id, project_id, lock=lock, visibility_write=lock)
    # 按本人真实归属读取（不经活动轮次的成员可见性谓词）：本人轮次授权失效时由调用方给 409，而非 404。
    with db.no_autoflush:
        task = await db.scalar(select(Task).where(
            Task.id == task_id, Task.user_id == user_id, Task.project_id == project_id,
            Task.kind == "agent.turn", Task.statistics_key.is_not(None),
        ).execution_options(populate_existing=True))
    if task is None:
        raise NotFoundError("智能体轮次不存在")
    return entry, task


async def _turn(db: AsyncSession, user_id: int, project_id: int, task_id: int, *, lock: bool = False) -> Task:
    entry, task = await _turn_entry(db, user_id, project_id, task_id, lock=lock)
    if lock:
        agent_access.check_agent_write(entry, user_id, agent_access.task_grant(task), deny=agent_access.deny_changed)
    return task


def _turn_conversation_id(task: Task) -> int:
    request = task.input.get("request") if isinstance(task.input, dict) else None
    conversation_id = request.get("conversation_id") if isinstance(request, dict) else None
    if type(conversation_id) is not int:
        raise NotFoundError("会话不存在")
    return conversation_id


async def execute_change(
    db: AsyncSession, task_context: TaskContext, change: AgentChange, *, user_id: int, compare_preview: bool = True,
    scope: "AiScope | None" = None, audit_coordinator: transactions.AgentAuditCoordinator,
    task_admission: "tasks_tool.PreparedTaskAdmission | None" = None,
) -> AgentChange:
    """调用方负责会话互斥与提交；savepoint 保证返回 error 的工具也不会留下部分修改。

    受控受理任务提案只能携带确认服务在锁外准备的 task_admission，由中央受理在自身保存点完成。
    """
    if change.status != "proposed":
        return change
    # 调用方（确认请求 / 后台执行体）已在本事务持锁；先读轮次绑定的授权，再锁后复核 Agent 能力。
    # 授权失效抛 AgentAuthorizationChangedError 终止本轮，不能记为 failed 后继续。
    with db.no_autoflush:
        task = await db.get(Task, change.turn_task_id) if change.turn_task_id else None
    grant = agent_access.task_grant(task)
    project = await agent_access.lock_agent_write(db, user_id, change.project_id, grant=grant)
    conversation = await owned_conversation(db, user_id, project.id, change.conversation_id)
    stamp = await history.require_open_history(
        db, conversation, expected_epoch=task.input.get("history_epoch") if task else None,
    )
    if scope is not None:
        await require_visibility_stamp(db, scope.stamp)
    if change.kind == "external":
        if grant is not None:
            # 裁决 2：协作轮次不开放 MCP；即使存在残留外部提案也不发网、不建未知结果屏障。
            change.status, change.error, change.result = "failed", mcp_tools.CHANGED, {"error": mcp_tools.CHANGED}
            await db.flush()
            return change
        return await _execute_external(
            db, task_context, change, user_id=user_id, compare_preview=compare_preview,
            audit_coordinator=audit_coordinator,
        )
    if scope is None:
        source_id = task.input.get("message_id") if task else None
        source = await db.scalar(select(AgentMessage.content).where(
            AgentMessage.id == source_id, AgentMessage.conversation_id == conversation.id, AgentMessage.role == "user",
        )) if source_id is not None else None
        if source is None:
            raise ConflictError("本轮原始消息不可用，请重新发起")
        scope = await history.build_tool_scope(db, stamp, source)
    tool = get_tool(change.tool_name)
    if tool is None:
        raise BadRequestError("工具不存在")
    if tool.name == "start_task":
        await require_task_admission_open(db)
    ctx = ToolCtx(
        db, project, user_id, task_context, scope, visibility_locked=True, audit_coordinator=audit_coordinator,
        grant=grant,
    )
    fresh = await tool.preview(ctx, change.arguments)
    if "error" in fresh:
        change.status, change.error, change.result = "failed", fresh["error"], fresh
        await db.flush()
        return change
    if compare_preview and fresh != change.preview:
        change.preview = fresh
        await audit_coordinator.commit(db, task_context.sessionmaker)
        await db.refresh(change)
        raise await _preview_changed(db, user_id, project.id, change)
    claimed = await db.execute(
        update(AgentChange)
        .where(
            AgentChange.id == change.id,
            AgentChange.status == "proposed",
        )
        .values(status="executing")
    )
    if claimed.rowcount != 1:
        raise ConflictError("修改正在处理中，请刷新后重试")
    change.status = "executing"
    failure = None
    if tasks_tool.requires_admission(change):
        return await _execute_admitted_task(db, ctx, change, project.id, task_admission)
    try:
        async with db.begin_nested():
            before = await effects.snapshot(db, project.id)
            result = await tool.execute(ctx, change.arguments)
            if "error" in result:
                raise BadRequestError(str(result["error"]))
            await db.flush()
            after = await effects.snapshot(db, project.id)
            captured = effects.diff(before, after)
            # 正文版本效果由写工具提供；不向模型 / 面板泄露内部快照辅助字段。
            if change.tool_name == "replace_chapter_text":
                captured.append(
                    effects.chapter_text_effect(
                        **{
                            key: result[key]
                            for key in (
                                "chapter_id",
                                "before_version_id",
                                "after_version_id",
                                "before_hash",
                                "after_hash",
                            )
                        }
                    )
                )
            captured.extend(result.pop("_effects", []))
    except (VisibilityPolicyError, TaskAdmissionClosed, SettingsUnavailable):
        # 授权或受理屏障的失败由根事务回滚，保留待确认的提案。
        raise
    except AppError as exc:
        failure = exc.detail
    except DBAPIError as exc:
        await transactions.rollback_conflict(db, exc)
    except Exception:  # noqa: BLE001
        logger.exception("智能体提案 %s 执行失败", change.id)
        failure = "修改执行失败：服务器内部错误"
    if failure:
        change.status, change.error, change.result = "failed", failure, {"error": failure}
    else:
        change.status, change.error = "executed", ""
        change.result, change.effects = result, captured
        change.executed_at = datetime.now(UTC)
    await db.flush()
    return change


async def _execute_admitted_task(
    db: AsyncSession, ctx: ToolCtx, change: AgentChange, project_id: int,
    prepared: "tasks_tool.PreparedTaskAdmission | None",
) -> AgentChange:
    """中央受理须在根事务层级消费锁外凭据；它在自身保存点内校验和插入，失败只回滚该保存点。"""
    root = db.get_transaction()
    root_sync = root.sync_transaction if root is not None else None
    failure = None
    try:
        before = await effects.snapshot(db, project_id)
        result = await tasks_tool.execute_admitted(ctx, change, prepared)
        await db.flush()
        after = await effects.snapshot(db, project_id)
        captured = effects.diff(before, after)
    except (VisibilityPolicyError, TaskAdmissionClosed, SettingsUnavailable):
        raise
    except AppError as exc:
        current = db.get_transaction()
        if current is None or not current.is_active or current.sync_transaction is not root_sync:
            # 模型重选失败已回滚确认事务：提案保持待确认，不在锁外补写失败。
            raise
        failure = exc.detail
    except DBAPIError as exc:
        await transactions.rollback_conflict(db, exc)
    except Exception:  # noqa: BLE001
        logger.exception("智能体提案 %s 执行失败", change.id)
        failure = "修改执行失败：服务器内部错误"
    if failure:
        change.status, change.error, change.result = "failed", failure, {"error": failure}
    else:
        change.status, change.error = "executed", ""
        change.result, change.effects = result, captured
        change.executed_at = datetime.now(UTC)
    await db.flush()
    return change


async def _preview_changed(db: AsyncSession, user_id: int, project_id: int, change: AgentChange) -> ConflictError:
    public = (await public_changes(db, user_id, project_id, [change]))[0]
    return ConflictError(
        "数据已经变化，请检查最新预览后重新确认",
        extra={"code": "preview_changed", "change": public.model_dump(mode="json")},
    )


async def _execute_external(db, task_context, change, *, user_id, compare_preview, audit_coordinator):
    task = await db.get(Task, change.turn_task_id) if change.turn_task_id else None
    engine = task_context.task_engine
    scope = engine.cleanup_scope(task) if engine is not None and task is not None else nullcontext()
    with scope:
        return await _execute_external_impl(
            db, task_context, change, user_id=user_id, compare_preview=compare_preview,
            audit_coordinator=audit_coordinator,
        )


@dataclass(frozen=True, slots=True)
class _ExternalClaim:
    project_id: int
    change_id: int
    change_key: str
    conversation_id: int
    task_id: int | None
    task_key: str | None
    task_queued_at: datetime
    tool_call_id: str
    user_id: int
    started_at: datetime
    claim_id: str
    task_snapshot: str
    change_snapshot: str
    worker_key: str
    snapshot_mac: str = ""

    def activity(self) -> ExternalActivity:
        return ExternalActivity(self.claim_id, self.worker_key, self.project_id, self.task_id, self.task_key,
                                self.task_queued_at, None, self.change_key)


async def _original_external_rows(db: AsyncSession, claim: _ExternalClaim) -> bool:
    """持行锁后核对真实原快照；同 UUID 的内容替换也不能承接旧调用。"""
    task = await db.scalar(select(Task).where(
        Task.id == claim.task_id, Task.statistics_key == claim.task_key,
        Task.project_id == claim.project_id, Task.user_id == claim.user_id,
        Task.queued_at == claim.task_queued_at, Task.execution_key.is_(None),
    ).with_for_update().execution_options(populate_existing=True))
    change = await db.scalar(select(AgentChange).where(
        AgentChange.id == claim.change_id, AgentChange.change_key == claim.change_key,
        AgentChange.project_id == claim.project_id, AgentChange.conversation_id == claim.conversation_id,
        AgentChange.turn_task_id == claim.task_id, AgentChange.turn_task_key == claim.task_key,
        AgentChange.tool_call_id == claim.tool_call_id, AgentChange.kind == "external",
        AgentChange.status == "executing", AgentChange.executed_at == claim.started_at,
        AgentChange.external_worker_key == claim.worker_key, claim.activity().active(),
    ).with_for_update().execution_options(populate_existing=True))
    try:
        return bool(task is not None and change is not None
                    and mcp_tools.valid_external_claim_result(change.result, automatic=False)
                    and change.result.get(transactions.EXTERNAL_CLAIM_KEY) == claim.claim_id
                    and change.result.get(_EXTERNAL_TASK_KEY) == claim.task_key
                    and change.result.get(_EXTERNAL_SNAPSHOT_MAC) == claim.snapshot_mac
                    and mcp_tools.task_snapshot(task) == claim.task_snapshot
                    and mcp_tools.change_snapshot(change) == claim.change_snapshot)
    except (TypeError, ValueError, RecursionError):
        return False


async def _interrupt_external_claim(
    db: AsyncSession, task_context: TaskContext, coordinator: transactions.AgentAuditCoordinator,
    claim: _ExternalClaim, *, outcome_unknown: bool = True, known_message: str = EXTERNAL_RETURNED_NOT_APPLIED,
) -> bool:
    """仅收尾仍属本次执行的外部提案；返回作品记录是否仍存在。"""
    await db.rollback()
    try:
        parent = await project_lifecycle.lock_record(db, claim.project_id)
    except NotFoundError:
        return False
    if parent.deleted_at is not None or claim.task_key is None or claim.task_id is None:
        return True
    if not await _original_external_rows(db, claim):
        # 损坏或被替换的证据不能被本次旧调用覆盖；留作执行中屏障。
        await db.rollback()
        return True
    original_task = exists(select(Task.id).where(
        Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.project_id == claim.project_id,
        Task.user_id == claim.user_id, Task.queued_at == claim.task_queued_at, Task.execution_key.is_(None),
    ))
    message = EXTERNAL_INTERRUPTED if outcome_unknown else known_message
    await task_context.task_engine.request_worker.require(db, claim.worker_key)
    result = await db.execute(update(AgentChange).where(
        AgentChange.id == claim.change_id, AgentChange.change_key == claim.change_key,
        AgentChange.project_id == claim.project_id,
        AgentChange.conversation_id == claim.conversation_id, AgentChange.turn_task_id == claim.task_id,
        AgentChange.turn_task_key == claim.task_key,
        AgentChange.tool_call_id == claim.tool_call_id, AgentChange.status == "executing",
        AgentChange.executed_at == claim.started_at, AgentChange.external_worker_key == claim.worker_key,
        claim.activity().active(), AgentChange.result[transactions.EXTERNAL_CLAIM_KEY].as_string() == claim.claim_id,
        AgentChange.result[_EXTERNAL_TASK_KEY].as_string() == claim.task_key,
        AgentChange.result[_EXTERNAL_SNAPSHOT_MAC].as_string() == claim.snapshot_mac, original_task,
    ).values(status="failed", error=message,
             external_worker_key=None,
             result=(_unknown_external_result(claim.claim_id, claim.task_key, snapshot_mac=claim.snapshot_mac)
                     if outcome_unknown
                     else {"error": message})))
    if result.rowcount == 1:
        await claim.activity().finish(db)
    await coordinator.commit(db, task_context.sessionmaker)
    return True


async def _execute_external_impl(
    db: AsyncSession, task_context: TaskContext, change: AgentChange, *, user_id: int, compare_preview: bool,
    audit_coordinator: transactions.AgentAuditCoordinator,
) -> AgentChange:
    """外部（MCP）调用：重新读取插件配置生成预览比较；一致才在本请求内调用（单独连接，30 秒超时）。

    先提交 executing 再发起网络调用，避免在调用期间持有数据库写锁；结果由调用方提交。
    """
    task = await db.get(Task, change.turn_task_id) if change.turn_task_id else None
    tool_map = ((task.checkpoint or {}) if task is not None else {}).get("mcp_tool_map") or {}
    entry = tool_map.get(change.tool_name)
    if entry is None:
        change.status, change.error, change.result = "failed", mcp_tools.CHANGED, {"error": mcp_tools.CHANGED}
        await db.flush()
        return change
    settings = task_context.settings
    resolved = await mcp_tools.resolve(db, settings, user_id, change.conversation_id, tool_map, change.tool_name)
    fresh = mcp_tools.preview(entry, resolved, change.arguments)
    if compare_preview and fresh != change.preview:
        change.preview = fresh
        await audit_coordinator.commit(db, task_context.sessionmaker)
        await db.refresh(change)
        raise await _preview_changed(db, user_id, change.project_id, change)
    failure = None
    if resolved is None:
        failure = mcp_tools.CHANGED
    elif (invalid := mcp_tools.validate(resolved, change.arguments)) is not None:
        failure = invalid["error"]
    if failure:
        change.status, change.error, change.result = "failed", failure, {"error": failure}
        await db.flush()
        return change
    try:
        if (task is None or task.project_id != change.project_id or task.user_id != user_id
                or not isinstance(task.statistics_key, str)
                or change.turn_task_key != task.statistics_key
                or str(UUID(task.statistics_key)) != task.statistics_key
                or not isinstance(task.queued_at, datetime) or task.execution_key is not None
                or not isinstance(task.input, dict)
                or type(task.input.get(_AGENT_ACTOR_TOKEN_VERSION)) is not int
                or type(task.input.get(_TASK_ACTOR_TOKEN_VERSION)) is not int):
            raise ValueError
    except ValueError:
        # 旧任务缺少不可复用身份时，不能安全地在网络请求后区分另一轮执行。
        raise ConflictError("当前任务身份无法确认，请重新发起", extra={"code": "task_identity_required"}) from None
    actor = db.info.get("authenticated_actor")
    if (not isinstance(actor, authz.AuthenticatedActor) or actor.id != user_id
            or actor.token_version != task.input[_AGENT_ACTOR_TOKEN_VERSION]
            or actor.token_version != task.input[_TASK_ACTOR_TOKEN_VERSION]):
        raise ConflictError("当前任务身份无法确认，请重新发起", extra={"code": "task_identity_required"})
    stamp = await read_visibility_stamp(db, change.project_id)
    operation_key = await conversation_operations.guard(db, change.conversation_id)
    if operation_key is None:
        raise ConflictError("会话操作身份已变化，请核对后重试")
    # executing 期间 executed_at 记录开始调用的时间（完成时改为完成时间），供 recover_external 判断是否中断；
    # 不用 updated_at：重试时把提案移到新任务的 UPDATE 会刷新它
    claim = _ExternalClaim(
        project_id=change.project_id, change_id=change.id, change_key=change.change_key,
        conversation_id=change.conversation_id,
        task_id=change.turn_task_id, task_key=task.statistics_key if task is not None else None,
        task_queued_at=task.queued_at,
        tool_call_id=change.tool_call_id, user_id=user_id, started_at=datetime.now(UTC), claim_id=str(uuid4()),
        task_snapshot=mcp_tools.task_snapshot(task), change_snapshot=mcp_tools.change_snapshot(change),
        worker_key=await task_context.task_engine.request_worker.require(db),
    )
    send_identity = mcp_tools.McpSendIdentity(
        sessionmaker=task_context.sessionmaker, actor=actor, project_id=claim.project_id,
        conversation_id=claim.conversation_id, task_id=task.id, task_key=task.statistics_key,
        queued_at=task.queued_at, execution_key=None, change_id=claim.change_id, change_key=claim.change_key,
        claim_id=claim.claim_id, tool_call_id=claim.tool_call_id, started_at=claim.started_at, stamp=stamp,
        task_snapshot=claim.task_snapshot, change_snapshot=claim.change_snapshot, worker_key=claim.worker_key,
        conversation_operation_key=operation_key,
    )
    claim = replace(claim, snapshot_mac=mcp_tools.identity_snapshot_mac(db.info.get("agent_view_hmac_key"),
                                                                      send_identity))
    claimed = await db.execute(
        update(AgentChange)
        .where(AgentChange.id == change.id, AgentChange.change_key == change.change_key,
               AgentChange.turn_task_id == claim.task_id, AgentChange.turn_task_key == claim.task_key,
               AgentChange.status == "proposed", AgentChange.external_worker_key.is_(None),
               ~exists(select(TaskWorkRecord.work_key).where(
                   TaskWorkRecord.kind == "external", TaskWorkRecord.operation_id == claim.change_key,
               )))
        .values(status="executing", executed_at=claim.started_at, external_worker_key=claim.worker_key,
                result={transactions.EXTERNAL_CLAIM_KEY: claim.claim_id, _EXTERNAL_TASK_KEY: claim.task_key,
                        _EXTERNAL_SNAPSHOT_MAC: claim.snapshot_mac})
    )
    if claimed.rowcount != 1:
        raise ConflictError("修改正在处理中，请刷新后重试")
    activity = claim.activity()
    activity.register(db)
    db.info["external_call_in_progress"] = activity
    try:
        await audit_coordinator.commit(db, task_context.sessionmaker)
    except BaseException:
        # 尚未创建网络会话。提交回执丢失时只对可重读的原 claim 保存“未发送”。
        await drain(_interrupt_external_claim(
            db, task_context, audit_coordinator, claim, outcome_unknown=False, known_message=EXTERNAL_NOT_SENT,
        ))
        raise
    sessions = mcp_tools.McpSessions(
        task_context.mcp_connector,
        task_context.cipher,
        settings.allow_private_ai_endpoints,
        settings=settings,
    )
    policy_failure = None
    outcome_unknown = False
    send_state = mcp_tools.McpSendState()
    try:
        try:
            data = await mcp_tools.call_data(
                sessions, resolved, change.arguments, preserve_outcome=True, send_state=send_state,
                send_identity=send_identity,
            )
        except mcp_tools.McpOutcomeUnknown as exc:
            outcome_unknown = True
            if exc.policy_changed:
                policy_failure = exc
            data = {"error": EXTERNAL_INTERRUPTED}
        except VisibilityPolicyError as exc:
            policy_failure = exc
        except Exception:  # noqa: BLE001
            # 进入网络边界之后不能证明远端未执行，也不能重试。
            logger.error("外部调用提案 %s 执行失败", claim.change_id)
            outcome_unknown = True
            data = {"error": EXTERNAL_INTERRUPTED}
        finally:
            await sessions.aclose()
    except asyncio.CancelledError:
        await drain(_interrupt_external_claim(
            db, task_context, audit_coordinator, claim, outcome_unknown=send_state.started,
            known_message=EXTERNAL_NOT_SENT,
        ))
        raise
    if policy_failure is not None:
        await _interrupt_external_claim(
            db, task_context, audit_coordinator, claim, outcome_unknown=outcome_unknown,
            known_message=EXTERNAL_NOT_SENT,
        )
        if outcome_unknown:
            raise ConflictError(EXTERNAL_INTERRUPTED, extra={"code": "external_outcome_unknown"}) from None
        raise policy_failure
    try:
        await _lock_owned_project(db, user_id, claim.project_id)
    except (ConflictError, NotFoundError, UnauthorizedError) as exc:
        auth_failed = isinstance(exc, UnauthorizedError)
        # 远端已可能执行，不可撤回；在删除协调等待的请求收尾内保存“结果未知”，不续跑。
        exists_project = await _interrupt_external_claim(
            db, task_context, audit_coordinator, claim, outcome_unknown=outcome_unknown,
            known_message=EXTERNAL_RETURNED_NOT_APPLIED if send_state.started else EXTERNAL_NOT_SENT,
        )
        if auth_failed:
            raise UnauthorizedError("未登录或登录已过期") from None
        if not exists_project:
            raise ConflictError("作品已不可访问，外部调用结果不再写入") from None
        raise ConflictError("作品正在删除，外部调用结果不再应用") from None
    # 从网络返回后以任务 UUID 和本次执行起点 CAS，不能把旧结果附着到后来的轮次。
    if not await _original_external_rows(db, claim):
        raise ConflictError(EXTERNAL_INTERRUPTED, extra={"code": "external_outcome_unknown"})
    original_task = exists(select(Task.id).where(
        Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.project_id == claim.project_id,
        Task.user_id == claim.user_id, Task.queued_at == claim.task_queued_at, Task.execution_key.is_(None),
    ))
    await task_context.task_engine.request_worker.require(db, claim.worker_key)
    if await conversation_operations.guard(db, claim.conversation_id) != operation_key:
        raise ConflictError(EXTERNAL_INTERRUPTED, extra={"code": "external_outcome_unknown"})
    if outcome_unknown:
        values = {"status": "failed", "error": EXTERNAL_INTERRUPTED,
                  "result": _unknown_external_result(claim.claim_id, claim.task_key, snapshot_mac=claim.snapshot_mac)}
    elif "error" in data:
        values = {"status": "failed", "error": str(data["error"]), "result": data}
    else:
        values = {"status": "executed", "error": "", "result": data, "effects": [],
                  "executed_at": datetime.now(UTC)}
    result = await db.execute(update(AgentChange).where(
        AgentChange.id == claim.change_id, AgentChange.change_key == claim.change_key,
        AgentChange.project_id == claim.project_id,
        AgentChange.conversation_id == claim.conversation_id, AgentChange.turn_task_id == claim.task_id,
        AgentChange.turn_task_key == claim.task_key,
        AgentChange.tool_call_id == claim.tool_call_id, AgentChange.status == "executing",
        AgentChange.executed_at == claim.started_at, AgentChange.external_worker_key == claim.worker_key,
        claim.activity().active(), AgentChange.result[transactions.EXTERNAL_CLAIM_KEY].as_string() == claim.claim_id,
        AgentChange.result[_EXTERNAL_TASK_KEY].as_string() == claim.task_key,
        AgentChange.result[_EXTERNAL_SNAPSHOT_MAC].as_string() == claim.snapshot_mac, original_task,
    ).values(**values, external_worker_key=None).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise ConflictError("外部操作的执行状态已变化，结果未应用", extra={"code": "external_execution_changed"})
    await activity.finish(db)
    change = await db.get(AgentChange, claim.change_id, populate_existing=True)
    return change


async def maybe_resume(
    db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int | None, *, task_key: str | None = None,
) -> str | None:
    if task_id is None:
        return None
    linked = await db.get(Task, task_id)
    if linked is None or not task_key or linked.statistics_key != task_key or linked.user_id != user_id:
        raise NotFoundError("智能体轮次不存在")
    project_id = linked.project_id
    cid = _turn_conversation_id(linked)
    # 回滚会使已加载对象过期：先取轮次绑定的 grant（作者为 None），提交后复核按原轮次。
    grant = agent_access.task_grant(linked)
    if project_id is not None and not authz.holds_gate(db) and not (db.new or db.dirty or db.deleted):
        # 调用方已提交、只剩提交后读取的旧快照（PostgreSQL REPEATABLE READ）；引擎刚被通知领取任务时会更新
        # 授权门禁行，旧快照上的门禁 UPDATE 会序列化失败。结束只读事务，使门禁成为新事务首条语句。
        await db.rollback()
    if project_id is not None:
        await _lock_owned_project(db, user_id, project_id, grant=grant)
    await conversation_operations.guard(db, cid)
    if await recover_external(db, task_id=task_id, task_key=task_key, engine=engine):
        await db.commit()
        if project_id is not None:
            # 提交释放了门禁；resume_task 前重新取门禁，避免在之后的读取快照上再取。
            await _lock_owned_project(db, user_id, project_id, grant=grant)
    remaining = await db.scalar(
        select(func.count(AgentChange.id)).where(
            AgentChange.turn_task_id == task_id, AgentChange.status.in_(("proposed", "executing"))
        )
    )
    task = await db.get(Task, task_id, populate_existing=True)
    if task is None or task.statistics_key != task_key or task.user_id != user_id:
        raise NotFoundError("智能体轮次不存在")
    if not remaining and task is not None and task.status == "paused":
        if _turn_conversation_id(task) != cid:
            raise NotFoundError("会话不存在")
        conversation = await owned_conversation(db, user_id, task.project_id, cid)
        try:
            await history.require_open_history(db, conversation, expected_epoch=task.input.get("history_epoch"))
        except history.HistorySealedError:
            # 人工仍可拒绝最后一项旧提案，但不能由此续跑旧上下文。
            await task_service.cancel_task(db, engine, user_id, task.id, expected_task_key=task_key)
            return None
        try:
            await task_service.resume_task(db, engine, user_id, task.id, expected_task_key=task_key)
        except (TaskAdmissionClosed, SettingsUnavailable) as exc:
            # 先前的提案决定已提交。保留它和待审核轮次，允许之后从原任务继续。
            if isinstance(exc, SettingsUnavailable):
                # 设置读取可能使连接失效。响应投影前结束无业务写入的续跑事务。
                await db.rollback()
            return exc.detail
    return None


async def decide_change(
    db: AsyncSession,
    engine: "TaskEngine",
    user_id: int,
    project_id: int,
    change_id: int,
    expected: ChangeAction,
    *,
    confirm: bool,
    resume: bool = True,
) -> ChangeOut:
    entry, initial = await _change_entry(db, user_id, project_id, change_id, lock=False)
    if initial.change_key != str(expected.change_key):
        raise ConflictError("提案身份已变化，请刷新后重试", extra={"code": "agent_change_changed"})
    # 锁外初检（锁内重验）：确认与拒绝都是 Agent 写操作（裁决 15），viewer / 旧轮次在模型预检前即 409。
    await _require_change_write(db, entry, user_id, initial)
    cid, turn_id, turn_key = initial.conversation_id, initial.turn_task_id, initial.turn_task_key
    conversation_key = await _conversation_key(db, user_id, project_id, cid)
    initial_snapshot = tasks_tool.ChangeSnapshot.of(initial)
    await db.rollback()  # 在等待会话占用前释放只读事务和旧快照。
    task_admission = None
    if confirm:
        # 受控受理任务：取得任何写锁前核对来源证明并完成模型预检/DNS；锁内再重验原身份。
        try:
            task_admission = await tasks_tool.prepare_confirmed_admission(
                db, engine.ctx, initial_snapshot, user_id=user_id, project_id=project_id,
            )
        finally:
            await db.rollback()
    async with conversation_operations.hold(
        db, engine.request_worker, cid, user_id, project_id, conversation_key=conversation_key,
    ):
        try:
            async with transactions.audit_scope(db, engine.ctx.sessionmaker) as coordinator:
                return await _decide_change(
                    db, engine, user_id, project_id, change_id, expected, cid=cid, turn_id=turn_id, turn_key=turn_key,
                    confirm=confirm, resume=resume, coordinator=coordinator, task_admission=task_admission,
                )
        except BaseException:
            # 审计与业务事务已核实后再确认外部调用退出，最后释放持久会话占用。
            activity = db.info.get("external_call_in_progress")
            if activity is not None:
                await drain(activity.confirm_exit(engine.request_worker.lease))
            raise
        finally:
            db.info.pop("external_call_in_progress", None)


async def _decide_change(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, change_id: int, expected: ChangeAction, *,
    cid: int, turn_id: int | None, turn_key: str | None, confirm: bool, resume: bool,
    coordinator: transactions.AgentAuditCoordinator, task_admission: "tasks_tool.PreparedTaskAdmission | None",
) -> ChangeOut:
    entry, change = await _change_entry(db, user_id, project_id, change_id, lock=True)
    if change.conversation_id != cid or change.turn_task_id != turn_id or change.turn_task_key != turn_key:
        raise ConflictError("原任务身份已变化，请刷新后重试", extra={"code": "agent_task_identity_required"})
    # 拒绝不经 execute_change：锁内自行判定 ai.use + 原轮次 grant（裁决 15）；确认在执行前再复核一次。
    await _require_change_write(db, entry, user_id, change)
    await conversation_operations.guard(db, cid)
    original_public = await _require_change_action(
        db, user_id, project_id, change, expected, require_visible_arguments=confirm,
    )
    original_claim_id = change.result.get(transactions.EXTERNAL_CLAIM_KEY) \
        if isinstance(change.result, dict) else None
    # 中断的外部调用（进程崩溃 / 请求被取消时停在 executing）先判为失败，否则本轮永远无法继续
    if turn_id is not None and await recover_external(db, task_id=turn_id, task_key=turn_key, engine=engine):
        await coordinator.commit(db, engine.ctx.sessionmaker)
        entry, change = await _change_entry(db, user_id, project_id, change_id, lock=True)
        await _require_change_write(db, entry, user_id, change)
        await conversation_operations.guard(db, cid)
        recovered_same = (
            original_public.status == "executing" and change.status == "failed"
            and change.change_key == str(expected.change_key) and change.turn_task_id == turn_id
            and change.turn_task_key == turn_key and isinstance(change.result, dict)
            and change.result.get("error") == EXTERNAL_INTERRUPTED
            and change.result.get(_EXTERNAL_OUTCOME_UNKNOWN) is True
            and change.result.get(_EXTERNAL_TASK_KEY) == turn_key
            and change.result.get(transactions.EXTERNAL_CLAIM_KEY) == original_claim_id and bool(original_claim_id)
        )
        if not recovered_same:
            await _require_change_action(db, user_id, project_id, change, expected,
                                         require_visible_arguments=confirm)
    if change.status == "executing":
        raise ConflictError("修改正在处理中，请刷新后重试")
    if change.status == "proposed":
        task = await db.get(Task, change.turn_task_id) if change.turn_task_id else None
        if task is None or task.status != "paused":
            raise ConflictError("请等待本轮暂停确认，或先重试中断的轮次")
        if confirm:
            await execute_change(
                db, engine.ctx, change, user_id=user_id, audit_coordinator=coordinator,
                task_admission=task_admission,
            )
        else:
            change.status, change.result = "rejected", {"message": "用户拒绝了该修改"}
        # 提交会释放行锁；在此之前冻结真正处理过的对象，防止刷新时采纳同 ID 的替代提案。
        await db.flush()
        await db.refresh(change)
        original_result = ChangeOut.model_validate(change).model_dump(mode="json")
        admitted = confirm and change.tool_name == "start_task" and change.status == "executed"
        await coordinator.commit(db, engine.ctx.sessionmaker)
        if admitted:
            engine.notify_queue_changed()
        else:
            engine.notify()
        await db.refresh(change)
    else:
        original_result = ChangeOut.model_validate(change).model_dump(mode="json")
    payload, proofs = await _prepare_committed_changes(db, user_id, project_id, [change])
    if proofs["changes"].get(change_id) != original_result:
        raise NotFoundError("修改提案不存在")
    if resume:
        await maybe_resume(db, engine, user_id, change.turn_task_id, task_key=turn_key)
    projected = await personal_projection.project_agent_payload(db, user_id, project_id, payload, proofs)
    return _public_change_models(projected, proofs, db)[0]


async def decide_turn(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, task_id: int, expected: TurnAction, *,
    confirm: bool,
) -> TurnChangesOut:
    initial = await _turn(db, user_id, project_id, task_id, lock=True)
    if initial.statistics_key != str(expected.task_key):
        raise ConflictError("原任务身份已变化，请刷新后重试", extra={"code": "agent_task_identity_required"})
    cid = _turn_conversation_id(initial)
    conversation_key = await _conversation_key(db, user_id, project_id, cid)
    await db.rollback()
    async with conversation_operations.hold(
        db, engine.request_worker, cid, user_id, project_id, conversation_key=conversation_key,
    ):
        return await _decide_turn(db, engine, user_id, project_id, task_id, expected, cid, confirm=confirm)


async def _decide_turn(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, task_id: int, expected: TurnAction,
    cid: int, *, confirm: bool,
) -> TurnChangesOut:
    task = await _turn(db, user_id, project_id, task_id, lock=True)
    task_key = task.statistics_key
    if not task_key or task_key != str(expected.task_key):
        raise ConflictError("原任务身份已变化，请刷新后重试", extra={"code": "agent_task_identity_required"})
    if _turn_conversation_id(task) != cid:
        raise NotFoundError("会话不存在")
    conversation = await owned_conversation(db, user_id, project_id, cid)
    await conversation_operations.guard(db, cid)
    conversation_proof = {"id": conversation.id, "instance_key": conversation.instance_key}
    if confirm:
        await history.require_open_history(db, conversation, expected_epoch=task.input.get("history_epoch"))
    selected = list(
        await db.scalars(
            select(AgentChange)
            .where(
                AgentChange.turn_task_id == task_id,
                AgentChange.status == "proposed",
            )
            .order_by(AgentChange.seq, AgentChange.id)
        )
    )
    expected_by_id = await _require_turn_actions(
        db, user_id, project_id, selected, expected, require_visible_arguments=confirm,
    )
    ids = [row.id for row in selected]
    changes, error = [], None
    for change_id in ids:
        try:
            change = await decide_change(
                db, engine, user_id, project_id, change_id, expected_by_id[change_id], confirm=confirm, resume=False,
            )
        except VisibilityPolicyError:
            raise
        except AppError as exc:
            if isinstance(exc, (TaskAdmissionClosed, SettingsUnavailable)) and not changes:
                raise
            error = exc.detail
            row = await owned_change(db, user_id, project_id, change_id, lock=True)
            changes.extend(await public_changes(db, user_id, project_id, [row]))
            break
        changes.append(change)
        if change.status == "failed":
            error = change.error
            break
    change_ids = [item.id for item in changes]
    saved = list(await db.scalars(select(AgentChange).where(AgentChange.id.in_(change_ids))
                                  .execution_options(populate_existing=True))) if change_ids else []
    by_id = {row.id: row for row in saved}
    if set(by_id) != set(change_ids):
        raise NotFoundError("修改提案不存在")
    payload, proofs = await _prepare_committed_changes(db, user_id, project_id, [by_id[item] for item in change_ids])
    proofs["conversation"] = conversation_proof
    notice = await maybe_resume(db, engine, user_id, task_id, task_key=task_key)
    task = await task_service.get_owned_task(db, user_id, task_id)
    if task.statistics_key != task_key or task.project_id != project_id:
        raise NotFoundError("智能体轮次不存在")
    task_out = task_service.to_out(task, queue_position=await task_service.queue_position(db, task), engine=engine)
    proofs["turn"] = {
        "id": task.id, "statistics_key": task_key, "queued_at": task.queued_at,
        "snapshot": personal_projection.agent_task_evidence(task),
    }
    projected = await personal_projection.project_agent_payload(
        db, user_id, project_id,
        {"changes": payload, "task": task_out.model_dump(mode="json"), "error": error, "notice": notice},
        proofs,
    )
    projected_changes = _public_change_models(projected["changes"], proofs, db)
    if [item.model_dump(mode="json") for item in projected_changes] != [
        item.model_dump(mode="json") for item in changes
    ]:
        raise NotFoundError("修改提案不存在")
    projected["changes"] = [item.model_dump(mode="json") for item in projected_changes]
    return TurnChangesOut.model_validate(projected)


def restored_memory_ids(effect_rows: list[dict]) -> list[int]:
    """撤销后重新存在、需要（可能缺失）向量的记忆：原先存在的行（被修改或被删除后恢复）。

    撤销删除的新建记忆（before 为空）不需要向量；正向 Agent 工具只会删除或改章号记忆，不产生新文本。
    """
    ids: list[int] = []
    for effect in effect_rows:
        if effect.get("table") != "story_memories" or effect.get("before") is None:
            continue
        ident = (effect.get("pk") or {}).get("id")
        if type(ident) is int and ident > 0 and ident not in ids:
            ids.append(ident)
    return ids


async def _enqueue_collaborator_memories(
    engine: "TaskEngine", actor: object, project_id: int, grant: "GrantIdentity", memory_ids: list[int],
) -> None:
    """已提交后的派生：失败只记日志，不能把派生失败伪装成撤销失败；授权变化时中央受理拒绝、不建任务。"""
    from app.services import kb_index_service

    if not memory_ids:
        return
    try:
        task_id = await kb_index_service.enqueue_collaborator_memory(
            engine, authenticated_actor=actor, project_id=project_id, grant=grant, memory_ids=memory_ids,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("智能体协作撤销后的记忆索引排队失败 error_type=%s", type(exc).__name__)
        return
    if task_id is not None:
        engine.notify_queue_changed()


async def _undo_grant(db: AsyncSession, rows: list[AgentChange]) -> "GrantIdentity | None":
    """撤销需要原轮次的授权；同一批只能来自同一授权，缺失或不一致失败关闭。"""
    task_ids = {row.turn_task_id for row in rows if row.turn_task_id is not None}
    with db.no_autoflush:
        tasks = list(await db.scalars(select(Task).where(Task.id.in_(task_ids)))) if task_ids else []
    grants = {agent_access.task_grant(task) for task in tasks}
    if len(tasks) != len(task_ids) or len(grants) > 1:
        raise transactions.AgentAuthorizationChangedError()
    return next(iter(grants), None)


async def _undo_changes(
    db: AsyncSession, user_id: int, project_id: int, rows: list[AgentChange], engine: "TaskEngine | None" = None,
    *, turn_grant: "GrantIdentity | None" = None,
) -> list[ChangeOut]:
    from app.services.ai_dispatch import visibility_sessionmaker

    # 撤销是 Agent 动作：锁后要求作者或原轮次 accepted editor（ai.use + 原 grant），不接受管理员人工来源。
    # 裁决 14：协作者因授权被拒（撤权、降为 viewer、旧轮次）返回 409 permission_denied，与确认路径一致；
    # 会话 / 提案不属于本人等不存在语义由入口（owned_change / _turn）维持 404。作者的生命周期错误不变。
    # 整轮撤销没有可撤销项时，以入口已核对的轮次 grant 复核（否则空批次会被当成作者轮次）。
    grant = await _undo_grant(db, rows) if rows else turn_grant
    await agent_access.lock_agent_write(
        db, user_id, project_id, grant=grant, visibility_write=False, allow_collaborator=True, on_denied="changed",
    )
    if any(row.kind == "external" for row in rows):
        raise BadRequestError("外部操作无法撤销")
    if any(row.kind == "task" for row in rows):
        raise ConflictError("已启动的任务不能撤销，请在任务面板取消")
    memory_changed = False
    attempted_effects = []
    factory = engine.ctx.sessionmaker if engine is not None else visibility_sessionmaker(db)
    # 裁决 6：撤销接审计协调器、不新增审计动作。撤销链路内的服务若进入管理审计作用域，
    # 失败快照在所有保存点退出后由根事务（或回滚后的独立会话）记账，不随保存点丢失。
    async with transactions.audit_scope(db, factory) as coordinator:
        proxy = TransactionSession(db, audit_coordinator=coordinator, audit_mode="execute")
        try:
            # 整轮先预检，再开始恢复；旧记录不能导致前几项先执行副作用。
            await effects.preflight_effect_versions(
                db, project_id, [effect for row in rows for effect in row.effects],
            )
            for row in rows:
                if row.status != "executed":
                    raise ConflictError("只有已执行的修改可以撤销")
                attempted_effects.extend(row.effects)
                memory_changed = await effects.undo(
                    proxy, project_id, row.effects, discard_inserts=False,
                ) or memory_changed
                row.status, row.undone_at = "undone", datetime.now(UTC)
                db.add(
                    AgentMessage(
                        conversation_id=row.conversation_id,
                        turn_task_id=row.turn_task_id,
                        turn_task_key=row.turn_task_key,
                        role="tool",
                        content=f"用户撤销了：{row.preview.get('title', row.tool_name)}",
                    )
                )
                await db.flush()
            await coordinator.commit(db, factory)
        except BaseException:
            await db.rollback()
            if attempted_effects:
                await effects.refresh_after_rollback(db, project_id, attempted_effects)
            raise
    effects.discard_undone_inserts(db, attempted_effects)
    # 协作者撤销不经作者入口派生全书记忆向量任务（原依赖该入口对非作者抛错后被吞掉）；裁决 17：
    # 只为本次恢复的记忆条目补建作者向量，任务归编辑者并绑定原邀请轮次（与人工记忆写入同一入口）。
    if memory_changed and engine is not None and grant is not None:
        await _enqueue_collaborator_memories(
            engine, db.info.get("authenticated_actor"), project_id, grant, restored_memory_ids(attempted_effects),
        )
    if memory_changed and engine is not None and grant is None:
        from app.services import kb_index_service

        try:
            queued = await kb_index_service.enqueue_memory(
                engine.ctx.sessionmaker, actor_id=user_id, project_id=project_id,
                authenticated_actor=db.info.get("authenticated_actor"),
            )
        except Exception as exc:  # noqa: BLE001
            # 撤销已提交，不能把派生索引排队失败伪装成撤销失败，诱导重复提交。
            logger.warning("智能体撤销后的记忆索引排队失败 error_type=%s", type(exc).__name__)
        else:
            if queued is not None:
                engine.notify_queue_changed()
    for row in rows:
        await db.refresh(row)
    return await public_changes(db, user_id, project_id, rows)


async def undo_change(
    db: AsyncSession, user_id: int, project_id: int, change_id: int, expected: ChangeAction, *,
    engine: "TaskEngine",
) -> ChangeOut:
    entry, initial = await _change_entry(db, user_id, project_id, change_id, lock=True)
    # 撤销需要 ai.use + 原轮次（裁决 8、14）：先于投影核对判定，失败 409 permission_denied。
    await _require_change_write(db, entry, user_id, initial)
    await _require_change_action(db, user_id, project_id, initial, expected)
    cid = initial.conversation_id
    conversation_key = await _conversation_key(db, user_id, project_id, cid)
    await db.rollback()
    async with conversation_operations.hold(
        db, engine.request_worker, cid, user_id, project_id, conversation_key=conversation_key,
    ):
        entry, row = await _change_entry(db, user_id, project_id, change_id, lock=True)
        if row.conversation_id != cid:
            raise NotFoundError("修改提案不存在")
        await _require_change_write(db, entry, user_id, row)
        await conversation_operations.guard(db, cid)
        await _require_change_action(db, user_id, project_id, row, expected)
        if row.status == "undone":
            return (await public_changes(db, user_id, project_id, [row]))[0]
        task = await active_turn(db, cid)
        if task and task.status != "paused":
            raise ConflictError("请等待智能体暂停或结束后撤销")
        return (await _undo_changes(db, user_id, project_id, [row], engine))[0]


async def undo_turn(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, task_id: int, expected: TurnAction,
) -> TurnChangesOut:
    task = await _turn(db, user_id, project_id, task_id, lock=True)
    if not task.statistics_key or task.statistics_key != str(expected.task_key):
        raise ConflictError("原任务身份已变化，请刷新后重试", extra={"code": "agent_task_identity_required"})
    cid = _turn_conversation_id(task)
    conversation_key = await _conversation_key(db, user_id, project_id, cid)
    await db.rollback()
    async with conversation_operations.hold(
        db, engine.request_worker, cid, user_id, project_id, conversation_key=conversation_key,
    ):
        task = await _turn(db, user_id, project_id, task_id, lock=True)
        if task.statistics_key != str(expected.task_key) or _turn_conversation_id(task) != cid:
            raise ConflictError("原任务身份已变化，请刷新后重试", extra={"code": "agent_task_identity_required"})
        await conversation_operations.guard(db, cid)
        active = await active_turn(db, cid)
        if active and active.status != "paused":
            raise ConflictError("请等待智能体暂停或结束后撤销")
        rows = list(
            await db.scalars(
                select(AgentChange)
                .where(
                    AgentChange.turn_task_id == task_id,
                    AgentChange.status == "executed",
                    AgentChange.kind.not_in(("task", "external")),
                )
                .order_by(AgentChange.seq.desc(), AgentChange.id.desc())
            )
        )
        await _require_turn_actions(db, user_id, project_id, rows, expected)
        skipped = await db.scalar(
            select(func.count(AgentChange.id)).where(
                AgentChange.turn_task_id == task_id,
                AgentChange.status == "executed",
                AgentChange.kind == "external",
            )
        )
        changes = await _undo_changes(
            db, user_id, project_id, rows, engine, turn_grant=agent_access.task_grant(task),
        )
        conversation = await owned_conversation(db, user_id, project_id, cid)
        conversation_proof = {"id": conversation.id, "instance_key": conversation.instance_key}
        payload, proofs = await _prepare_committed_changes(db, user_id, project_id, rows)
        proofs["conversation"] = conversation_proof
        task = await task_service.get_owned_task(db, user_id, task_id, expected_task_key=str(expected.task_key))
        task_out = task_service.to_out(task, queue_position=await task_service.queue_position(db, task), engine=engine)
        proofs["turn"] = {
            "id": task.id, "statistics_key": task.statistics_key, "queued_at": task.queued_at,
            "snapshot": personal_projection.agent_task_evidence(task),
        }
        projected = await personal_projection.project_agent_payload(
            db, user_id, project_id, {"changes": payload, "task": task_out.model_dump(mode="json")}, proofs,
        )
        final = _public_change_models(projected["changes"], proofs, db)
        if [item.model_dump(mode="json") for item in final] != [item.model_dump(mode="json") for item in changes]:
            raise NotFoundError("修改提案不存在")
        return TurnChangesOut(
            changes=final, task=projected["task"],
            notice=f"已跳过 {skipped} 项外部操作（无法撤销）" if skipped else None,
        )
