"""建索引任务 kb.index（serial_key = index:{project_id}，同项目串行）。三种模式：

1. document（document_id，resource_key = kbdoc:{id}）：文档还没有分块时从全文分块，
   写入 kb_chunks 与检索单元（同时算好关键词），
   提交后再分批（每批 32 条，每批提交一次）为本文档缺少当前模型向量的分块生成向量
   （含旧模型向量，替换范围仅限本文档，E6 裁决 12）。
2. memory（resource_key 为空）：为项目中缺少向量的记忆检索单元生成向量（分析成功 / 手动记忆变更后排队）；
   授权编辑者的人工记忆只处理受理时绑定的本次条目，分析派生只处理收据绑定的记忆。
3. rebuild（resource_key = kbrebuild:{project_id}）：为项目全部缺少当前模型向量的检索单元重新生成向量，并刷新文档状态。

- 中断后重试：已分块的文档不再重新分块，已有当前 embedding_key 的块跳过，从断点继续。
- 没有向量模型或生成向量失败：任务仍成功，result.warnings 给出原因，这些块只参与关键词检索
  （文档状态 ready_keyword_only）。
- 文档任务失败 / 取消 / 中断：文档状态置为 failed（on_abort），可重试任务或「重建该文档」。
- 进度：输出形如「生成向量 32/120」的行，progress 按最后一行的比例估算。
"""

import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import batched
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

import anyio
from sqlalchemy import String, cast, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.providers import AIProviderError, ProviderFactory
from app.core.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from app.core.security import SecretCipher
from app.models import (
    AnalysisIndexReceipt,
    ChapterAnalysis,
    KbChunk,
    KbDocument,
    Project,
    ProjectCollaborator,
    RetrievalChunk,
    SceneReviewAnalysisReceipt,
    StoryMemory,
    Task,
)
from app.models.entities import OPEN_TASK_STATUSES
from app.services import (
    ai_dispatch,
    kb_parser,
    project_access,
    project_lifecycle,
    retrieval_service,
    task_statistics_service,
)
from app.services import authorization_service as authz
from app.services.collaboration_task_access import GRANT_KEY, GrantIdentity, bind_task_grant, task_grant_identity
from app.services.project_service import get_owned_project
from app.services.retrieval_service import ChunkInput, Embedder

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services import analysis_service, project_ai_config
    from app.services.task_engine.execution_identity import TaskExecutionIdentity

KIND = "kb.index"
Mode = Literal["document", "memory", "rebuild"]
MODES = ("document", "memory", "rebuild")
NO_EMBEDDING = "未配置向量模型，仅使用关键词检索"
NO_EMBEDDING_REBUILD = "尚未配置向量模型，请先在「模型配置」中添加向量模型"
_PROGRESS_RE = re.compile(r"(\d+)/(\d+)\s*$")
# 作者单章分析（on_success）与批量分析阶段的收据式派生均已接通；关闭时拒绝受理。
_BOUND_MEMORY_READY = True
# 授权编辑者人工记忆（E6-c，裁决 4）：只向量化本次写入的条目；服务端写入，不接受客户端传入。
MANUAL_MEMORY_KEY = "_manual_memory_ref_ids"
MAX_MANUAL_MEMORY_REFS = 200
_ANALYSIS_MEMORY_KEYS = ("_memory_ref_ids", "_memory_analysis_id", "_analysis_index_receipt")


RetryBlockedReason = Literal["memory_bound", "legacy_index", "binding_invalid", "request_invalid"]


def retry_blocked_reason(task_input: object) -> RetryBlockedReason | None:
    """kb.index 任务仅凭自身输入即可确定「不能直接重试」的原因；``KbIndexHandler.validate_retry`` 与
    ``TaskOut.retry_blocked_reason`` 共用本函数，两处口径逐字一致（E6-f 裁决 19）。

    - ``legacy_index``：0045 前无占用文档任务的系统清理记录，须经「重新索引」新受理；
    - ``memory_bound``：分析派生 / 人工记忆条目绑定任务（来源不可重放），以及协作者的非文档、非重建任务；
    - ``binding_invalid``：协作绑定破损（无 grant 却带共享向量选择、grant 不可解析）；
    - ``request_invalid``：原请求结构破损（request 非对象）。
    None 只表示输入层面不拒绝；状态、管理员取消、轮次、授权等仍由重试受理时锁内判定。
    """
    payload = task_input if isinstance(task_input, dict) else {}
    if LEGACY_CLEANUP_KEY in payload:
        return "legacy_index"
    grant = task_grant_identity(payload)
    if any(key in payload for key in (*_ANALYSIS_MEMORY_KEYS, MANUAL_MEMORY_KEY)):
        return "memory_bound"
    if ("_shared_embedding" in payload and grant is None) or (GRANT_KEY in payload) != (grant is not None):
        return "binding_invalid"
    request = payload.get("request")
    if not isinstance(request, dict):
        return "request_invalid"
    if grant is not None and request.get("mode") not in ("document", "rebuild"):
        return "memory_bound"
    return None


class MemoryIndexCurrent(ConflictError):
    """本次条目都已有向量：无需建任务（调用方视为无工作，不是错误）。"""

    def __init__(self) -> None:
        super().__init__("记忆向量已是最新", extra={"code": "memory_index_current"})


def manual_memory_refs(task_input: object) -> list[int] | None:
    """授权编辑者人工记忆任务绑定的条目；破损值返回 None（调用方失败关闭）。"""
    refs = task_input.get(MANUAL_MEMORY_KEY) if isinstance(task_input, dict) else None
    if (type(refs) is not list or not refs or len(refs) > MAX_MANUAL_MEMORY_REFS
            or any(type(value) is not int or not 0 < value <= 2**31 - 1 for value in refs)
            or refs != sorted(set(refs))):
        return None
    return refs


@dataclass(frozen=True, slots=True, repr=False)
class AnalysisIndexAttempt:
    receipt_key: str
    child_task_key: str


@dataclass(frozen=True, slots=True, repr=False)
class AnalysisIndexAdmission:
    state: Literal["created", "existing", "no_work"]
    receipt_key: str
    child_task_key: str | None
    task_id: int | None
    source_kind: str
    source_key: str
    selection_hash: str
    memory_hash: str


def new_analysis_index_attempt() -> AnalysisIndexAttempt:
    """调用者保留这两个 UUID 直至提交确认，不能在 ACK 未知时重新生成。"""
    return AnalysisIndexAttempt(str(uuid4()), str(uuid4()))


class AnalysisIndexCommitUncertain(ConflictError):
    def __init__(self) -> None:
        super().__init__("分析索引提交结果尚未确认，请稍后核对", extra={"code": "analysis_index_uncertain"})


@dataclass(frozen=True, slots=True)
class DocumentIndexClaim:
    """仅供服务端持有的任务及本次文档占用；不是访问授权凭证。"""

    task_id: int
    task_key: str
    actor_id: int
    project_id: int
    document_id: int
    queued_at: datetime
    claim_key: str
    phase: Literal["queued", "running"]


def _claim_changed() -> ConflictError:
    return ConflictError("知识库索引任务状态已变化，请刷新后重试", extra={"code": "kb_index_claim_changed"})


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _document_request(payload: Any, project_id: int) -> int | None:
    request = payload.get("request") if isinstance(payload, dict) else None
    if not isinstance(request, dict) or request.get("mode") != "document":
        return None
    pid, document_id = request.get("project_id"), request.get("document_id")
    if type(pid) is not int or pid != project_id or type(document_id) is not int or document_id <= 0:
        return None
    return document_id


def _claim_task_conditions(db: AsyncSession, claim: DocumentIndexClaim, *, cleanup: bool):
    """SQL 原子核对任务身份、请求归属、入队轮次与实际领取者。"""
    conditions = _document_task_conditions(
        db, task_id=claim.task_id, task_key=claim.task_key, actor_id=claim.actor_id, project_id=claim.project_id,
        document_id=claim.document_id, queued_at=claim.queued_at,
    )
    terminal = ("cancelled", "failed", "interrupted") if cleanup else ()
    if claim.phase == "queued":
        conditions.extend((Task.execution_key.is_(None), Task.status.in_(("queued", *terminal))))
    elif claim.phase == "running":
        conditions.extend((Task.execution_key == claim.claim_key, Task.status.in_(("running", *terminal))))
    else:
        raise ValueError("索引占用阶段无效")
    if not cleanup:
        conditions.extend((Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None)))
    return conditions


def _document_task_conditions(
    db: AsyncSession, *, task_id: int, task_key: str, actor_id: int, project_id: int, document_id: int,
    queued_at: datetime,
) -> list:
    """任务 UUID/入队轮次/actor 与「本作品 document 模式、resource_key == kbdoc:{id}」的 SQL 核对。"""
    request = Task.input["request"]
    pid, did = request["project_id"], request["document_id"]
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        types = (
            func.json_type(Task.input, '$.request.project_id') == "integer",
            func.json_type(Task.input, '$.request.document_id') == "integer",
            func.json_type(Task.input, '$.request.mode') == "text",
        )
    elif dialect == "postgresql":
        types = (func.json_typeof(pid) == "number", func.json_typeof(did) == "number",
                 func.json_typeof(request["mode"]) == "string")
    else:
        raise RuntimeError("索引身份不支持当前数据库")
    return [
        Task.id == task_id, Task.statistics_key == task_key, Task.user_id == actor_id,
        Task.project_id == project_id,
        Task.kind == KIND, Task.resource_key == f"kbdoc:{document_id}", Task.queued_at == queued_at,
        request["mode"].as_string() == "document", cast(pid.as_string(), String) == str(project_id),
        cast(did.as_string(), String) == str(document_id), *types,
    ]


def _claim_document_conditions(claim: DocumentIndexClaim):
    return (
        KbDocument.id == claim.document_id, KbDocument.project_id == claim.project_id,
        KbDocument.index_task_key == claim.task_key, KbDocument.index_task_queued_at == claim.queued_at,
        KbDocument.index_claim_key == claim.claim_key,
    )


