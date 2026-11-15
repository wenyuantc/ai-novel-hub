"""Agent 快照事务使用固定数据库视图，不将并发请求的修改误计为自身效果。"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

import anyio
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError, ConflictError
from app.models import AdminAuditLog
from app.services.ai_dispatch import VisibilityPolicyError

CONCURRENT_CHANGE = "数据同时被其他操作修改，请刷新预览后重试"
EXTERNAL_CLAIM_KEY = "_external_claim_id"
VISIBILITY_BIND_KEY = "visibility_bind"


def public_change_payload(payload: dict) -> dict:
    """内部来源与外部占用证据不能进入 API 或事件。"""
    result = payload.get("result")
    if isinstance(result, dict) and (
        (payload.get("kind") == "task" and payload.get("status") == "proposed"
         and "_agent_task_origin" in result)
        or (payload.get("kind") == "external" and payload.get("status") == "executing"
            and EXTERNAL_CLAIM_KEY in result)
    ):
        return {**payload, "result": None}
    return payload


class AgentAuthorizationChangedError(VisibilityPolicyError):
    """授权失效会终止 Agent 轮次，不能转换成普通工具错误继续发送。"""

    def __init__(self) -> None:
        super().__init__("协作授权已变化，请重新发起", extra={"code": "permission_denied"})


class AgentTransactionError(AppError):
    """Agent 数据库故障的固定响应，不携带 SQL、参数或原始异常。"""

    status_code = 500


def session(sessionmaker):
    bind = sessionmaker.kw["bind"]
    if bind.dialect.name == "postgresql":
        # execution_options 每次返回新的引擎代理；记下原引擎，使受控发送会话按原引擎（默认隔离级别）
        # 建立，既不在加锁等待后沿用旧 RR 快照，也与其他入口构建的发送守卫保持同一引擎身份。
        return sessionmaker(
            bind=bind.execution_options(isolation_level="REPEATABLE READ"),
            info={VISIBILITY_BIND_KEY: bind},
        )
    return sessionmaker()


def is_serialization_error(exc: DBAPIError) -> bool:
    original = exc.orig
    return getattr(original, "sqlstate", None) == "40001" or getattr(original, "pgcode", None) == "40001"


async def rollback_conflict(db, exc: DBAPIError) -> None:
    conflict = is_serialization_error(exc)
    try:
        await db.rollback()
    except Exception:
        conflict = False  # 无法确认事务结束时不能提示可重试的冲突。
        try:
            await db.invalidate()
        except Exception:
            pass
    failure = ConflictError(CONCURRENT_CHANGE) if conflict else AgentTransactionError("服务器内部错误")
    try:
        raise failure from None
    except (ConflictError, AgentTransactionError) as safe:
        # 不把可能包含 SQL 参数的 DBAPIError 留在异常链上。
        safe.__context__ = None
        raise


_FAILURE_FIELDS = (
    "operation_id", "actor_user_id", "actor_username", "action", "result", "target_type", "target_id",
    "target_label", "owner_user_id", "owner_username", "project_id", "project_title", "chapter_id",
    "summary", "version_ref", "error_code", "error_message",
)


@dataclass(frozen=True, slots=True, repr=False)
class AgentAuditFailure:
    """仅保存 AuditOperation 已验证的安全字段；不持有 ORM 或异常。"""

    operation_id: str
    _payload: str

    @classmethod
    def from_event(cls, event: AdminAuditLog) -> "AgentAuditFailure":
        if event.result != "failure" or event.version_ref is not None:
            raise ValueError("仅允许暂存安全失败审计")
        values = {key: getattr(event, key, None) for key in _FAILURE_FIELDS}
        return cls(event.operation_id, json.dumps(values, ensure_ascii=False, separators=(",", ":")))

    def event(self) -> AdminAuditLog:
        values = json.loads(self._payload)
        if values.get("operation_id") != self.operation_id or values.get("result") != "failure":
            raise ValueError("审计身份不匹配")
        return AdminAuditLog(**values)


@dataclass(frozen=True, slots=True)
class AgentAuditScope:
    coordinator: "AgentAuditCoordinator"
    mode: Literal["preview", "execute"]

    def __post_init__(self) -> None:
        if self.mode not in ("preview", "execute"):
            raise ValueError("未知 Agent 审计阶段")


async def _existing_result(factory: async_sessionmaker[AsyncSession], operation_id: str) -> str | None:
    async with factory() as db:
        return await db.scalar(select(AdminAuditLog.result).where(AdminAuditLog.operation_id == operation_id))


async def _record_failure(factory: async_sessionmaker[AsyncSession], snapshot: AgentAuditFailure) -> str:
    """只在根事务已释放后调用；提交结果未知时按 operation_id 再核实。"""
    existing = await _existing_result(factory, snapshot.operation_id)
    if existing is not None:
        return existing
    try:
        async with factory() as db:
            db.add(snapshot.event())
            await db.commit()
        return "failure"
    except Exception:
        existing = await _existing_result(factory, snapshot.operation_id)
        if existing is not None:
            return existing
        raise


class AgentAuditReconciliationError(RuntimeError):
    """不能确认根事务或独立审计结果；不含 SQL 和业务参数。"""


class AgentAuditCoordinator:
    """只记录操作快照；调用方负责在所有保存点退出后交接根事务结果。"""

    def __init__(self) -> None:
        self._staged: dict[str, AgentAuditFailure] = {}
        self._failed: dict[str, AgentAuditFailure] = {}
        self._flushed: set[str] = set()
        self._prepared = False

    def stage(self, snapshot: AgentAuditFailure, *, mode: Literal["preview", "execute"]) -> None:
        if mode == "execute":
            self._staged[snapshot.operation_id] = snapshot
            self._prepared = False
        elif mode != "preview":
            raise ValueError("未知 Agent 审计阶段")

    def fail(self, snapshot: AgentAuditFailure) -> None:
        self._staged.pop(snapshot.operation_id, None)
        self._failed[snapshot.operation_id] = snapshot
        self._flushed.discard(snapshot.operation_id)
        self._prepared = False

    def discard(self, operation_id: str) -> None:
        """尚未认证的请求不属于管理员审计范围。"""
        self._staged.pop(operation_id, None)
        self._failed.pop(operation_id, None)
        self._flushed.discard(operation_id)
        self._prepared = False

    async def flush_failures(self, db: AsyncSession) -> None:
        """根事务准备提交前调用；不得在任何仍会回滚的保存点内调用。"""
        if db.in_nested_transaction():
            raise RuntimeError("失败审计必须在所有保存点退出后写入")
        # 工具执行成功后，外层保存点仍可能因效果快照/投影失败而回滚。
        # 无对应成功行意味着业务未提交，必须记录失败而不是静默丢弃暂存事实。
        for operation_id, snapshot in list(self._staged.items()):
            with db.no_autoflush:
                existing = await db.scalar(select(AdminAuditLog.result).where(
                    AdminAuditLog.operation_id == operation_id,
                ))
            if existing is None:
                self._staged.pop(operation_id)
                self._failed[operation_id] = snapshot
        for operation_id, snapshot in self._failed.items():
            if operation_id in self._flushed:
                continue
            with db.no_autoflush:
                existing = await db.scalar(select(AdminAuditLog.result).where(
                    AdminAuditLog.operation_id == operation_id,
                ))
            if existing is None:
                db.add(snapshot.event())
                await db.flush()
            self._flushed.add(operation_id)
        self._prepared = True

    def committed(self) -> None:
        """仅当根事务 commit 已确认返回后调用，遗留失败不得丢弃。"""
        if (self._staged or self._failed) and not self._prepared:
            raise RuntimeError("根事务提交前尚未核对审计")
        if self._failed.keys() - self._flushed:
            raise RuntimeError("失败审计尚未写入根事务")
        self._staged.clear()
        self._failed.clear()
        self._flushed.clear()
        self._prepared = False

    async def commit(self, db: AsyncSession, factory: async_sessionmaker[AsyncSession]) -> None:
        """根提交与失败核验的唯一入口；调用方须先退出所有保存点。"""
        try:
            await self.flush_failures(db)
            await db.commit()
        except BaseException:
            await self.reconcile_after_failure(db, factory)
            raise
        self.committed()

    @property
    def pending(self) -> bool:
        return bool(self._staged or self._failed)

    async def _reconcile(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession],
    ) -> dict[str, str]:
        if db.in_nested_transaction():
            raise RuntimeError("审计核验必须在所有保存点退出后执行")
        rollback_safe = True
        try:
            await db.rollback()
        except Exception:
            try:
                await db.invalidate()
            except Exception:
                rollback_safe = False
        snapshots = self._staged | self._failed
        results = {}
        for operation_id, snapshot in snapshots.items():
            outcome = (
                await _record_failure(factory, snapshot)
                if rollback_safe else await _existing_result(factory, operation_id)
            )
            if outcome is None:
                raise AgentAuditReconciliationError("事务结果未知，无法补写失败审计")
            results[operation_id] = outcome
        self._staged.clear()
        self._failed.clear()
        self._flushed.clear()
        self._prepared = False
        return results

    async def reconcile_after_failure(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession],
    ) -> dict[str, str]:
        """回滚/失效后按 operation_id 核实，重复取消不得中断独立安全收尾。"""
        async def protected() -> dict[str, str]:
            with anyio.CancelScope(shield=True):
                return await self._reconcile(db, factory)

        cleanup = asyncio.create_task(protected())
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
                if cleanup.cancelled():
                    raise
            except Exception:
                try:
                    raise AgentAuditReconciliationError("事务或审计结果无法确认") from None
                except AgentAuditReconciliationError as safe:
                    safe.__context__ = None
                    raise
        if cancelled is not None:
            raise cancelled
        return result


@asynccontextmanager
async def audit_scope(db: AsyncSession, factory: async_sessionmaker[AsyncSession]):
    """持有根事务协调器，异常、取消和生成器提前关闭时不丢失未决审计。"""
    coordinator = AgentAuditCoordinator()
    try:
        yield coordinator
    except BaseException:
        await coordinator.reconcile_after_failure(db, factory)
        raise
    else:
        if coordinator.pending:
            # 调用方遗漏了必要的根提交；失败关闭，不能把暂存成功当成已提交。
            await coordinator.reconcile_after_failure(db, factory)
            raise AgentAuditReconciliationError("事务退出时仍有未决审计")