async def bind_queued_document_claim(
    db: AsyncSession, task_id: int, task_key: str | None, *, queue_claim_key: str,
) -> DocumentIndexClaim:
    """在受理事务中安装排队占用；调用前已鉴权、锁项目并 flush 任务，调用方负责提交。

    queue_claim_key 必须由服务端在本次受理时生成并保存到提交确认结束；绝不能取请求值。
    未知旧入队代次无法安全认领，旧执行器排空后须通过新受理事务建立新任务。
    """
    if type(task_id) is not int or task_id <= 0 or not _canonical_uuid(task_key) \
            or not _canonical_uuid(queue_claim_key) or task_key == queue_claim_key:
        raise _claim_changed()
    with db.no_autoflush:
        row = (await db.execute(select(
            Task.id, Task.statistics_key, Task.user_id, Task.project_id, Task.queued_at, Task.input, Task.resource_key,
            Task.status, Task.execution_key, Task.admin_cancel_operation_id,
        ).where(Task.id == task_id, Task.statistics_key == task_key))).one_or_none()
        if (row is None or type(row.project_id) is not int or type(row.user_id) is not int or row.queued_at is None
                or row.status != "queued"
                or row.execution_key is not None or row.admin_cancel_operation_id is not None):
            raise _claim_changed()
        document_id = _document_request(row.input, row.project_id)
        if document_id is None or row.resource_key != f"kbdoc:{document_id}":
            raise _claim_changed()
        queued_at = _utc(row.queued_at)
        claim = DocumentIndexClaim(
            task_id, task_key, row.user_id, row.project_id, document_id, queued_at, queue_claim_key, "queued",
        )
        current = (await db.execute(select(
            KbDocument.index_task_key, KbDocument.index_task_queued_at, KbDocument.index_claim_key, KbDocument.status,
        ).where(KbDocument.id == document_id, KbDocument.project_id == row.project_id))).one_or_none()
        if current is None or current.status != "pending":
            raise _claim_changed()
        if current.index_task_key is not None:
            same_round = (current.index_task_key == task_key and current.index_task_queued_at is not None
                          and _utc(current.index_task_queued_at) == queued_at)
            if same_round and current.index_claim_key != queue_claim_key:
                raise _claim_changed()
            if not same_round:
                # 未能证明旧任务已停止时不覆盖占用；终态旧任务的迟到清理将被新 claim 条件挡住。
                old = (await db.execute(select(Task.status, Task.admin_cancel_operation_id, Task.execution_key).where(
                    Task.statistics_key == current.index_task_key,
                ))).one_or_none()
                if old is None or old.status not in ("succeeded", "failed", "cancelled", "interrupted") \
                        or old.admin_cancel_operation_id is not None or old.execution_key is not None:
                    raise _claim_changed()
        expected_task = exists(select(Task.id).where(*_claim_task_conditions(db, claim, cleanup=False)))
        result = await db.execute(update(KbDocument).where(
            KbDocument.id == document_id, KbDocument.project_id == row.project_id, KbDocument.status == "pending",
            KbDocument.index_task_key == current.index_task_key,
            KbDocument.index_task_queued_at == current.index_task_queued_at,
            KbDocument.index_claim_key == current.index_claim_key, expected_task,
        ).values(index_task_key=task_key, index_task_queued_at=queued_at, index_claim_key=queue_claim_key)
            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise _claim_changed()
    return claim


def running_document_claim(task: Task, execution_key: str | None = None) -> DocumentIndexClaim | None:
    """用执行体已捕获的任务快照构造身份，不从调用者请求恢复内部 UUID。"""
    key = execution_key if execution_key is not None else task.execution_key
    if (task.kind != KIND or not _canonical_uuid(task.statistics_key) or not _canonical_uuid(key)
            or type(task.project_id) is not int or type(task.user_id) is not int or task.queued_at is None):
        return None
    document_id = _document_request(task.input, task.project_id)
    if document_id is None or task.resource_key != f"kbdoc:{document_id}":
        return None
    return DocumentIndexClaim(task.id, task.statistics_key, task.user_id, task.project_id, document_id,
                              _utc(task.queued_at), key, "running")


async def queued_document_claim(db: AsyncSession, task: Task) -> DocumentIndexClaim | None:
    """读取已绑定该任务及入队代次的文档占用；缺失旧身份时不补造。"""
    if (task.kind != KIND or not _canonical_uuid(task.statistics_key) or type(task.project_id) is not int
            or type(task.user_id) is not int or task.queued_at is None or task.execution_key is not None):
        return None
    document_id = _document_request(task.input, task.project_id)
    if document_id is None or task.resource_key != f"kbdoc:{document_id}":
        return None
    with db.no_autoflush:
        doc = (await db.execute(select(KbDocument.index_task_key, KbDocument.index_task_queued_at,
                                       KbDocument.index_claim_key).where(
            KbDocument.id == document_id, KbDocument.project_id == task.project_id,
        ))).one_or_none()
    if (doc is None or doc.index_task_key != task.statistics_key or doc.index_task_queued_at is None
            or _utc(doc.index_task_queued_at) != _utc(task.queued_at) or not _canonical_uuid(doc.index_claim_key)):
        return None
    return DocumentIndexClaim(task.id, task.statistics_key, task.user_id, task.project_id, document_id,
                              _utc(task.queued_at), doc.index_claim_key, "queued")


async def _lock_running_document(db: AsyncSession, claim: DocumentIndexClaim) -> KbDocument:
    """项目→任务→文档锁覆盖检查与文档、分块和向量写入的整个事务。"""
    await project_lifecycle.lock_active(db, claim.project_id)
    with db.no_autoflush:
        task_id = await db.scalar(select(Task.id).where(
            *_claim_task_conditions(db, claim, cleanup=False),
        ).with_for_update())
        doc = await db.scalar(select(KbDocument).where(
            *_claim_document_conditions(claim), KbDocument.status.in_(("pending", "indexing")),
        ).with_for_update())
    if task_id is None or doc is None:
        raise _claim_changed()
    return doc


async def activate_document_claim(
    db: AsyncSession, queued: DocumentIndexClaim, *, execution_key: str,
) -> DocumentIndexClaim:
    """领取者的 Task CAS 后、同事务提交前轮换文档 claim；失败须回滚整个领取。"""
    if queued.phase != "queued" or not _canonical_uuid(execution_key) \
            or execution_key in (queued.claim_key, queued.task_key):
        raise _claim_changed()
    running = DocumentIndexClaim(
        queued.task_id, queued.task_key, queued.actor_id, queued.project_id, queued.document_id, queued.queued_at,
        execution_key, "running",
    )
    with db.no_autoflush:
        expected_task = exists(select(Task.id).where(*_claim_task_conditions(db, running, cleanup=False)))
        result = await db.execute(update(KbDocument).where(
            *_claim_document_conditions(queued), KbDocument.status == "pending", expected_task,
        ).values(index_claim_key=execution_key).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise _claim_changed()
    return running


async def document_claim_is_current(db: AsyncSession, claim: DocumentIndexClaim) -> bool:
    """只验证本次活动身份；鉴权、模型绑定与项目生命周期由调用者另行重验。"""
    with db.no_autoflush:
        return bool(await db.scalar(select(exists(select(KbDocument.id).where(
            *_claim_document_conditions(claim), KbDocument.status.in_(("pending", "indexing")),
            exists(select(Task.id).where(*_claim_task_conditions(db, claim, cleanup=False))),
        )))))


def _valid_running_document_claim(claim: object, actor: object, grant_identity: object) -> bool:
    return bool(
        type(claim) is DocumentIndexClaim and type(actor) is authz.AuthenticatedActor
        and all(type(value) is int and 0 < value <= 2**31 - 1 for value in (
            claim.task_id, claim.actor_id, claim.project_id, claim.document_id,
        ))
        and actor.id == claim.actor_id and type(actor.token_version) is int and actor.token_version >= 0
        and _canonical_uuid(claim.task_key) and _canonical_uuid(claim.claim_key) and claim.task_key != claim.claim_key
        and isinstance(claim.queued_at, datetime) and claim.phase == "running"
        and (grant_identity is None or (type(grant_identity) is tuple and len(grant_identity) == 2
                                         and _canonical_uuid(grant_identity[0])
                                         and type(grant_identity[1]) is int and 0 < grant_identity[1] <= 2**31 - 1))
    )


async def _lock_document_claim_rows(db: AsyncSession, claim: DocumentIndexClaim, *, write: bool) -> None:
    """调用者先取得用户和作品屏障；按 Task → Document 顺序持行锁。"""
    with db.no_autoflush:
        task_id = await db.scalar(select(Task.id).where(
            *_claim_task_conditions(db, claim, cleanup=False),
        ).with_for_update(read=not write))
        document_id = await db.scalar(select(KbDocument.id).where(
            *_claim_document_conditions(claim), KbDocument.status.in_(("pending", "indexing")),
        ).with_for_update(read=not write))
    if task_id != claim.task_id or document_id != claim.document_id:
        raise _claim_changed()


async def require_writing_document_claim(
    db: AsyncSession, claim: DocumentIndexClaim, *, actor: authz.AuthenticatedActor,
    grant_identity: GrantIdentity | None,
) -> None:
    """同一根写事务重验原 token、邀请、执行和文档占用，锁保持至提交。"""
    try:
        if (not _valid_running_document_claim(claim, actor, grant_identity)
                or db.get_nested_transaction() is not None or db.new or db.dirty or db.deleted):
            raise _claim_changed()
        with db.no_autoflush:
            original_project = await project_lifecycle.load_record(db, claim.project_id)
            if original_project is None:
                raise _claim_changed()
            await authz.require_authenticated(db, actor, write=True, targets=(original_project.user_id,))
            project = await project_access.require_project(
                db, actor.id, claim.project_id, "ai.use", for_update=True, visibility_write=True,
                grant_identity=grant_identity,
            )
            if (project.user_id == actor.id) != (grant_identity is None):
                raise _claim_changed()
            await _lock_document_claim_rows(db, claim, write=True)
    except BaseException:
        await db.rollback()
        raise


async def require_sending_document_claim(
    db: AsyncSession, claim: DocumentIndexClaim, *, actor: authz.AuthenticatedActor,
    grant_identity: GrantIdentity | None, preflight: bool,
) -> None:
    """只能在实际发送或预检的同一物理租约内使用，持文档读锁至租约结束。"""
    try:
        if (not _valid_running_document_claim(claim, actor, grant_identity) or type(preflight) is not bool
                or db.get_nested_transaction() is not None or db.new or db.dirty or db.deleted):
            raise _claim_changed()
        require_lease = ai_dispatch.require_preflight_lease if preflight else ai_dispatch.require_actual_send_lease
        await require_lease(db, actor.id, claim.project_id)
        with db.no_autoflush:
            await authz.require_authenticated_for_send(db, actor)
            statement = select(Project.id).where(
                Project.id == claim.project_id, *project_lifecycle.active_conditions(),
            )
            if db.get_bind().dialect.name == "postgresql":
                statement = statement.with_for_update(read=True)
            if await db.scalar(statement) is None:
                raise _claim_changed()
            project = await project_access.require_project(
                db, actor.id, claim.project_id, "ai.use", grant_identity=grant_identity,
            )
            if (project.user_id == actor.id) != (grant_identity is None):
                raise _claim_changed()
            await _lock_document_claim_rows(db, claim, write=False)
        await require_lease(db, actor.id, claim.project_id)
        return
    except Exception:
        ai_dispatch._invalidate_send_lease(db)
        await db.rollback()
    except BaseException:
        ai_dispatch._invalidate_send_lease(db)
        await db.rollback()
        raise
    raise ai_dispatch.VisibilityPolicyError("知识库索引状态已变化，已停止发送", extra={
        "code": "kb_index_claim_changed",
    })


async def release_document_claim(db: AsyncSession, claim: DocumentIndexClaim) -> bool:
    """仅清理已停止/正在安全收尾的本轮占用；调用者须先证明执行体不会继续写入。"""
    with db.no_autoflush:
        result = await db.execute(update(KbDocument).where(
            *_claim_document_conditions(claim), KbDocument.status.in_(("pending", "indexing")),
            exists(select(Task.id).where(*_claim_task_conditions(db, claim, cleanup=True))),
        ).values(status="failed", error="建索引未完成（失败、取消或中断），请重试", index_task_key=None,
                 index_task_queued_at=None, index_claim_key=None)
            .execution_options(synchronize_session=False))
    return result.rowcount == 1


# ---------------- 旧无占用文档任务的系统清理（E4-e） ----------------

# 0045 之前入队的文档任务在文档侧没有 index_task_key/queued_at/claim_key。不补造身份、不执行；
# 只在锁内证明「没有执行体且文档无人占用」后，把任务与文档一并记为未完成，提示作者重新索引。
LEGACY_CLEANUP_KEY = "_kb_legacy_unclaimed_cleanup"
LEGACY_TASK_MESSAGES = {
    "failed": "旧版知识库索引任务缺少占用记录，未执行；请在知识库中重新索引该文档",
    "cancelled": "任务已取消；旧版知识库索引任务已清理，请在知识库中重新索引该文档",
}
LEGACY_DOCUMENT_ERROR = "旧版建索引任务已清理，请重新索引"


@dataclass(frozen=True, slots=True)
class LegacyDocumentTask:
    """调用方在读取时捕获的旧任务身份；只是形状检查结果，不代表可以清理。"""

    task_id: int
    task_key: str
    actor_id: int
    project_id: int
    document_id: int
    queued_at: datetime


def capture_legacy_document_task(task: Task, *, task_key: object, queued_at: object) -> LegacyDocumentTask | None:
    """task_key/queued_at 必须是调用方先前读取到的值；与当前快照不一致或缺失时不清理。"""
    if (task.kind != KIND or type(task.id) is not int or not _canonical_uuid(task_key)
            or task.statistics_key != task_key or type(task.project_id) is not int or type(task.user_id) is not int
            or not isinstance(queued_at, datetime) or task.queued_at is None
            or _utc(task.queued_at) != _utc(queued_at)):
        return None
    document_id = _document_request(task.input, task.project_id)
    if document_id is None or task.resource_key != f"kbdoc:{document_id}":
        return None
    return LegacyDocumentTask(task.id, task_key, task.user_id, task.project_id, document_id, _utc(queued_at))


def _legacy_task_conditions(db: AsyncSession, legacy: LegacyDocumentTask) -> list:
    return [
        *_document_task_conditions(
            db, task_id=legacy.task_id, task_key=legacy.task_key, actor_id=legacy.actor_id,
            project_id=legacy.project_id, document_id=legacy.document_id, queued_at=legacy.queued_at,
        ),
        Task.status == "queued", Task.execution_key.is_(None),
        Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
    ]


def _legacy_document_conditions(legacy: LegacyDocumentTask) -> tuple:
    return (
        KbDocument.id == legacy.document_id, KbDocument.project_id == legacy.project_id,
        KbDocument.status == "pending", KbDocument.index_task_key.is_(None),
        KbDocument.index_task_queued_at.is_(None), KbDocument.index_claim_key.is_(None),
    )


async def cleanup_legacy_unclaimed_document_task(
    db: AsyncSession, legacy: LegacyDocumentTask, *, status: Literal["failed", "cancelled"],
) -> Task | None:
    """旧无占用文档任务的受限系统清理；调用方已按原锁序持有作品父行锁并负责提交。

    全部证明在本事务锁内成立才写入：Task 仍按原 UUID/入队轮次排队、无执行键与管理员取消意图、
    请求为本作品 document 模式且 resource_key 对应；文档属本作品、pending 且三列占用为空；
    该文档没有其他未结束的 kb.index 任务。随后两次 CAS（Task → 终态、文档 → failed）。
    不写正文/分块/向量，不调用模型，不派生任务。返回 None 表示无法证明，调用方须回滚并保持原屏障。
    """
    from app.services.task_service import STAGE_LABELS

    if type(legacy) is not LegacyDocumentTask or status not in LEGACY_TASK_MESSAGES:
        raise ValueError("旧索引任务清理参数无效")
    with db.no_autoflush:
        # 锁序与领取/取消一致：作品（调用方）→ Task → Document。
        task = await db.scalar(select(Task).where(*_legacy_task_conditions(db, legacy)).with_for_update()
                               .execution_options(populate_existing=True))
        if task is None or not isinstance(task.input, dict) or LEGACY_CLEANUP_KEY in task.input:
            return None
        document_id = await db.scalar(select(KbDocument.id).where(
            *_legacy_document_conditions(legacy),
        ).with_for_update())
        if document_id != legacy.document_id:
            return None
        others = await db.execute(select(Task.id, Task.project_id, Task.resource_key, Task.input).where(
            Task.kind == KIND, Task.id != legacy.task_id,
            or_(Task.status.in_(OPEN_TASK_STATUSES), Task.execution_key.is_not(None),
                Task.admin_cancel_operation_id.is_not(None), Task.admin_cancel_requested_at.is_not(None)),
            or_(Task.project_id == legacy.project_id, Task.resource_key == f"kbdoc:{legacy.document_id}"),
        ))
        for other in others:
            if (other.resource_key == f"kbdoc:{legacy.document_id}"
                    or _document_request(other.input, legacy.project_id) == legacy.document_id):
                return None
        stamp = task_statistics_service.now_utc()
        payload = {**task.input, LEGACY_CLEANUP_KEY: True}
        changed = await db.execute(update(Task).where(*_legacy_task_conditions(db, legacy)).values(
            status=status, input=payload, error=LEGACY_TASK_MESSAGES[status], result=None,
            stage=STAGE_LABELS[status], finished_at=stamp,
        ).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            return None
        released = await db.execute(update(KbDocument).where(*_legacy_document_conditions(legacy)).values(
            status="failed", error=LEGACY_DOCUMENT_ERROR,
        ).execution_options(synchronize_session=False))
        if released.rowcount != 1:
            return None
        await task_statistics_service.record_state_change(db, task, status, finished_at=stamp)
    return task


async def legacy_cleanup_committed(
    db: AsyncSession, legacy: LegacyDocumentTask, *, status: Literal["failed", "cancelled"],
) -> bool:
    """提交确认丢失时，只按原 UUID/入队轮次的持久事实确认，不重放 CAS（两次 CAS 同事务提交）。"""
    with db.no_autoflush:
        row = (await db.execute(select(Task.input).where(
            Task.id == legacy.task_id, Task.statistics_key == legacy.task_key, Task.queued_at == legacy.queued_at,
            Task.status == status, Task.error == LEGACY_TASK_MESSAGES[status], Task.execution_key.is_(None),
        ))).one_or_none()
    return row is not None and isinstance(row.input, dict) and row.input.get(LEGACY_CLEANUP_KEY) is True


def serial_key(project_id: int) -> str:
    return f"index:{project_id}"


@dataclass
class IndexJob:
    mode: Mode
    project_id: int
    document_id: int | None
    embedder: Embedder | None
    task_id: int | None = None
    document_claim: DocumentIndexClaim | None = None
    warnings: list[str] = field(default_factory=list)
    # 配置了向量模型但不可用（端点被拦截、密钥无法解密）时的原因；没有配置时为 None
    embedder_error: str | None = None
    task_execution_identity: "TaskExecutionIdentity | None" = field(default=None, repr=False)
    selection: "project_ai_config.ResolvedSharedEmbedding | None" = field(default=None, repr=False)
    cipher: SecretCipher | None = field(default=None, repr=False)
    factory: ProviderFactory | None = field(default=None, repr=False)
    ai_timeout: float = 0
    allow_private: bool = False
    visibility_stamp: ai_dispatch.VisibilityStamp | None = field(default=None, repr=False)
    # 真实执行者与协作授权轮次（E6-a）：作者任务 grant_identity 为 None；协作者任务携带受理时的原邀请轮次，
    # 向量选择恒为受理时冻结的作者配置（selection），绝不解析协作者本人的向量模型。
    actor_id: int | None = None
    grant_identity: GrantIdentity | None = None
    # 作品作者（向量配置归属）；每批写入按 User ID 升序同时取 actor 与作者屏障。
    project_owner_id: int | None = None
    # 分析派生的绑定记忆（带不可变收据）；其余模式（文档/重建/作者旧记忆）走 _embed_stale 的受控批次。
    bound_memory: bool = False
    # 受控批次因来源/可见性代次反复变化或批次上限而未处理完：文档记为仅关键词并附提示。
    incomplete: bool = False
    # 授权编辑者人工记忆任务只处理受理时绑定的条目（E6-c）；None 表示作者旧记忆（全书补空向量）。
    memory_ref_ids: list[int] | None = None

    def no_embedding_warning(self) -> str:
        return f"向量模型不可用：{self.embedder_error}；仅使用关键词检索" if self.embedder_error else NO_EMBEDDING


# ---------------- 创建 ----------------


async def validate_index_task(
    db: AsyncSession, *, user_id: int, params: dict[str, Any], cipher: SecretCipher | None = None,
    allow_private: bool = False,
) -> dict[str, Any]:
    """返回 TaskSpec 字段（不 commit）。文档模式把文档状态置为 pending。

    作者沿用原受理；非作者走协作受理（E6-a，不依赖文本模型）：锁内冻结原邀请轮次与作者向量选择。
    协作者 HTTP 入口仍由 kb_service 在调用前失败关闭（E6-c 才开放）。
    """
    from app.services import kb_service

    mode = params.get("mode")
    if mode not in MODES:
        raise BadRequestError("建索引模式不正确")
    project_id = int(params["project_id"])
    with db.no_autoflush:
        owner_id = await db.scalar(select(Project.user_id).where(Project.id == project_id))
    if owner_id is not None and owner_id != user_id:
        return await _validate_collaborator_index_task(
            db, user_id=user_id, project_id=project_id, mode=mode, params=params, cipher=cipher,
            allow_private=allow_private,
        )
    project = await get_owned_project(db, user_id, project_id, lock=True)
    request: dict[str, Any] = {"project_id": project_id, "mode": mode}
    if mode == "document":
        doc = await kb_service.get_project_document(db, user_id, project_id, int(params["document_id"]))
        doc.status, doc.error = "pending", ""
        request["document_id"] = doc.id
        return {
            "project_id": project_id,
            "resource_key": kb_service.doc_resource_key(doc.id),
            "title": f"知识库建索引 · 《{doc.filename}》",
            "input": {"request": request, "document_id": doc.id},
        }
    if mode == "rebuild":
        if await retrieval_service.default_embedding_config(db, project.user_id) is None:
            raise BadRequestError(NO_EMBEDDING_REBUILD)
        return {
            "project_id": project_id,
            "resource_key": f"kbrebuild:{project_id}",
            "title": "重建检索索引",
            "input": {"request": request},
        }
    return {"project_id": project_id, "resource_key": None, "title": "剧情记忆向量", "input": {"request": request}}


async def _validate_collaborator_index_task(
    db: AsyncSession, *, user_id: int, project_id: int, mode: str, params: dict[str, Any],
    cipher: SecretCipher | None, allow_private: bool,
) -> dict[str, Any]:
    """协作者受理：认证/User → 发送写门禁 → 作品父行锁后重读成员，并冻结作者向量选择与原邀请轮次。

    服务端覆盖 `_collaboration_grant` / `_shared_embedding`（客户端保留键已由中央受理拒绝）；
    向量选择只取作者默认向量配置，协作者本人有无向量配置都不参与。作者未授权 → 403
    `owner_embedding_not_allowed`；作者无向量配置 → 原缺失提示。

    记忆模式（E6-c，裁决 4）只供人工记忆写入后的派生：参数须带写入时的原邀请轮次与本次条目 ID，
    锁内只绑定其中仍缺向量的条目；都已有向量 → MemoryIndexCurrent（不建任务）。
    """
    from app.services import kb_service, project_ai_config

    if cipher is None:
        raise NotFoundError("项目不存在")
    project = await project_access.require_project(
        db, user_id, project_id, "ai.use", for_update=True, visibility_write=True,
    )
    if project.user_id == user_id:
        raise ConflictError("作品权限已变化，请重新发起", extra={"code": "collaboration_busy"})
    memory_refs: list[int] | None = None
    if mode == "memory":
        expected, raw_ids = params.get("expected_grant"), params.get("memory_ids")
        if (type(expected) not in (list, tuple) or len(expected) != 2 or type(raw_ids) is not list
                or not raw_ids or len(raw_ids) > MAX_MANUAL_MEMORY_REFS
                or any(type(value) is not int or not 0 < value <= 2**31 - 1 for value in raw_ids)):
            raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == user_id,
        ).execution_options(populate_existing=True))
        if (member is None or member.status != "accepted" or member.role != "editor"
                or (member.invitation_id, member.revision) != tuple(expected)):
            raise ForbiddenError("协作授权已变化", extra={"code": "permission_denied"})
        memory_refs = await _manual_memory_targets(db, project_id, sorted(set(raw_ids)))
    selection = await project_ai_config.resolve_shared_embedding(
        db, user_id, project_id, cipher=cipher, require_vector=True, allow_private=allow_private, for_update=True,
    )
    if (selection.mode != "vector" or selection.model is None or selection.grant_identity is None
            or selection.actor_id != user_id or selection.project_owner_id != project.user_id
            or selection.model.config_owner_id != project.user_id):
        raise ConflictError("模型授权或配置已变化，请重新发起", extra={"code": "collaboration_busy"})
    request: dict[str, Any] = {"project_id": project_id, "mode": mode}
    task_input: dict[str, Any] = {"request": request}
    if mode == "document":
        doc = await kb_service.get_project_document(db, user_id, project_id, int(params["document_id"]))
        doc.status, doc.error = "pending", ""
        request["document_id"] = doc.id
        task_input["document_id"] = doc.id
        spec = {"resource_key": kb_service.doc_resource_key(doc.id), "title": f"知识库建索引 · 《{doc.filename}》"}
    elif mode == "memory":
        task_input[MANUAL_MEMORY_KEY] = memory_refs
        spec = {"resource_key": None, "title": "剧情记忆向量"}
    else:
        spec = {"resource_key": f"kbrebuild:{project_id}", "title": "重建检索索引"}
    task_input["_shared_embedding"] = project_ai_config.serialize_shared_embedding(selection)
    task_input = bind_task_grant(task_input, selection.grant_identity)
    return {"project_id": project_id, **spec, "input": task_input}


async def _manual_memory_targets(db: AsyncSession, project_id: int, memory_ids: list[int]) -> list[int]:
    """锁内核对本次条目属于本作品且检索单元完整；只返回仍缺向量的条目（不补全书）。"""
    with db.no_autoflush:
        found = set(await db.scalars(select(StoryMemory.id).where(
            StoryMemory.project_id == project_id, StoryMemory.id.in_(memory_ids),
        )))
        if found != set(memory_ids):
            raise NotFoundError("记忆不存在")
        rows = (await db.execute(select(
            RetrievalChunk.ref_id, RetrievalChunk.embedding, RetrievalChunk.embedding_key,
        ).where(RetrievalChunk.project_id == project_id, RetrievalChunk.source == "memory",
                RetrievalChunk.ref_id.in_(memory_ids)))).all()
    if {row.ref_id for row in rows} != set(memory_ids) or len(rows) != len(memory_ids):
        raise retrieval_service._invalid_retrieval_reference()
    empty = sorted(row.ref_id for row in rows if row.embedding is None and row.embedding_key is None)
    if not empty:
        raise MemoryIndexCurrent()
    return empty


async def enqueue_collaborator_memory(
    engine, *, authenticated_actor: authz.AuthenticatedActor, project_id: int, grant: GrantIdentity,
    memory_ids: list[int],
) -> int | None:
    """授权编辑者人工记忆写入后的派生（E6-c，裁决 4）：只为本次条目建作者向量任务，任务归编辑者。

    经中央协作受理（认证/User → 发送写门禁 → 作品父行锁后按写入时的原邀请轮次重验并冻结作者向量）；
    无授权、作者无向量配置或条目都已有向量时不建任务（返回 None），零向量调用。
    """
    from app.services import task_service

    if (type(authenticated_actor) is not authz.AuthenticatedActor or type(project_id) is not int or project_id <= 0
            or task_grant_identity({GRANT_KEY: {"invitation_id": grant[0], "revision": grant[1]}}) != grant
            or type(memory_ids) is not list or not memory_ids):
        raise UnauthorizedError("未登录或登录已过期")
    async with engine.ctx.sessionmaker() as db:
        db.info["authenticated_actor"] = authenticated_actor
        with db.no_autoflush:
            owner_id = await db.scalar(select(Project.user_id).where(Project.id == project_id))
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project_id,
                ProjectCollaborator.user_id == authenticated_actor.id,
            ))
            if (owner_id is None or member is None or not member.allow_owner_ai
                    or await retrieval_service.default_embedding_config(db, owner_id) is None):
                return None
        await db.rollback()
        try:
            out = await task_service.create_task(db, engine, authenticated_actor.id, KIND, {
                "project_id": project_id, "mode": "memory", "memory_ids": list(memory_ids),
                "expected_grant": [grant[0], grant[1]],
            })
        except MemoryIndexCurrent:
            return None
        return out.id


async def _enqueue_memory_locked(
    db: AsyncSession, *, actor_id: int, project_id: int, actor_token_version: int,
) -> int | None:
    """人工作者的旧向量空间；分析派生只通过独立不可变收据入口。"""
    from app.services import task_service

    if type(actor_token_version) is not int or actor_token_version < 0:
        raise UnauthorizedError("未登录或登录已过期")
    if db.new or db.dirty or db.deleted:
        raise ConflictError("当前事务存在未提交内容，无法安全排队")
    with db.no_autoflush:
        cfg = await retrieval_service.default_embedding_config(db, actor_id)
        if cfg is None or not await db.scalar(select(RetrievalChunk.id).where(
            RetrievalChunk.project_id == project_id, RetrievalChunk.source == "memory",
            RetrievalChunk.embedding.is_(None), RetrievalChunk.embedding_key.is_(None),
        ).limit(1)):
            return None
        queued = list(await db.scalars(
            select(Task).where(
                Task.project_id == project_id,
                Task.user_id == actor_id,
                Task.kind == KIND,
                Task.status == "queued",
                Task.resource_key.is_(None),
                Task.execution_key.is_(None),
                Task.admin_cancel_operation_id.is_(None),
                Task.input["request"]["mode"].as_string() == "memory",
                Task.input["_task_actor_token_version"].as_integer() == actor_token_version,
            )
        ))
        if any(isinstance(item.input, dict) and "_shared_embedding" not in item.input for item in queued):
            return None
    if db.new or db.dirty or db.deleted:
        from app.services import analysis_service

        # 会话若在锁内校验期间被加入待写对象，仍不能让 begin_nested 隐式 flush。
        raise analysis_service.AnalysisExecutionChanged()
    async with db.begin_nested():
        task_input = {"request": {"project_id": project_id, "mode": "memory"},
                      "_task_actor_token_version": actor_token_version}
        task = Task(
            user_id=actor_id,
            project_id=project_id,
            kind=KIND,
            title="剧情记忆向量",
            serial_key=serial_key(project_id),
            status="queued",
            input=task_input,
            stage=task_service.STAGE_LABELS["queued"],
        )
        db.add(task)
        await task_statistics_service.record_created(db, task)
        return task.id


async def _verified_analysis_index_source(db, execution, reference, *, cipher, allow_private):
    from app.services import analysis_service, project_ai_config

    if (db.new or db.dirty or db.deleted or not analysis_service._valid_execution(execution)
            or cipher is None or type(allow_private) is not bool
            or execution.embedding_selection.actor_id != execution.actor_id
            or execution.embedding_selection.grant_identity != execution.grant_identity
            # 作者 ⇔ 无 grant；授权编辑者（E6-c）以原邀请轮次派生，向量仍是受理时冻结的作者配置。
            or (execution.grant_identity is None)
            != (execution.actor_id == execution.embedding_selection.project_owner_id)):
        raise analysis_service.AnalysisExecutionChanged()
    if execution.task_kind == analysis_service.KIND:
        if reference is not None:
            raise analysis_service.AnalysisExecutionChanged()
        await analysis_service._analysis_write_barrier(db, execution, status="succeeded")
        try:
            receipt = await analysis_service.completed_analysis_receipt(db, execution, status="succeeded")
        except Exception:  # noqa: BLE001  收据或存储不可用均不能派生，且不能输出 SQL 或正文。
            raise analysis_service.AnalysisExecutionChanged() from None
        if (type(receipt) is not tuple or len(receipt) != 2
                or any(type(value) is not dict for value in receipt)):
            raise analysis_service.AnalysisExecutionChanged()
        saved = receipt[0]
        source = {"source_kind": "single", "source_key": saved.get("nonce"),
                  "source_task_key": execution.task_key, "chapter_id": execution.task_chapter_id,
                  "analysis_id": saved.get("analysis_id"), "analysis_hash": saved.get("analysis_hash"),
                  "content_hash": saved.get("content_hash")}
    else:
        if type(reference) is not analysis_service.AnalysisReference:
            raise analysis_service.AnalysisExecutionChanged()
        try:
            if analysis_service.capture_analysis_reference(reference.as_dict()) != reference:
                raise analysis_service.AnalysisExecutionChanged()
            await analysis_service._analysis_write_barrier(db, execution, status="running")
            await analysis_service.require_completed_batch_analysis(db, execution, reference)
            analysis_id = await db.scalar(select(SceneReviewAnalysisReceipt.analysis_id).where(
                SceneReviewAnalysisReceipt.run_key == reference.run_key,
            ))
            analysis = await db.scalar(select(ChapterAnalysis).where(
                ChapterAnalysis.id == analysis_id, ChapterAnalysis.project_id == execution.project_id,
                ChapterAnalysis.chapter_id == reference.chapter_id,
            ).with_for_update().execution_options(populate_existing=True))
            if analysis is None:
                raise analysis_service.AnalysisExecutionChanged()
            source = {"source_kind": "batch", "source_key": reference.run_key,
                      "source_task_key": reference.task_key, "chapter_id": reference.chapter_id,
                      "analysis_id": analysis_id, "analysis_hash": analysis_service._analysis_digest(analysis),
                      "content_hash": reference.content_hash}
        except Exception:  # noqa: BLE001  拒绝伪造、失效收据或无法安全检查的数据库状态。
            raise analysis_service.AnalysisExecutionChanged() from None
    if (type(source["analysis_id"]) is not int or source["analysis_id"] <= 0
            or type(source["chapter_id"]) is not int or source["chapter_id"] <= 0
            or not _canonical_uuid(source["source_key"]) or not _canonical_uuid(source["source_task_key"])
            or any(type(source[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", source[key]) is None
                   for key in ("analysis_hash", "content_hash"))):
        raise analysis_service.AnalysisExecutionChanged()
    selection = execution.embedding_selection
    await project_ai_config.revalidate_shared_embedding(
        db, selection, cipher=cipher, allow_private=allow_private, for_update=True,
    )
    source["selection_hash"] = retrieval_service._canonical_digest(
        project_ai_config.serialize_shared_embedding(selection),
    )
    return source


async def _analysis_index_memory_refs(db, execution, source):
    """完整锁内快照：异常关联/损坏向量不能伪装成无需索引。"""
    memories = (await db.execute(select(
        StoryMemory.id, StoryMemory.chapter_id, StoryMemory.chapter_no, StoryMemory.analysis_id,
        StoryMemory.pinned, StoryMemory.muted,
    ).where(StoryMemory.project_id == execution.project_id, StoryMemory.analysis_id == source["analysis_id"])
        .order_by(StoryMemory.id).with_for_update())).all()
    if len(memories) > 10000 or any(row.chapter_id != source["chapter_id"] for row in memories):
        raise _claim_changed()
    snapshots, has_empty = [], False
    for batch in batched(memories, 400):
        ids = [row.id for row in batch]
        indexed = (await db.execute(select(
            RetrievalChunk.id, RetrievalChunk.ref_id, RetrievalChunk.chapter_no, RetrievalChunk.pinned,
            RetrievalChunk.enabled, RetrievalChunk.embedding, RetrievalChunk.embedding_key,
        ).where(RetrievalChunk.project_id == execution.project_id, RetrievalChunk.source == "memory",
                RetrievalChunk.ref_id.in_(ids)).order_by(RetrievalChunk.ref_id).with_for_update())).all()
        if {row.ref_id for row in indexed} != set(ids):
            raise _claim_changed()
        by_ref = {row.id: row for row in batch}
        for row in indexed:
            memory = by_ref[row.ref_id]
            if (row.chapter_no != memory.chapter_no or row.pinned != memory.pinned
                    or row.enabled != (not memory.muted) or (row.embedding is None) != (row.embedding_key is None)):
                raise _claim_changed()
            has_empty = has_empty or row.embedding is None
        await retrieval_service.validate_project_source_refs(db, execution.project_id, "memory", ids)
        snapshots.extend(await retrieval_service.snapshot_embedding_inputs(db, execution.project_id,
                                                                           [row.id for row in indexed]))
    refs = sorted((retrieval_service.analysis_index_memory_ref(item, source["analysis_id"]) for item in snapshots),
                  key=lambda item: item["ref_id"])
    return refs, has_empty


def _matching_analysis_index_receipt(row, execution, source) -> bool:
    grant = execution.grant_identity if execution.grant_identity is not None else (None, None)
    if (not isinstance(row, AnalysisIndexReceipt) or row.source_kind != source["source_kind"]
            or row.source_key != source["source_key"] or row.source_task_key != source["source_task_key"]
            or row.actor_id != execution.actor_id or row.actor_token_version != execution.actor_token_version
            or row.project_id != execution.project_id or row.chapter_id != source["chapter_id"]
            or row.analysis_id != source["analysis_id"] or row.analysis_hash != source["analysis_hash"]
            or row.content_hash != source["content_hash"] or row.selection_hash != source["selection_hash"]
            or (row.grant_invitation_id, row.grant_revision) != grant
            or row.outcome not in ("queued", "no_work")
            or (row.outcome == "queued") != (row.child_task_key is not None)):
        return False
    try:
        refs = retrieval_service.validate_analysis_index_refs(
            row.memory_refs, analysis_id=source["analysis_id"], memory_hash=row.memory_hash,
        )
    except retrieval_service.EmbeddingPolicyError:
        return False
    if row.outcome == "queued" and not refs:
        return False
    if source["source_kind"] == "single" and (
        row.parent_task_key != execution.task_key or row.parent_execution_key != execution.execution_key
        or _utc(row.parent_queued_at) != _utc(execution.queued_at) or row.input_hash != execution.input_hash
    ):
        return False
    return True


async def enqueue_memory_for_analysis(
    db: AsyncSession, execution: "analysis_service.AnalysisExecution", *, attempt: AnalysisIndexAttempt,
    reference: "analysis_service.AnalysisReference | None" = None, cipher: SecretCipher | None = None,
    allow_private: bool = False,
) -> AnalysisIndexAdmission:
    """锁内验证原完成收据，同事务记录去重证据、子 Task 与统计；不 commit。"""
    from app.services import analysis_service, task_service

    if (not _BOUND_MEMORY_READY or type(attempt) is not AnalysisIndexAttempt
            or not _canonical_uuid(attempt.receipt_key) or not _canonical_uuid(attempt.child_task_key)
            or UUID(attempt.receipt_key).version != 4 or UUID(attempt.child_task_key).version != 4
            or attempt.receipt_key == attempt.child_task_key
            or execution.task_kind not in (analysis_service.KIND, "chapter.batch")):
        # 单章分析与批量分析阶段都只凭原执行的完成收据受理；批量另须原分析 reference。
        raise analysis_service.AnalysisExecutionChanged()
    source = await _verified_analysis_index_source(
        db, execution, reference, cipher=cipher, allow_private=allow_private,
    )
    row = await db.scalar(select(AnalysisIndexReceipt).where(
        AnalysisIndexReceipt.source_kind == source["source_kind"],
        AnalysisIndexReceipt.source_key == source["source_key"],
    ).with_for_update().execution_options(populate_existing=True))
    if row is not None:
        if not _matching_analysis_index_receipt(row, execution, source):
            raise analysis_service.AnalysisExecutionChanged()
        return AnalysisIndexAdmission("existing", row.receipt_key, row.child_task_key, None, row.source_kind,
                                      row.source_key, row.selection_hash, row.memory_hash)
    refs, has_empty = await _analysis_index_memory_refs(db, execution, source)
    if db.new or db.dirty or db.deleted:
        raise analysis_service.AnalysisExecutionChanged()
    outcome = "queued" if execution.embedding_selection.mode == "vector" and has_empty else "no_work"
    memory_hash = retrieval_service._canonical_digest(refs)
    grant = execution.grant_identity if execution.grant_identity is not None else (None, None)
    async with db.begin_nested():
        row = AnalysisIndexReceipt(
            receipt_key=attempt.receipt_key, source_kind=source["source_kind"], source_key=source["source_key"],
            source_task_key=source["source_task_key"], parent_task_key=execution.task_key,
            parent_execution_key=execution.execution_key, parent_queued_at=execution.queued_at,
            actor_id=execution.actor_id, actor_token_version=execution.actor_token_version,
            project_id=execution.project_id, chapter_id=source["chapter_id"], analysis_id=source["analysis_id"],
            grant_invitation_id=grant[0], grant_revision=grant[1], analysis_hash=source["analysis_hash"],
            content_hash=source["content_hash"], input_hash=execution.input_hash,
            selection_hash=source["selection_hash"],
            memory_refs=refs, memory_hash=memory_hash, outcome=outcome,
            child_task_key=attempt.child_task_key if outcome == "queued" else None,
        )
        db.add(row)
        task_id = None
        if outcome == "queued":
            from app.services import project_ai_config

            child_input = bind_task_grant({
                "request": {"project_id": execution.project_id, "mode": "memory"},
                "_task_actor_token_version": execution.actor_token_version,
                "_shared_embedding": project_ai_config.serialize_shared_embedding(execution.embedding_selection),
                "_memory_ref_ids": [item["ref_id"] for item in refs],
                "_memory_analysis_id": source["analysis_id"], "_analysis_index_receipt": attempt.receipt_key,
            }, execution.grant_identity)
            # 子任务归真实触发者（编辑者时携带原邀请轮次，领取与每批写入按该轮次重验）。
            task = Task(
                statistics_key=attempt.child_task_key, user_id=execution.actor_id, project_id=execution.project_id,
                kind=KIND, title="剧情记忆向量", serial_key=serial_key(execution.project_id), status="queued",
                stage=task_service.STAGE_LABELS["queued"], input=child_input,
            )
            db.add(task)
            await task_statistics_service.record_created(db, task)
            task_id = task.id
        await db.flush()
    return AnalysisIndexAdmission("created" if outcome == "queued" else "no_work", attempt.receipt_key,
                                  row.child_task_key, task_id, row.source_kind, row.source_key,
                                  row.selection_hash, memory_hash)


async def confirm_analysis_index_attempt(
    db: AsyncSession, execution: "analysis_service.AnalysisExecution", *, attempt: AnalysisIndexAttempt,
    admission: AnalysisIndexAdmission, cipher: SecretCipher, allow_private: bool,
    reference: "analysis_service.AnalysisReference | None" = None,
) -> AnalysisIndexAdmission:
    """提交确认未知后只核本次保留的 receipt/child UUID；绝不生成第二次任务。"""
    if (type(attempt) is not AnalysisIndexAttempt or type(admission) is not AnalysisIndexAdmission
            or admission.state not in ("created", "no_work") or admission.receipt_key != attempt.receipt_key
            or (admission.child_task_key != attempt.child_task_key if admission.state == "created"
                else admission.child_task_key is not None)):
        raise AnalysisIndexCommitUncertain()
    try:
        source = await _verified_analysis_index_source(
            db, execution, reference, cipher=cipher, allow_private=allow_private,
        )
        row = await db.scalar(select(AnalysisIndexReceipt).where(
            AnalysisIndexReceipt.receipt_key == attempt.receipt_key,
            AnalysisIndexReceipt.child_task_key == admission.child_task_key,
        ).with_for_update().execution_options(populate_existing=True))
        if (row is None or not _matching_analysis_index_receipt(row, execution, source)
                or row.source_kind != admission.source_kind or row.source_key != admission.source_key
                or row.selection_hash != admission.selection_hash or row.memory_hash != admission.memory_hash):
            raise AnalysisIndexCommitUncertain()
        task_id = None
        if row.child_task_key is not None:
            task_id = await db.scalar(select(Task.id).where(
                Task.statistics_key == row.child_task_key, Task.user_id == execution.actor_id,
                Task.project_id == execution.project_id, Task.kind == KIND,
            ))
        return AnalysisIndexAdmission(admission.state, row.receipt_key, row.child_task_key, task_id,
                                      row.source_kind, row.source_key, row.selection_hash, row.memory_hash)
    except Exception:
        await db.rollback()
    except BaseException:
        await db.rollback()
        raise
    raise AnalysisIndexCommitUncertain() from None


async def enqueue_memory(
    sessionmaker: async_sessionmaker[AsyncSession], *, actor_id: int, project_id: int,
    authenticated_actor: authz.AuthenticatedActor,
) -> int | None:
    """人工 HTTP 写入后的派生：必须携带原认证快照，在新事务再次锁内验证作者。"""
    if (type(actor_id) is not int or actor_id <= 0 or type(project_id) is not int or project_id <= 0
            or type(authenticated_actor) is not authz.AuthenticatedActor or authenticated_actor.id != actor_id):
        raise UnauthorizedError("未登录或登录已过期")
    async with sessionmaker() as db:
        db.info["authenticated_actor"] = authenticated_actor
        with db.no_autoflush:
            project = await project_access.require_project(
                db, actor_id, project_id, "content.edit", for_update=True, visibility_write=True,
            )
            if project.user_id != actor_id:
                raise NotFoundError("项目不存在")
        task_id = await _enqueue_memory_locked(
            db, actor_id=actor_id, project_id=project_id, actor_token_version=authenticated_actor.token_version,
        )
        await db.commit()
        return task_id


# ---------------- 组装 / 运行 ----------------


async def build_index_job(
    db: AsyncSession,
    *,
    task: Task,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    execution: "TaskExecutionIdentity | None" = None,
) -> IndexJob:
    from app.services.task_engine.execution_identity import matches_running

    request = dict((task.input or {}).get("request") or {})
    project_id = int(request["project_id"])
    if execution is not None and not matches_running(execution, task):
        raise _claim_changed()
    payload = task.input if isinstance(task.input, dict) else {}
    if any(key in payload for key in _ANALYSIS_MEMORY_KEYS) or (
        "_shared_embedding" in payload and GRANT_KEY not in payload
    ):
        # 分析派生的绑定记忆（作者或授权编辑者，带不可变收据）。
        return await _build_bound_memory_job(
            db, task=task, request=request, project_id=project_id, cipher=cipher, factory=factory,
            ai_timeout=ai_timeout, allow_private=allow_private, execution=execution,
        )
    if GRANT_KEY in payload:
        # 协作者任务：以原执行身份为准，向量选择恒为受理时冻结的作者配置（E6-a）。
        return await _build_collaborator_index_job(
            db, task=task, request=request, project_id=project_id, cipher=cipher, factory=factory,
            ai_timeout=ai_timeout, allow_private=allow_private, execution=execution,
        )
    # 无协作授权轮次的任务只能是作者本人的执行；不以 Task 主人推断作品作者。
    # 受控发送与每批写入都按原领取身份与原令牌重验（E6-b，裁决 6），缺任一即失败关闭。
    if (execution is None or execution.grant_identity is not None or execution.actor_id != task.user_id
            or type(execution.actor_token_version) is not int or execution.kind != KIND
            or execution.project_id != project_id):
        raise _claim_changed()
    project = await db.scalar(select(Project).where(Project.id == project_id, *project_lifecycle.active_conditions()))
    if project is None or project.user_id != execution.actor_id:
        raise NotFoundError("项目不存在")
    mode: Mode = request.get("mode", "document")
    document_id = int(request["document_id"]) if mode == "document" else None
    if document_id is not None:
        claim = running_document_claim(task)
        if claim is None or not await document_claim_is_current(db, claim):
            raise _claim_changed()
        doc = await db.scalar(
            select(KbDocument).where(KbDocument.id == document_id, KbDocument.project_id == project_id)
        )
        if doc is None:
            raise NotFoundError("文档不存在")
    # 作者任务 = 作者（作品所有者）当前默认向量配置（运行时解析、无配置或不可用时降级为关键词）。
    # resolve_embedder 只作可用性探测（解密、端点）以保留原降级提示；其 provider 不用于发送：
    # 真实发送一律经受控 guard，由构建期绑定的作者配置身份逐次重验（E6-b）。
    embedder, embedder_error = await retrieval_service.resolve_embedder(
        db, project.user_id, cipher=cipher, factory=factory, ai_timeout=ai_timeout, allow_private=allow_private
    )
    selection = await _author_index_selection(db, project) if embedder is not None else None
    if mode == "rebuild" and selection is None:
        raise BadRequestError(f"向量模型不可用：{embedder_error}" if embedder_error else NO_EMBEDDING_REBUILD)
    return IndexJob(
        mode=mode,
        project_id=project_id,
        document_id=document_id,
        embedder=None,
        task_id=task.id,
        document_claim=claim if document_id is not None else None,
        embedder_error=embedder_error,
        task_execution_identity=execution,
        selection=selection,
        cipher=cipher,
        factory=factory,
        ai_timeout=ai_timeout,
        allow_private=allow_private,
        actor_id=project.user_id,
        project_owner_id=project.user_id,
    )


async def _build_bound_memory_job(
    db: AsyncSession, *, task: Task, request: dict[str, Any], project_id: int, cipher: SecretCipher,
    factory: ProviderFactory, ai_timeout: float, allow_private: bool,
    execution: "TaskExecutionIdentity | None",
) -> IndexJob:
    """分析派生记忆：作者无 grant；授权编辑者（E6-c）以原邀请轮次与受理时冻结的作者向量选择执行。"""
    from app.services import project_ai_config
    from app.services.task_engine.execution_identity import require_write_execution

    payload = task.input if isinstance(task.input, dict) else {}
    grant = task_grant_identity(payload)
    if (execution is None or execution.grant_identity != grant or execution.actor_id != task.user_id
            or execution.project_id != project_id or request.get("mode") != "memory"
            or "_shared_embedding" not in execution.input or MANUAL_MEMORY_KEY in execution.input
            or (GRANT_KEY in payload) != (grant is not None)
            or execution.input.get(GRANT_KEY) != payload.get(GRANT_KEY)
            or type(execution.actor_token_version) is not int):
        raise _claim_changed()
    if grant is None:
        await require_write_execution(db, execution)
        project = await get_owned_project(db, execution.actor_id, project_id)
    else:
        owner_id = await db.scalar(select(Project.user_id).where(
            Project.id == project_id, *project_lifecycle.active_conditions(),
        ))
        if owner_id is None:
            raise NotFoundError("项目不存在")
        if owner_id == execution.actor_id:
            raise _claim_changed()
        actor = authz.AuthenticatedActor(execution.actor_id, execution.actor_token_version)
        await authz.require_authenticated(db, actor, write=True, targets=(owner_id,))
        await require_write_execution(db, execution)
        project = await project_access.require_project(
            db, actor.id, project_id, "ai.use", grant_identity=grant,
        )
    try:
        selection = project_ai_config.deserialize_shared_embedding(
            execution.input["_shared_embedding"], actor_id=execution.actor_id, project_id=project_id,
            project_owner_id=project.user_id,
            task_grant=execution.input.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
        )
    except AppError:
        raise _claim_changed() from None
    if (selection is None or selection.mode != "vector" or selection.model is None
            or selection.grant_identity != grant or selection.model.config_owner_id != project.user_id):
        raise _claim_changed()
    await project_ai_config.revalidate_shared_embedding(
        db, selection, cipher=cipher, allow_private=allow_private, for_update=True,
    )
    stamp = await ai_dispatch.read_visibility_stamp(db, project_id)
    return IndexJob(
        mode="memory", project_id=project_id, document_id=None, embedder=None, task_id=task.id,
        task_execution_identity=execution, selection=selection, cipher=cipher, factory=factory,
        ai_timeout=ai_timeout, allow_private=allow_private, visibility_stamp=stamp,
        actor_id=execution.actor_id, grant_identity=grant, project_owner_id=project.user_id, bound_memory=True,
    )


async def _author_index_selection(
    db: AsyncSession, project: Project,
) -> "project_ai_config.ResolvedSharedEmbedding | None":
    """作者本人默认向量配置的运行时绑定（不写入 Task）；配置已被删除则视为未配置。"""
    from app.services import project_ai_config

    with db.no_autoflush:
        row = await project_ai_config._default_config(db, project.user_id, "embedding")
    if row is None:
        return None
    model = project_ai_config._bind(project, project.user_id, None, row, source="actor")
    selection = project_ai_config.ResolvedSharedEmbedding(
        project_id=project.id, actor_id=project.user_id, project_owner_id=project.user_id, grant_identity=None,
        mode="vector", reason=None, model=model,
    )
    project_ai_config.serialize_shared_embedding(selection)
    if not retrieval_service.legacy_owner_index_selection(selection):
        raise _claim_changed()
    return selection


async def _build_collaborator_index_job(
    db: AsyncSession, *, task: Task, request: dict[str, Any], project_id: int, cipher: SecretCipher,
    factory: ProviderFactory, ai_timeout: float, allow_private: bool,
    execution: "TaskExecutionIdentity | None",
) -> IndexJob:
    """协作者 kb.index：原认证/令牌/邀请轮次/作者模型授权及冻结的作者向量配置须全部仍成立，否则失败关闭。

    不调用 resolve_embedder：既不读取协作者本人的向量模型，也不按作者当前默认配置补签；
    返回的作业不含未受控 embedder，实际发送与每批写入由受控路径（_embed_stale，E6-b）承担。
    """
    from app.services import project_ai_config
    from app.services.task_engine.execution_identity import require_write_execution

    mode = request.get("mode")
    grant = task_grant_identity(task.input)
    memory_refs = manual_memory_refs(execution.input) if execution is not None else None
    if (execution is None or grant is None or execution.grant_identity != grant
            or execution.kind != KIND or execution.actor_id != task.user_id or execution.project_id != project_id
            or type(execution.actor_token_version) is not int or mode not in ("document", "rebuild", "memory")
            or execution.input.get(GRANT_KEY) != task.input.get(GRANT_KEY)
            or "_shared_embedding" not in execution.input
            or execution.input.get("_shared_embedding") != task.input.get("_shared_embedding")
            or any(key in execution.input for key in _ANALYSIS_MEMORY_KEYS)
            # 编辑者记忆任务只能处理受理时绑定的本次条目；文档/重建不得夹带条目绑定。
            or (mode == "memory") != (memory_refs is not None)
            or (mode != "memory" and MANUAL_MEMORY_KEY in execution.input)
            or execution.input.get(MANUAL_MEMORY_KEY) != task.input.get(MANUAL_MEMORY_KEY)):
        raise _claim_changed()
    owner_id = await db.scalar(select(Project.user_id).where(
        Project.id == project_id, *project_lifecycle.active_conditions(),
    ))
    if owner_id is None:
        raise NotFoundError("项目不存在")
    if owner_id == execution.actor_id:
        raise _claim_changed()
    try:
        selection = project_ai_config.deserialize_shared_embedding(
            execution.input["_shared_embedding"], actor_id=execution.actor_id, project_id=project_id,
            project_owner_id=owner_id, task_grant=execution.input[GRANT_KEY],
        )
    except AppError:
        raise _claim_changed() from None
    if (selection is None or selection.mode != "vector" or selection.model is None
            or selection.grant_identity != grant or selection.model.config_owner_id != owner_id):
        raise _claim_changed()
    actor = authz.AuthenticatedActor(execution.actor_id, execution.actor_token_version)
    await authz.require_authenticated(db, actor, write=True, targets=(owner_id,))
    # 原领取、当前 accepted editor 与原邀请轮次、作者模型授权开关在锁内逐项复核。
    await require_write_execution(db, execution)
    await project_ai_config.revalidate_shared_embedding(
        db, selection, cipher=cipher, allow_private=allow_private, for_update=True,
    )
    document_id = int(request["document_id"]) if mode == "document" else None
    claim = None
    if document_id is not None:
        claim = running_document_claim(task)
        if claim is None or claim.actor_id != execution.actor_id or not await document_claim_is_current(db, claim):
            raise _claim_changed()
        doc = await db.scalar(
            select(KbDocument).where(KbDocument.id == document_id, KbDocument.project_id == project_id)
        )
        if doc is None:
            raise NotFoundError("文档不存在")
    stamp = await ai_dispatch.read_visibility_stamp(db, project_id)
    return IndexJob(
        mode=mode, project_id=project_id, document_id=document_id, embedder=None, task_id=task.id,
        document_claim=claim, task_execution_identity=execution, selection=selection, cipher=cipher,
        factory=factory, ai_timeout=ai_timeout, allow_private=allow_private, visibility_stamp=stamp,
        actor_id=execution.actor_id, grant_identity=grant, project_owner_id=owner_id, memory_ref_ids=memory_refs,
    )


async def _ensure_chunks(db: AsyncSession, doc: KbDocument) -> int:
    """文档还没有分块时从全文分块并写入检索单元。返回分块数。"""
    existing = await db.scalar(select(KbChunk.id).where(KbChunk.document_id == doc.id).limit(1))
    if existing is not None:
        return doc.chunk_count
    chunks = await anyio.to_thread.run_sync(kb_parser.chunk_text, doc.text)
    rows = [
        KbChunk(
            document_id=doc.id,
            seq=c.seq,
            heading_path=c.heading_path,
            content=c.content,
            start_offset=c.start_offset,
            end_offset=c.end_offset,
        )
        for c in chunks
    ]
    async with db.begin_nested():
        db.add_all(rows)
        await db.flush()
        await retrieval_service.require_unindexed_sources(db, "kb", [r.id for r in rows])
        await retrieval_service.upsert_project_sources(
            db,
            doc.project_id,
            "kb",
            [
                ChunkInput(ref_id=r.id, text=kb_parser.retrieval_text(r.heading_path, r.content), enabled=doc.inject)
                for r in rows
            ],
        )
        doc.chunk_count = len(rows)
    return len(rows)


async def chunk_document(db: AsyncSession, doc: KbDocument) -> int:
    """从文档全文分块并写入关键词检索单元（不 commit）。项目备份导入时在同一事务里调用。"""
    return await _ensure_chunks(db, doc)


# 受控批次：来源或可见性代次在单批期间变化（鉴权仍成立）时允许重新快照的次数上限。
_MAX_SOURCE_RETRIES = 3
_POLICY_STOP = "建索引已停止：授权、模型配置或索引状态已变化，请重新索引"
_SOURCE_CHANGED = "部分资料在建索引期间发生变化，未完成的部分暂时只能关键词检索，可稍后重建索引"
_STILL_STALE = "部分分块仍缺少当前向量模型的向量，暂时只能关键词检索，请重新索引"


def _require_job_identity(job: IndexJob) -> None:
    """作业自身各身份字段须与领取时冻结的执行身份一致；不从当前数据库补签。"""
    from app.services import project_ai_config
    from app.services.task_engine.execution_identity import TaskExecutionIdentity

    execution, selection, claim = job.task_execution_identity, job.selection, job.document_claim
    if (type(execution) is not TaskExecutionIdentity or execution.kind != KIND
            or execution.project_id != job.project_id or execution.actor_id != job.actor_id
            or execution.grant_identity != job.grant_identity
            or type(execution.actor_token_version) is not int or execution.actor_token_version < 0
            or type(job.project_owner_id) is not int or job.project_owner_id <= 0
            or (job.grant_identity is None) != (job.actor_id == job.project_owner_id)
            or (job.grant_identity is not None and selection is None)
            or (job.mode == "document") != (claim is not None)
            or (job.memory_ref_ids is not None and (
                job.mode != "memory" or job.grant_identity is None
                or job.memory_ref_ids != manual_memory_refs(execution.input)))
            or (job.mode == "memory" and job.grant_identity is not None and job.memory_ref_ids is None)):
        raise _claim_changed()
    if selection is not None and (
        type(selection) is not project_ai_config.ResolvedSharedEmbedding or selection.mode != "vector"
        or selection.model is None
        or (selection.actor_id, selection.project_id, selection.project_owner_id, selection.grant_identity)
        != (job.actor_id, job.project_id, job.project_owner_id, job.grant_identity)
        or selection.model.config_owner_id != job.project_owner_id
        or job.cipher is None or job.factory is None
    ):
        raise _claim_changed()
    if claim is not None and (
        claim.task_id != execution.task_id or claim.task_key != execution.task_key
        or claim.actor_id != execution.actor_id or claim.claim_key != execution.execution_key
        or claim.project_id != job.project_id or claim.document_id != job.document_id
    ):
        raise _claim_changed()


async def _require_index_write(db: AsyncSession, job: IndexJob) -> None:
    """文档状态、分块与向量写入前在同一根写事务内重验（E4-h）：原 token 与作者屏障、原领取执行、
    协作者原邀请轮次与作者模型授权、文档占用、冻结/构建期的向量配置；锁保持至提交。"""
    from app.services import project_ai_config
    from app.services.task_engine.execution_identity import require_write_execution

    try:
        _require_job_identity(job)
        if db.get_nested_transaction() is not None or db.new or db.dirty or db.deleted:
            raise _claim_changed()
        execution = job.task_execution_identity
        assert execution is not None and execution.actor_token_version is not None
        actor = authz.AuthenticatedActor(execution.actor_id, execution.actor_token_version)
        await authz.require_authenticated(db, actor, write=True, targets=(job.project_owner_id,))
        await require_write_execution(db, execution)
        if job.document_claim is not None:
            await require_writing_document_claim(db, job.document_claim, actor=actor, grant_identity=job.grant_identity)
        with db.no_autoflush:
            project = await project_access.require_project(
                db, actor.id, job.project_id, "ai.use", for_update=True, visibility_write=True,
                grant_identity=job.grant_identity,
            )
        if project.user_id != job.project_owner_id:
            raise _claim_changed()
        if job.selection is not None:
            await project_ai_config.revalidate_shared_embedding(
                db, job.selection, cipher=job.cipher, allow_private=job.allow_private, for_update=True,
            )
    except BaseException:
        await db.rollback()
        raise


async def require_index_event(db: AsyncSession, job: IndexJob, *, event: str) -> None:
    """引擎事件/刷新核验（调用前已在本事务按原执行身份锁 actor、门禁、作品与 Task）。

    进度帧还须证明文档占用仍属本轮、构建期向量配置仍有效；done 帧在本任务自己的收尾写事务
    （已完整重验并释放占用）之后产出，只需原执行身份仍成立。
    """
    from app.services import project_ai_config

    _require_job_identity(job)
    if event == "done":
        return
    if job.document_claim is not None:
        await _lock_document_claim_rows(db, job.document_claim, write=True)
    if job.selection is not None:
        await project_ai_config.revalidate_shared_embedding(
            db, job.selection, cipher=job.cipher, allow_private=job.allow_private, for_update=True,
        )


async def require_index_finalization(db: AsyncSession, job: IndexJob) -> None:
    """成功收尾前按原执行身份重验认证、授权轮次与作者模型授权；内容已在受控写事务中提交。"""
    from app.services.task_engine.execution_identity import require_write_execution

    _require_job_identity(job)
    assert job.task_execution_identity is not None
    await require_write_execution(db, job.task_execution_identity)


async def _require_index_alive(sessionmaker: async_sessionmaker[AsyncSession], job: IndexJob) -> None:
    """单批失败后区分「来源/可见性代次变化」与「授权或执行失效」：后者一律失败关闭，不降级为关键词。"""
    try:
        async with sessionmaker() as db:
            await _require_index_write(db, job)
            await db.rollback()
    except ai_dispatch.VisibilityBusyError:
        # 写门禁一时繁忙不是授权失效：本批未写入任何内容，重试时发送与写入仍各自完整重验。
        return
    except Exception:  # noqa: BLE001  不传播 SQL/配置细节。
        raise retrieval_service.EmbeddingPolicyError from None


def _legacy_model_key(config: retrieval_service.BoundSharedEmbeddingConfig) -> str:
    # 裁决 1：作者与授权协作者都写作者现用旧键 provider:model:dim。
    return f"{config.provider}:{config.model}"


def _stale_statement(job: IndexJob, model_key: str, source: str | None, ref_ids: list[int] | None):
    if job.mode == "rebuild" or (job.mode == "document" and ref_ids is not None and source == "kb"):
        # 裁决 12：文档模式与重建一样替换旧模型向量，但范围仅限本次索引文档的分块（ref_ids）。
        condition = retrieval_service._stale_condition(model_key)
    else:
        # 记忆模式（作者旧记忆 / 编辑者本次条目）只补空向量，不改写其他条目或其他空间。
        condition = RetrievalChunk.embedding.is_(None) & RetrievalChunk.embedding_key.is_(None)
    statement = select(RetrievalChunk.id).where(RetrievalChunk.project_id == job.project_id, condition)
    if source is not None:
        statement = statement.where(RetrievalChunk.source == source)
    if ref_ids is not None:
        statement = statement.where(RetrievalChunk.ref_id.in_(ref_ids))
    return statement


async def _embed_stale(
    sessionmaker: async_sessionmaker[AsyncSession],
    job: IndexJob,
    *,
    source: retrieval_service.Source | None,
    ref_ids: list[int] | None,
) -> AsyncIterator[tuple[str, dict]]:
    """分批为缺少当前模型向量的检索单元生成向量（E6-b / E4-h），每批：

    1. 短事务快照（检索行 + 业务来源 + 可见性戳），立即结束事务；
    2. 受控 guard（原执行、文档占用、本批快照、冻结/构建期作者向量配置）在真实发送租约内重验后发送；
    3. 新写事务 `guard.require_task_write` + 快照批量 CAS，只写快照时仍匹配的行，提交。

    普通向量服务故障抛 AIProviderError（调用方降级为关键词）；授权/策略失效抛 EmbeddingPolicyError（失败关闭）。
    """
    from app.services import collaboration_ai_guard

    _require_job_identity(job)
    execution, selection = job.task_execution_identity, job.selection
    if selection is None or job.cipher is None or job.factory is None or execution is None:
        raise _claim_changed()
    async with sessionmaker() as db:
        first = await retrieval_service.load_bound_shared_embedding_config(
            db, selection, cipher=job.cipher, allow_private=job.allow_private,
        )
        model_key = _legacy_model_key(first)
        total = int(await db.scalar(select(func.count()).select_from(
            _stale_statement(job, model_key, source, ref_ids).subquery(),
        )) or 0)
        await db.rollback()
    done = retries = 0
    for _ in range(total // retrieval_service.EMBED_BATCH + 2 + _MAX_SOURCE_RETRIES):
        async with sessionmaker() as db:
            config = await retrieval_service.load_bound_shared_embedding_config(
                db, selection, cipher=job.cipher, allow_private=job.allow_private,
            )
            if _legacy_model_key(config) != model_key:
                raise retrieval_service.EmbeddingPolicyError
            ids = list(await db.scalars(_stale_statement(job, model_key, source, ref_ids)
                                        .order_by(RetrievalChunk.id).limit(retrieval_service.EMBED_BATCH)))
            if not ids:
                return
            try:
                snapshots = await retrieval_service.snapshot_embedding_inputs(
                    db, job.project_id, ids, allow_orphans=True,
                )
            except ConflictError as exc:
                # PostgreSQL 读已提交下检索行与业务来源分两次读取，并发修改可让二者暂时不一致；
                # 与写时快照不符同样处理：本批不发送、不写入，鉴权仍成立才重新快照（失效则失败关闭）。
                if (exc.extra or {}).get("code") != "retrieval_reference_invalid":
                    raise
                snapshots = None
            else:
                stamp = await ai_dispatch.read_visibility_stamp(db, job.project_id)
            await db.rollback()
        if snapshots is None:
            await _require_index_alive(sessionmaker, job)
            retries += 1
            if retries > _MAX_SOURCE_RETRIES:
                job.incomplete = True
                job.warnings.append(_SOURCE_CHANGED)
                return
            continue
        # 内部关键词索引会附加从设定库解析出的角色名；快照只把记忆正文作为向量输入。
        orphans = [item for item in snapshots if item.embed_text is None]
        live = [item for item in snapshots if item.embed_text is not None]
        if orphans:
            async with sessionmaker() as db:
                await _require_index_write(db, job)
                for item in orphans:
                    await retrieval_service.delete_orphan_snapshot(db, item)
                await db.commit()
        stored = True
        if live:
            unique: list[retrieval_service.EmbeddingInputSnapshot] = []
            texts: list[str] = []
            for item in live:
                text = item.embed_text[:retrieval_service.EMBED_TEXT_CHARS]
                if text not in texts:
                    texts.append(text)
                    unique.append(item)
            guard = collaboration_ai_guard.for_task(
                execution, selection, stamp, cipher=job.cipher, allow_private=job.allow_private,
                document_claim=job.document_claim, embedding_inputs=tuple(unique),
            )
            stored = False
            try:
                bound = await guard.prepare_provider(sessionmaker, job.factory, request_timeout=job.ai_timeout)
                raw = await bound.provider.embed(list(texts), bound.model)
            except AIProviderError as exc:
                if retrieval_service._policy_cause(exc) is None:
                    raise  # 普通故障：不重发不确定的请求，由调用方降级为关键词
            except AppError:
                pass
            else:
                if len(raw) != len(texts):
                    raise AIProviderError("模型调用失败：返回的向量数量与请求不符")
                by_text = {}
                for text, values in zip(texts, raw, strict=True):
                    vector = retrieval_service.normalize_vector(values)
                    if vector is None:
                        raise AIProviderError("模型调用失败：返回了空向量")
                    by_text[text] = vector
                if len({int(vector.size) for vector in by_text.values()}) != 1:
                    raise AIProviderError("模型调用失败：向量维度不一致")
                vectors = [by_text[item.embed_text[:retrieval_service.EMBED_TEXT_CHARS]] for item in live]
                async with sessionmaker() as db:
                    try:
                        await guard.require_task_write(db)
                        stored = await retrieval_service.store_index_legacy_embedding_snapshots(
                            db, live, vectors, config, execution=execution,
                        )
                    except AppError:
                        stored = False
                    if stored:
                        await db.commit()
                    else:
                        await db.rollback()
        if not stored:
            # 不写任何向量；授权/执行/占用/配置失效即失败关闭，否则视为本批来源或可见性代次变化，重新快照。
            await _require_index_alive(sessionmaker, job)
            retries += 1
            if retries > _MAX_SOURCE_RETRIES:
                job.incomplete = True
                job.warnings.append(_SOURCE_CHANGED)
                return
            continue
        done += len(snapshots)
        yield "delta", {"text": f"生成向量 {min(done, max(total, done))}/{max(total, done)}\n"}
    job.incomplete = True
    job.warnings.append(_SOURCE_CHANGED)


async def require_bound_index_job(db: AsyncSession, job: IndexJob) -> None:
    """原领取和模型绑定在每个短事务内重验；不从新 Task 或当前默认模型补签。"""
    from app.services import project_ai_config
    from app.services.task_engine.execution_identity import TaskExecutionIdentity, require_write_execution

    execution, selection = job.task_execution_identity, job.selection
    if (type(execution) is not TaskExecutionIdentity or type(selection) is not project_ai_config.ResolvedSharedEmbedding
            or job.cipher is None or job.visibility_stamp is None or execution.kind != KIND
            or execution.project_id != job.project_id or execution.actor_id != selection.actor_id
            or selection.project_id != job.project_id
            # 作者 ⇔ 无 grant；授权编辑者（E6-c）⇔ 原邀请轮次，与冻结选择逐项一致。
            or execution.grant_identity != selection.grant_identity or job.grant_identity != execution.grant_identity
            or (execution.grant_identity is None) != (selection.project_owner_id == execution.actor_id)
            or selection.model is None or selection.model.config_owner_id != selection.project_owner_id
            or job.mode != "memory" or selection.mode != "vector" or job.document_id is not None
            or type(execution.actor_token_version) is not int or db.new or db.dirty or db.deleted):
        raise _claim_changed()
    original = execution.input
    if (original.get("_shared_embedding") != project_ai_config.serialize_shared_embedding(selection)
            or not isinstance(original.get("request"), dict) or original["request"].get("mode") != "memory"
            or type(original.get("_memory_analysis_id")) is not int or original["_memory_analysis_id"] <= 0
            or type(original.get("_memory_ref_ids")) is not list or not original["_memory_ref_ids"]
            or len(original["_memory_ref_ids"]) > 10000
            or any(type(value) is not int or value <= 0 for value in original["_memory_ref_ids"])
            or len(set(original["_memory_ref_ids"])) != len(original["_memory_ref_ids"])):
        raise _claim_changed()
    actor = authz.AuthenticatedActor(execution.actor_id, execution.actor_token_version)
    await authz.require_authenticated(db, actor, write=True, targets=(selection.project_owner_id,))
    await require_write_execution(db, execution)
    await ai_dispatch.require_visibility_stamp(db, job.visibility_stamp)
    await project_ai_config.revalidate_shared_embedding(
        db, selection, cipher=job.cipher, allow_private=job.allow_private, for_update=True,
    )
    await retrieval_service.require_analysis_index_receipt(db, execution, selection)


async def _bound_memory_inputs(
    db: AsyncSession, job: IndexJob,
) -> tuple[tuple[retrieval_service.EmbeddingInputSnapshot, ...], retrieval_service.BoundSharedEmbeddingConfig]:
    """只选原完成分析产生的未索引记忆；先冻结本批实际正文，事务外才能发送。"""
    await require_bound_index_job(db, job)
    execution, selection = job.task_execution_identity, job.selection
    assert execution is not None and selection is not None and job.cipher is not None
    original = execution.input
    refs, analysis_id = original["_memory_ref_ids"], original["_memory_analysis_id"]
    candidates = []
    for batch in batched(refs, 400):
        memories = (await db.execute(select(StoryMemory.id, StoryMemory.analysis_id).where(
            StoryMemory.project_id == job.project_id, StoryMemory.id.in_(batch),
        ))).all()
        if {row.id for row in memories} != set(batch) or any(row.analysis_id != analysis_id for row in memories):
            raise retrieval_service.EmbeddingPolicyError
        rows = (await db.execute(select(
            RetrievalChunk.id, RetrievalChunk.ref_id, RetrievalChunk.embedding, RetrievalChunk.embedding_key,
        ).where(RetrievalChunk.project_id == job.project_id, RetrievalChunk.source == "memory",
                RetrievalChunk.ref_id.in_(batch)).order_by(RetrievalChunk.id))).all()
        if {row.ref_id for row in rows} != set(batch):
            raise retrieval_service.EmbeddingPolicyError
        for row in rows:
            if (row.embedding is None) != (row.embedding_key is None):
                raise retrieval_service.EmbeddingPolicyError
            if row.embedding is None:
                candidates.append(row.id)
    config = await retrieval_service.load_bound_shared_embedding_config(
        db, selection, cipher=job.cipher, allow_private=job.allow_private, for_update=True,
    )
    if not candidates:
        return (), config
    snapshots = await retrieval_service.snapshot_embedding_inputs(db, job.project_id,
                                                                  candidates[:retrieval_service.EMBED_BATCH])
    unique, texts = [], set()
    for snapshot in snapshots:
        if snapshot.embed_text is None:
            raise retrieval_service.EmbeddingPolicyError
        text = snapshot.embed_text[:retrieval_service.EMBED_TEXT_CHARS]
        if text not in texts:
            texts.add(text)
            unique.append(snapshot)
    return tuple(unique), config


async def _run_bound_memory(job: IndexJob, sessionmaker) -> AsyncIterator[tuple[str, dict]]:
    from app.services import collaboration_ai_guard

    execution, selection = job.task_execution_identity, job.selection
    if (execution is None or selection is None or job.cipher is None or job.factory is None
            or job.visibility_stamp is None):
        raise _claim_changed()
    done = batches = 0
    while True:
        async with sessionmaker() as db:
            snapshots, config = await _bound_memory_inputs(db, job)
            await db.rollback()
        if not snapshots:
            break
        guard = collaboration_ai_guard.for_task(
            execution, selection, job.visibility_stamp, cipher=job.cipher, allow_private=job.allow_private,
            embedding_inputs=snapshots,
        )
        bound = await guard.prepare_provider(sessionmaker, job.factory, request_timeout=job.ai_timeout)
        texts = [s.embed_text[:retrieval_service.EMBED_TEXT_CHARS] for s in snapshots if s.embed_text is not None]
        try:
            vectors = await bound.provider.embed(texts, bound.model)
        except AIProviderError as exc:
            if retrieval_service._policy_cause(exc) is not None:
                raise retrieval_service.EmbeddingPolicyError from None
            # 不重发不确定的请求，保持未完成向量待人工处理。
            job.warnings.append("向量生成失败，未完成的部分暂时只能关键词检索")
            break
        async with sessionmaker() as db:
            await guard.require_task_write(db)
            if not await retrieval_service.store_owner_legacy_embedding_snapshots(
                db, snapshots, vectors, config, execution=execution,
            ):
                raise retrieval_service.EmbeddingPolicyError
            await db.commit()
        done += len(snapshots)
        batches += 1
        yield "delta", {"text": f"生成向量 {done}/{len(execution.input['_memory_ref_ids'])}\n"}
    yield "done", {"mode": "memory", "project_id": job.project_id, "batches": batches, "warnings": job.warnings}


async def run_index_job(
    job: IndexJob, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[tuple[str, dict]]:
    try:
        if job.bound_memory:
            async for event in _run_bound_memory(job, sessionmaker):
                yield event
            return
        # 作者与协作者的文档/重建/旧记忆任务共用受控路径（E6-b）；作业身份不一致即失败关闭，零发送。
        _require_job_identity(job)
        if job.mode == "document":
            async for event in _run_document(job, sessionmaker):
                yield event
        else:
            async for event in _run_vectors(job, sessionmaker):
                yield event
    except AppError:
        logger.error("建索引任务因授权或状态变化停止（项目 %s）", job.project_id)
        yield "error", {"detail": _POLICY_STOP}
    except Exception:  # noqa: BLE001
        logger.error("建索引任务失败（项目 %s）", job.project_id)
        yield "error", {"detail": "建索引失败：服务器内部错误"}


async def _run_document(job: IndexJob, sessionmaker) -> AsyncIterator[tuple[str, dict]]:
    assert job.document_id is not None
    if job.document_claim is None:
        raise _claim_changed()
    async with sessionmaker() as db:
        await _require_index_write(db, job)
        doc = await _lock_running_document(db, job.document_claim)
        doc.status, doc.error = "indexing", ""
        count = await _ensure_chunks(db, doc)
        await db.commit()
        warnings = list(doc.warnings or [])
    yield "delta", {"text": f"已分块：{count} 块\n"}
    status = "ready"
    if count == 0:
        warnings.append("文档没有可分块的正文")
    elif job.selection is None:
        status = "ready_keyword_only"
        job.warnings.append(job.no_embedding_warning())
    else:
        async with sessionmaker() as db:
            ref_ids = list(await db.scalars(select(KbChunk.id).where(KbChunk.document_id == job.document_id)))
            await db.rollback()
        try:
            async for event in _embed_stale(sessionmaker, job, source="kb", ref_ids=ref_ids):
                yield event
            if job.incomplete:
                status = "ready_keyword_only"
        except AIProviderError as exc:
            status = "ready_keyword_only"
            job.warnings.append(f"向量生成失败（{exc}），该文档暂时只能关键词检索，可稍后重建索引")
    async with sessionmaker() as db:
        await _require_index_write(db, job)
        doc = await _lock_running_document(db, job.document_claim)
        if status == "ready" and job.selection is not None and job.cipher is not None:
            # 文档模式已替换本文档内的旧模型向量（裁决 12）；收尾仍按当前作者旧键复核本文档分块，
            # 真正缺当前模型向量的（如替换后又被并发清空）不能标为 ready。
            config = await retrieval_service.load_bound_shared_embedding_config(
                db, job.selection, cipher=job.cipher, allow_private=job.allow_private, for_update=True,
            )
            ref_ids = list(await db.scalars(select(KbChunk.id).where(KbChunk.document_id == job.document_id)))
            if ref_ids and await retrieval_service.stale_chunks(
                db, job.project_id, _legacy_model_key(config), source="kb", ref_ids=ref_ids, limit=1,
            ):
                status = "ready_keyword_only"
                job.warnings.append(_STILL_STALE)
        doc.status, doc.error = status, ""
        doc.warnings = list(dict.fromkeys([*warnings, *job.warnings]))[:20]
        doc.index_task_key = doc.index_task_queued_at = doc.index_claim_key = None
        await db.commit()
        chunk_count = doc.chunk_count
    yield (
        "done",
        {
            "mode": "document",
            "document_id": job.document_id,
            "chunk_count": chunk_count,
            "status": status,
            "warnings": job.warnings,
        },
    )


async def _run_vectors(job: IndexJob, sessionmaker) -> AsyncIterator[tuple[str, dict]]:
    source: retrieval_service.Source | None = "memory" if job.mode == "memory" else None
    embedded = 0
    if job.selection is None:
        job.warnings.append(job.no_embedding_warning())
    else:
        try:
            async for event in _embed_stale(sessionmaker, job, source=source, ref_ids=job.memory_ref_ids):
                embedded += 1
                yield event
        except AIProviderError as exc:
            job.warnings.append(f"向量生成失败（{exc}），未完成的部分暂时只能关键词检索")
    if job.mode == "rebuild":
        await _refresh_doc_statuses(sessionmaker, job)
    yield "done", {"mode": job.mode, "project_id": job.project_id, "batches": embedded, "warnings": job.warnings}


async def _refresh_doc_statuses(sessionmaker, job: IndexJob) -> None:
    """重建后：文档的分块都有当前模型向量 → ready，否则 ready_keyword_only（建索引中 / 失败的不动）。

    在同一写事务重验原认证、执行、授权与向量配置后才改状态（E4-h）。
    """
    if job.selection is None or job.cipher is None:
        return
    async with sessionmaker() as db:
        await _require_index_write(db, job)
        config = await retrieval_service.load_bound_shared_embedding_config(
            db, job.selection, cipher=job.cipher, allow_private=job.allow_private, for_update=True,
        )
        model_key = _legacy_model_key(config)
        docs = list(
            await db.scalars(
                select(KbDocument).where(
                    KbDocument.project_id == job.project_id, KbDocument.status.in_(("ready", "ready_keyword_only"))
                ).order_by(KbDocument.id).with_for_update()
            )
        )
        for doc in docs:
            ids = list(await db.scalars(select(KbChunk.id).where(KbChunk.document_id == doc.id)))
            stale = ids and await retrieval_service.stale_chunks(
                db, job.project_id, model_key, source="kb", ref_ids=ids, limit=1
            )
            doc.status = "ready_keyword_only" if stale else "ready"
        await db.commit()


def progress(text: str) -> int:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return 5
    m = _PROGRESS_RE.search(lines[-1])
    if m is None:
        return 10
    done, total = int(m.group(1)), int(m.group(2))
    return min(95, 10 + int(85 * done / total)) if total else 10


async def on_abort(sessionmaker: async_sessionmaker[AsyncSession], task: Task) -> None:
    """文档任务未成功：文档状态置为 failed（仍在 pending / indexing 时），可重试。"""
    request = (task.input or {}).get("request") or {}
    if request.get("mode") != "document" or request.get("document_id") is None:
        return
    try:
        async with sessionmaker() as db:
            if task.project_id is None:
                return
            await project_lifecycle.lock_record(db, task.project_id)
            claim = (running_document_claim(task) if task.execution_key is not None
                     else await queued_document_claim(db, task))
            if claim is not None:
                await release_document_claim(db, claim)
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.error("建索引任务回退文档状态失败 task=%s", task.id)
        raise
