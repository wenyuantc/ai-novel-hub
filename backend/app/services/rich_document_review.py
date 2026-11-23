"""章级富文档提案：来源快照、任务身份和一次执行轮次必须在同一事务中绑定。"""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, insert, select
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.models import (
    Chapter,
    ChapterVersion,
    GenerationLog,
    Project,
    Scene,
    SceneReviewPayloadChunk,
    SceneReviewProposal,
    Task,
)
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.rich_document import SceneDocument
from app.schemas.rich_document_review import ReviewAccept, ReviewIdentity
from app.services import (
    authorization_service,
    chapter_claim,
    project_access,
    project_ai_config,
    scene_ai_context,
    scene_service,
    setting_ai_visibility,
    task_statistics_service,
    version_service,
)
from app.services.ai_dispatch import visibility_sessionmaker
from app.services.chapter_service import commit_versioned
from app.services.collaboration_task_access import task_grant_identity, task_visible_to_actor
from app.services.rich_document_proposal_mapping import (
    ProposalMappingError,
    SceneProposalMapping,
    build_scene_proposal,
    resolve_scene_proposal,
)

if TYPE_CHECKING:
    from app.services.generation_service import ChapterJob
    from app.services.task_engine.engine import TaskEngine

# 单行按字符分块，UTF-8 编码至多约 1 MiB；完整候选没有应用层总量截断。
PROPOSAL_CHUNK_CHARS = 256 * 1024
PROPOSAL_CHUNK_BATCH = 32
REVIEW_AUTO_ORIGIN_UNTRUSTED = "原任务缺少可信执行身份或授权已失效，自动分析未排队，请手动分析"


def _trusted_owner_review_origin(task: Task, original_input: object, actor: object) -> bool:
    """作者采纳自己任务的提案时，派生分析只继承原任务本人及其受理时的令牌轮次。

    原任务缺少统计 UUID、入队时间或受理令牌（升级前的历史任务），或令牌轮次已被撤销时返回 False；
    调用方应跳过派生而非用本次会话或当前数据库值补签。
    """
    if (type(actor) is not authorization_service.AuthenticatedActor or type(task.user_id) is not int
            or actor.id != task.user_id or not isinstance(task.queued_at, datetime)
            or not isinstance(original_input, dict)):
        return False
    try:
        if str(UUID(task.statistics_key)) != task.statistics_key:
            return False
    except (TypeError, ValueError, AttributeError):
        return False
    token = original_input.get("_task_actor_token_version")
    return type(token) is int and token >= 0 and token == actor.token_version


class ReviewStorageError(AppError):
    """数据库错误不能携带正文、提案分块或内部模型绑定。"""

    status_code = 500


class ReviewRecoveryUncertain(ConflictError):
    def __init__(self) -> None:
        super().__init__("待审提案的原执行事实无法确认", extra={"code": "rich_review_task_changed"})


def _same_json(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
            right, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError):
        return False


def _source_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True, repr=False)
class ReviewAutoSource:
    """编辑者提案派生分析的原来源；仅由已持锁的服务端采纳事务创建。"""

    task_id: int
    task_key: str
    actor_id: int
    actor_token_version: int
    project_id: int
    owner_id: int
    chapter_id: int
    queued_at: str
    grant_identity: tuple[str, int]
    proposal_id: int
    proposal_token: str
    run_key: str
    source_revision: int
    applied_revision: int
    log_id: int
    version_id: int
    version_source: str
    input_json: str
    checkpoint_json: str
    proposal_header_json: str
    receipt_json: str
    accepted_decision_json: str


async def require_review_auto_source(
    db: AsyncSession, source: ReviewAutoSource, actor: authorization_service.AuthenticatedActor,
) -> tuple[Project, dict[str, Any], tuple[str, int]]:
    """同一未提交事务复验原提案和已应用正文；不刷新或 flush 调用者暂存对象。"""
    invalid = ConflictError("提案的自动分析来源已变化", extra={"code": "rich_review_task_changed"})
    try:
        if (type(source) is not ReviewAutoSource or type(actor) is not authorization_service.AuthenticatedActor
                or db.info.get("authenticated_actor") != actor or actor.id != source.actor_id
                or type(actor.token_version) is not int or actor.token_version != source.actor_token_version
                or type(source.grant_identity) is not tuple or len(source.grant_identity) != 2
                or source.owner_id == source.actor_id
                or any(type(value) is not int or value <= 0 for value in (
                    source.task_id, source.actor_id, source.project_id, source.owner_id, source.chapter_id,
                    source.proposal_id, source.log_id, source.version_id,
                )) or type(source.source_revision) is not int or type(source.applied_revision) is not int
                or source.applied_revision != source.source_revision + 1):
            raise invalid
        original_input = json.loads(source.input_json)
        original_checkpoint = json.loads(source.checkpoint_json)
        original_header = json.loads(source.proposal_header_json)
        original_receipt = json.loads(source.receipt_json)
        original_decision = json.loads(source.accepted_decision_json)
        if (not isinstance(original_input, dict) or not isinstance(original_checkpoint, dict)
                or not isinstance(original_header, dict) or not isinstance(original_receipt, dict)
                or not isinstance(original_decision, dict)
                or _source_json(original_input) != source.input_json
                or _source_json(original_checkpoint) != source.checkpoint_json
                or _source_json(original_header) != source.proposal_header_json
                or _source_json(original_receipt) != source.receipt_json
                or _source_json(original_decision) != source.accepted_decision_json
                or type(original_input.get("_task_actor_token_version")) is not int
                or original_input["_task_actor_token_version"] != source.actor_token_version
                or chapter_claim._grant(original_input) != source.grant_identity):
            raise invalid
        with db.no_autoflush:
            await authorization_service.require_authenticated(db, actor, write=True, targets=(source.owner_id,))
            project = await project_access.require_project(
                db, actor.id, source.project_id, "ai.use", for_update=True, visibility_write=True,
                grant_identity=source.grant_identity,
            )
            if project.user_id != source.owner_id:
                raise invalid
            task = await db.scalar(select(Task).where(
                Task.id == source.task_id, Task.statistics_key == source.task_key, Task.user_id == source.actor_id,
                Task.project_id == source.project_id, Task.chapter_id == source.chapter_id,
                Task.kind.in_(("chapter.generate", "chapter.polish", "chapter.revise")), Task.status == "paused",
                Task.execution_key.is_(None), Task.admin_cancel_operation_id.is_(None),
                Task.admin_cancel_requested_at.is_(None),
            ).with_for_update())
            chapter = await db.scalar(select(Chapter).where(
                Chapter.id == source.chapter_id, Chapter.project_id == source.project_id,
            ).with_for_update())
            proposal = await db.scalar(select(SceneReviewProposal).where(
                SceneReviewProposal.id == source.proposal_id, SceneReviewProposal.task_id == source.task_id,
                SceneReviewProposal.task_key == source.task_key, SceneReviewProposal.chapter_id == source.chapter_id,
                SceneReviewProposal.token == source.proposal_token, SceneReviewProposal.run_key == source.run_key,
                SceneReviewProposal.status == "pending", SceneReviewProposal.content_revision == source.source_revision,
            ).with_for_update())
            if (task is None or chapter is None or proposal is None or db.is_modified(task, include_collections=False)
                    or db.is_modified(proposal, include_collections=False) or task.queued_at is None
                    or _round_time(task.queued_at) != source.queued_at
                    or _source_json(task.input) != source.input_json
                    or _source_json(task.checkpoint) != source.checkpoint_json
                    or _source_json(proposal.payload) != source.proposal_header_json
                    or chapter.content_revision != source.applied_revision or chapter.status != "done"
                    or any((chapter.generation_claim_key, chapter.generation_claim_task_key,
                            chapter.generation_claim_prev_status))):
                raise invalid
            payload = await _read_payload(db, proposal)
            _require_proposal_round(task, proposal, chapter, payload)
            if (_source_json(payload.get("execution_receipt")) != source.receipt_json
                    or not _same_json(payload.get("_model_binding"), original_input.get("_project_model"))):
                raise invalid
            model = project_ai_config.deserialize_project_model(
                original_input.get("_project_model"), actor_id=source.actor_id, project_id=source.project_id,
                project_owner_id=source.owner_id,
            )
            selection = project_ai_config.deserialize_shared_embedding(
                original_input.get("_shared_embedding"), actor_id=source.actor_id, project_id=source.project_id,
                project_owner_id=source.owner_id, task_grant=original_input.get("_collaboration_grant"),
            )
            if (model is None or selection is None or model.grant_identity != source.grant_identity
                    or selection.grant_identity != source.grant_identity):
                raise invalid
            log = await db.scalar(select(GenerationLog).where(
                GenerationLog.id == source.log_id, GenerationLog.task_id == source.task_id,
                GenerationLog.actor_user_id == source.actor_id, GenerationLog.project_id == source.project_id,
                GenerationLog.chapter_id == source.chapter_id, GenerationLog.kind == source.version_source,
                GenerationLog.status == "success",
            ).with_for_update())
            version = await db.scalar(select(ChapterVersion).where(
                ChapterVersion.id == source.version_id, ChapterVersion.chapter_id == source.chapter_id,
                ChapterVersion.generation_log_id == source.log_id, ChapterVersion.source == source.version_source,
            ).with_for_update())
            if (log is None or version is None or version.content != chapter.content
                    or log.model_source != model.source or log.model_config_owner_id != model.config_owner_id):
                raise invalid
        return project, original_input, source.grant_identity
    except (AttributeError, KeyError, TypeError, ValueError):
        raise invalid from None


def _source_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def serialize_applied_review_source(source: ReviewAutoSource) -> dict[str, Any]:
    """仅保存身份与摘要，不将候选正文或模型私密信息复制到派生任务。"""
    if type(source) is not ReviewAutoSource:
        raise ConflictError("提案自动分析来源无效", extra={"code": "rich_review_task_changed"})
    try:
        checkpoint, receipt = json.loads(source.checkpoint_json), json.loads(source.receipt_json)
        if (not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("rich_review"), dict)
                or checkpoint["rich_review"].get("phase") != "awaiting_review" or not isinstance(receipt, dict)
                or not chapter_claim._uuid(receipt.get("execution_key"))
                or not chapter_claim._uuid(receipt.get("claim_key"))):
            raise ValueError
        checkpoint["rich_review"]["phase"] = "applied"
        return {
            "version": 1, "task_id": source.task_id, "task_key": source.task_key,
            "actor_id": source.actor_id, "actor_token_version": source.actor_token_version,
            "project_id": source.project_id, "owner_id": source.owner_id, "chapter_id": source.chapter_id,
            "queued_at": source.queued_at, "grant": list(source.grant_identity), "proposal_id": source.proposal_id,
            "proposal_token": source.proposal_token, "run_key": source.run_key,
            "execution_key": receipt["execution_key"], "claim_key": receipt["claim_key"],
            "source_revision": source.source_revision, "applied_revision": source.applied_revision,
            "log_id": source.log_id, "version_id": source.version_id, "version_source": source.version_source,
            "input_hash": _source_hash(source.input_json), "checkpoint_hash": _source_hash(_source_json(checkpoint)),
            "header_hash": _source_hash(source.proposal_header_json), "receipt_hash": _source_hash(source.receipt_json),
            "decision_hash": _source_hash(source.accepted_decision_json),
        }
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError):
        raise ConflictError("提案自动分析来源无效", extra={"code": "rich_review_task_changed"}) from None


_APPLIED_SOURCE_FIELDS = frozenset({
    "version", "task_id", "task_key", "actor_id", "actor_token_version", "project_id", "owner_id", "chapter_id",
    "queued_at", "grant", "proposal_id", "proposal_token", "run_key", "execution_key", "claim_key",
    "source_revision", "applied_revision", "log_id", "version_id", "version_source", "input_hash",
    "checkpoint_hash", "header_hash", "receipt_hash", "decision_hash",
})


@dataclass(frozen=True, slots=True, repr=False)
class AppliedReviewSource:
    """已采纳来源的不可变、无正文快照；解析只做结构检查，不授予发送权限。"""

    source_json: str

    @property
    def value(self) -> dict[str, Any]:
        return json.loads(self.source_json)


def capture_applied_review_source(value: Any) -> AppliedReviewSource:
    invalid = ConflictError("提案自动分析来源无效", extra={"code": "rich_review_task_changed"})
    try:
        if type(value) is not dict or set(value) != _APPLIED_SOURCE_FIELDS or type(value["version"]) is not int \
                or value["version"] != 1:
            raise invalid
        if (any(type(value[key]) is not int or value[key] <= 0 for key in (
            "task_id", "actor_id", "project_id", "owner_id", "chapter_id", "proposal_id", "log_id", "version_id",
        )) or value["actor_id"] == value["owner_id"]
                or type(value["actor_token_version"]) is not int or value["actor_token_version"] < 0
                or type(value["source_revision"]) is not int or value["source_revision"] < 0
                or type(value["applied_revision"]) is not int
                or value["applied_revision"] != value["source_revision"] + 1
                or value["version_source"] not in {"generate", "polish", "revise"}
                or any(not chapter_claim._uuid(value[key]) for key in (
                    "task_key", "proposal_token", "run_key", "execution_key", "claim_key",
                ))):
            raise invalid
        grant = value["grant"]
        if (type(grant) is not list or len(grant) != 2 or not chapter_claim._uuid(grant[0])
                or type(grant[1]) is not int or grant[1] <= 0):
            raise invalid
        queued_at = datetime.fromisoformat(value["queued_at"])
        if _round_time(queued_at) != value["queued_at"]:
            raise invalid
        for key in ("input_hash", "checkpoint_hash", "header_hash", "receipt_hash", "decision_hash"):
            digest = value[key]
            if type(digest) is not str or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise invalid
        return AppliedReviewSource(_source_json(value))
    except (AttributeError, KeyError, TypeError, ValueError):
        raise invalid from None


async def require_applied_review_analysis_source(
    db: AsyncSession, frozen_source: AppliedReviewSource, child_execution: Any, *, read_lock: bool,
) -> None:
    """锁内复验原提案及正文版本；本函数本身不是物理发送许可。"""
    from app.services.analysis_service import AnalysisExecution
    from app.services.task_engine.execution_identity import TaskExecutionIdentity

    invalid = ConflictError("原提案分析来源已变化", extra={"code": "rich_review_task_changed"})
    try:
        if (type(frozen_source) is not AppliedReviewSource or type(child_execution) is not AnalysisExecution
                or type(read_lock) is not bool):
            raise invalid
        source = frozen_source.value
        if capture_applied_review_source(source) != frozen_source:
            raise invalid
        identity = child_execution.task_identity
        grant = (source["grant"][0], source["grant"][1])
        if (type(identity) is not TaskExecutionIdentity or child_execution.task_kind != "chapter.analyze"
                or (child_execution.actor_id, child_execution.project_id, child_execution.task_chapter_id,
                    child_execution.actor_token_version, child_execution.grant_identity)
                != (source["actor_id"], source["project_id"], source["chapter_id"],
                    source["actor_token_version"], grant)
                or (identity.task_id, identity.task_key, identity.actor_id, identity.project_id, identity.chapter_id,
                    identity.kind, identity.queued_at, identity.execution_key, identity.actor_token_version,
                    identity.grant_identity) != (
                    child_execution.task_id, child_execution.task_key, child_execution.actor_id,
                    child_execution.project_id, child_execution.task_chapter_id, child_execution.task_kind,
                    child_execution.queued_at, child_execution.execution_key, child_execution.actor_token_version,
                    child_execution.grant_identity,
                )
                or _source_hash(identity.input_json) != child_execution.input_hash
                or not _same_json(identity.input.get("_analysis_proposal_source"), source)):
            raise invalid
        actor = authorization_service.AuthenticatedActor(source["actor_id"], source["actor_token_version"])
        with db.no_autoflush:
            await authorization_service.require_authenticated(
                db, actor, write=not read_lock, targets=(source["owner_id"],),
            )
            project = await project_access.require_project(
                db, actor.id, source["project_id"], "ai.use", for_update=not read_lock,
                visibility_write=not read_lock, grant_identity=grant,
            )
            if project.user_id != source["owner_id"]:
                raise invalid
            child = await db.scalar(select(Task).where(
                Task.id == child_execution.task_id, Task.statistics_key == child_execution.task_key,
                Task.user_id == actor.id, Task.project_id == source["project_id"],
                Task.chapter_id == source["chapter_id"], Task.kind == "chapter.analyze",
                Task.status.in_(("running", "succeeded")), Task.queued_at == child_execution.queued_at,
                Task.execution_key == child_execution.execution_key, Task.admin_cancel_operation_id.is_(None),
                Task.admin_cancel_requested_at.is_(None),
                task_visible_to_actor(db, actor.id, active_grant_required=True, token_version=actor.token_version),
            ).with_for_update(read=read_lock))
            original = await db.scalar(select(Task).where(
                Task.id == source["task_id"], Task.statistics_key == source["task_key"], Task.user_id == actor.id,
                Task.project_id == source["project_id"], Task.chapter_id == source["chapter_id"],
                Task.kind.in_(("chapter.generate", "chapter.polish", "chapter.revise")), Task.status == "succeeded",
                Task.execution_key.is_(None), Task.admin_cancel_operation_id.is_(None),
                Task.admin_cancel_requested_at.is_(None),
            ).with_for_update(read=read_lock))
            chapter = await db.scalar(select(Chapter).where(
                Chapter.id == source["chapter_id"], Chapter.project_id == source["project_id"],
            ).with_for_update(read=read_lock))
            proposal = await db.scalar(select(SceneReviewProposal).where(
                SceneReviewProposal.id == source["proposal_id"], SceneReviewProposal.task_id == source["task_id"],
                SceneReviewProposal.task_key == source["task_key"],
                SceneReviewProposal.chapter_id == source["chapter_id"],
                SceneReviewProposal.token == source["proposal_token"],
                SceneReviewProposal.run_key == source["run_key"],
                SceneReviewProposal.content_revision == source["source_revision"],
                SceneReviewProposal.status == "applied",
            ).with_for_update(read=read_lock))
            if (child is None or original is None or chapter is None or proposal is None
                    or original.queued_at is None or _round_time(original.queued_at) != source["queued_at"]
                    or _source_hash(_source_json(original.input)) != source["input_hash"]
                    or _source_hash(_source_json(original.checkpoint)) != source["checkpoint_hash"]
                    or chapter_claim._grant(original.input) != grant
                    or type(original.input.get("_task_actor_token_version")) is not int
                    or original.input["_task_actor_token_version"] != actor.token_version
                    or _source_json(child.input) != identity.input_json
                    or not _same_json(child.input.get("_project_model"), original.input.get("_project_model"))
                    or not _same_json(child.input.get("_shared_embedding"), original.input.get("_shared_embedding"))
                    or chapter.content_revision != source["applied_revision"] or chapter.status != "done"
                    or any((chapter.generation_claim_key, chapter.generation_claim_task_key,
                            chapter.generation_claim_prev_status))):
                raise invalid
            header = dict(proposal.payload)
            decision = header.pop("accepted_decision", None)
            applied = header.pop("applied_result", None)
            if (_source_hash(_source_json(header)) != source["header_hash"]
                    or _source_hash(_source_json(decision)) != source["decision_hash"]
                    or not isinstance(applied, dict) or applied.get("status") != "applied"
                    or applied.get("task_status") != "succeeded" or applied.get("chapter_id") != chapter.id
                    or applied.get("content_revision") != chapter.content_revision):
                raise invalid
            payload = await _read_payload(db, proposal)
            receipt = payload.get("execution_receipt")
            _require_proposal_round(original, proposal, chapter, payload)
            expected_receipt = {
                "version": 1, "task_id": source["task_id"], "task_key": source["task_key"],
                "actor_id": source["actor_id"], "project_id": source["project_id"],
                "chapter_id": source["chapter_id"], "queued_at": source["queued_at"],
                "execution_key": source["execution_key"], "claim_key": source["claim_key"],
                "grant": source["grant"], "kind": "single",
            }
            if (_source_hash(_source_json(receipt)) != source["receipt_hash"]
                    or not _same_json(receipt, expected_receipt)
                    or not _same_json(payload.get("_model_binding"), original.input.get("_project_model"))):
                raise invalid
            log = await db.scalar(select(GenerationLog).where(
                GenerationLog.id == source["log_id"], GenerationLog.task_id == source["task_id"],
                GenerationLog.actor_user_id == actor.id, GenerationLog.project_id == source["project_id"],
                GenerationLog.chapter_id == source["chapter_id"], GenerationLog.kind == source["version_source"],
                GenerationLog.status == "success",
            ).with_for_update(read=read_lock))
            version = await db.scalar(select(ChapterVersion).where(
                ChapterVersion.id == source["version_id"], ChapterVersion.chapter_id == source["chapter_id"],
                ChapterVersion.generation_log_id == source["log_id"],
                ChapterVersion.source == source["version_source"],
            ).with_for_update(read=read_lock))
            if log is None or version is None or version.content != chapter.content:
                raise invalid
    except (AttributeError, KeyError, TypeError, ValueError):
        raise invalid from None


def _safe_storage_error(exc: SQLAlchemyError) -> AppError:
    if isinstance(exc, DBAPIError) and isinstance(chapter_claim._database_error(exc), ConflictError):
        return ConflictError("提案状态正在变化，请刷新后重试", extra={"code": "rich_review_task_changed"})
    return ReviewStorageError("提案存储暂不可用")


async def _write_payload(db: AsyncSession, proposal: SceneReviewProposal, payload: dict[str, Any]) -> None:
    """在同一事务流式写入完整提案；任务／提案完成前任何失败均整体回滚。"""
    await db.flush()  # 提案 ID 是外键；尚未提交，不会出现无分块的持久待审任务。
    count, pending, parts, size = 0, [], [], 0
    digest = hashlib.sha256()

    async def emit() -> None:
        nonlocal count, pending, parts, size
        if not parts:
            return
        content = "".join(parts)
        digest.update(content.encode("utf-8"))
        pending.append({"proposal_id": proposal.id, "ordinal": count, "content": content})
        count += 1
        parts, size = [], 0
        if len(pending) >= PROPOSAL_CHUNK_BATCH:
            await db.execute(insert(SceneReviewPayloadChunk), pending)
            pending = []

    try:
        for token in json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).iterencode(payload):
            cursor = 0
            while cursor < len(token):
                take = min(PROPOSAL_CHUNK_CHARS - size, len(token) - cursor)
                parts.append(token[cursor:cursor + take])
                size += take
                cursor += take
                if size == PROPOSAL_CHUNK_CHARS:
                    await emit()
        await emit()
        if pending:
            await db.execute(insert(SceneReviewPayloadChunk), pending)
    except (TypeError, UnicodeEncodeError) as exc:
        raise ConflictError("候选稿包含无法保存的内容，原章未修改", extra={"code": "rich_review_invalid"}) from exc
    proposal.payload = {"storage_version": 2, "chunk_count": count, "sha256": digest.hexdigest()}


async def _read_payload(db: AsyncSession, proposal: SceneReviewProposal) -> dict[str, Any]:
    header = proposal.payload
    if header.get("storage_version") != 2:
        return header  # 兼容已存在的早期单行提案。
    count = header.get("chunk_count")
    if type(count) is not int or count < 1 or not isinstance(header.get("sha256"), str):
        raise ConflictError("提案分块格式无效，原章未修改", extra={"code": "rich_review_invalid"})
    digest, chunks = hashlib.sha256(), []
    rows = await db.stream(select(SceneReviewPayloadChunk.ordinal, SceneReviewPayloadChunk.content).where(
        SceneReviewPayloadChunk.proposal_id == proposal.id,
    ).order_by(SceneReviewPayloadChunk.ordinal).execution_options(yield_per=4))
    try:
        async for ordinal, content in rows:
            if ordinal != len(chunks):
                raise ConflictError("提案分块不完整，原章未修改", extra={"code": "rich_review_invalid"})
            digest.update(content.encode("utf-8"))
            chunks.append(content)
    finally:
        await rows.close()
    if len(chunks) != count or digest.hexdigest() != header["sha256"]:
        raise ConflictError("提案分块不完整，原章未修改", extra={"code": "rich_review_invalid"})
    try:
        payload = json.loads("".join(chunks))
        if not isinstance(payload, dict):
            raise ValueError("候选格式错误")
    except (ValueError, TypeError) as exc:
        raise ConflictError("提案分块格式无效，原章未修改", extra={"code": "rich_review_invalid"}) from exc
    return payload


def review_identity(proposal: SceneReviewProposal) -> dict[str, Any]:
    return {"reason": "rich_document_review", "token": proposal.token, "task_key": proposal.task_key,
            "run_key": proposal.run_key, "chapter_id": proposal.chapter_id}


async def pending_for_task(db: AsyncSession, task: Task, *, lock: bool = False) -> SceneReviewProposal | None:
    if task.statistics_key is None:
        return None
    statement = select(SceneReviewProposal).where(
        SceneReviewProposal.task_id == task.id, SceneReviewProposal.task_key == task.statistics_key,
        SceneReviewProposal.status == "pending",
    ).order_by(SceneReviewProposal.id.desc()).limit(1)
    return await db.scalar(statement.with_for_update() if lock else statement)


async def recover_pending_identity(db: AsyncSession, task: Task) -> dict[str, Any] | None:
    """运行中恢复只认原 execution/队列/授权与完整提案；缺证时不能自动重发。"""
    try:
        with db.no_autoflush:
            proposal = await pending_for_task(db, task)
            checkpoint = task.checkpoint if isinstance(task.checkpoint, dict) else {}
            review = checkpoint.get("rich_review")
            if proposal is None:
                if isinstance(review, dict) and review.get("phase") == "awaiting_review":
                    raise ReviewRecoveryUncertain()
                return None
            if (not isinstance(review, dict) or review.get("phase") != "awaiting_review"
                    or review.get("token") != proposal.token or review.get("run_key") != proposal.run_key
                    or type(review.get("chapter_id")) is not int or review.get("chapter_id") != proposal.chapter_id
                    or task.project_id is None or task.status != "running"):
                raise ReviewRecoveryUncertain()
            visible = await db.scalar(select(Task.id).where(
                Task.id == task.id, Task.statistics_key == task.statistics_key,
                task_visible_to_actor(db, task.user_id, active_grant_required=True),
            ))
            chapter = await db.scalar(select(Chapter).where(
                Chapter.id == proposal.chapter_id, Chapter.project_id == task.project_id,
            ))
            if visible is None or chapter is None:
                raise ReviewRecoveryUncertain()
            payload = await _read_payload(db, proposal)
            if "execution_receipt" not in payload:
                raise ReviewRecoveryUncertain()
            _require_proposal_round(task, proposal, chapter, payload, running=True)
            _stored_plans(payload)
            return review_identity(proposal)
    except SQLAlchemyError as exc:
        # 存储暂不可用不能充当凭据损坏的证据。让接管者保留原认领并在下周期重试。
        raise _safe_storage_error(exc) from None
    except (AppError, ValueError, TypeError):
        pass
    # 不让错误链携带可能包含私密候选的 SQL/参数或畸形历史内容。
    raise ReviewRecoveryUncertain() from None


async def verify_single_pending_event(
    db: AsyncSession, execution: Any, claim: chapter_claim.ChapterClaim, event_data: Any,
) -> None:
    """调用方持认证、作品和任务锁；只发布原单章执行体已原子保存的待审提案。"""
    from app.services.task_engine.execution_identity import TaskExecutionIdentity

    invalid = ConflictError("待审提案的原执行身份已变化", extra={"code": "rich_review_task_changed"})
    try:
        if (type(execution) is not TaskExecutionIdentity or type(claim) is not chapter_claim.ChapterClaim
                or not chapter_claim._valid_identity(claim) or claim.kind != "single" or claim.phase != "running"
                or execution.kind not in {"chapter.generate", "chapter.polish", "chapter.revise"}
                or type(execution.actor_token_version) is not int or execution.actor_token_version < 0
                or (claim.task_id, claim.task_key, claim.actor_id, claim.project_id, claim.chapter_id,
                    chapter_claim._utc(claim.queued_at), claim.execution_key, claim.grant_identity)
                != (execution.task_id, execution.task_key, execution.actor_id, execution.project_id,
                    execution.chapter_id, execution.queued_at, execution.execution_key, execution.grant_identity)
                or not isinstance(event_data, dict)):
            raise invalid
        original_input = execution.input
        pointer = {"chapter_id": claim.chapter_id, "claim_key": claim.claim_key, "prev_status": claim.prev_status}
        if (not _same_json(original_input.get("_chapter_claim"), pointer)
                or type(original_input.get("_task_actor_token_version")) is not int
                or original_input["_task_actor_token_version"] != execution.actor_token_version):
            raise invalid
        original_input.pop("_chapter_claim")
        checkpoint = execution.checkpoint
        if checkpoint is not None and not isinstance(checkpoint, dict):
            raise invalid
        checkpoint = deepcopy(checkpoint or {})
        with db.no_autoflush:
            task = await db.scalar(select(Task).where(
                Task.id == execution.task_id, Task.statistics_key == execution.task_key,
                Task.user_id == execution.actor_id, Task.project_id == execution.project_id,
                Task.chapter_id == execution.chapter_id, Task.kind == execution.kind,
                Task.status == "running", Task.queued_at == execution.queued_at,
                Task.execution_key == execution.execution_key, Task.admin_cancel_operation_id.is_(None),
                Task.admin_cancel_requested_at.is_(None),
                task_visible_to_actor(db, execution.actor_id, active_grant_required=True,
                                      token_version=execution.actor_token_version),
            ).with_for_update().execution_options(populate_existing=True))
            chapter = await db.scalar(select(Chapter).where(
                Chapter.id == claim.chapter_id, Chapter.project_id == claim.project_id,
            ).with_for_update().execution_options(populate_existing=True))
            if (task is None or chapter is None or task.step_cursor != execution.step_cursor
                    or not _same_json(task.input, original_input) or chapter.status != claim.prev_status
                    or any((chapter.generation_claim_key, chapter.generation_claim_task_key,
                            chapter.generation_claim_prev_status))):
                raise invalid
            proposal = await pending_for_task(db, task, lock=True)
            if proposal is None or not _same_json(review_identity(proposal), event_data):
                raise invalid
            checkpoint["rich_review"] = {
                "phase": "awaiting_review", "token": proposal.token, "run_key": proposal.run_key,
                "chapter_id": proposal.chapter_id,
            }
            if not _same_json(task.checkpoint, checkpoint):
                raise invalid
            payload = await _read_payload(db, proposal)
            _require_proposal_round(task, proposal, chapter, payload, running=True)
            receipt = {
                "version": 1, "task_id": claim.task_id, "task_key": claim.task_key, "actor_id": claim.actor_id,
                "project_id": claim.project_id, "chapter_id": claim.chapter_id,
                "queued_at": _round_time(claim.queued_at), "execution_key": claim.execution_key,
                "claim_key": claim.claim_key, "grant": list(claim.grant_identity) if claim.grant_identity else None,
                "kind": "single",
            }
            if not _same_json(payload.get("execution_receipt"), receipt):
                raise invalid
            _stored_plans(payload)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise invalid from None


async def require_review_identity(db: AsyncSession, task: Task, expected: ReviewIdentity | None) -> None:
    proposal = await pending_for_task(db, task)
    if proposal is None:
        if expected is not None:
            raise ConflictError("原提案轮次已变化，请刷新后重试", extra={"code": "rich_review_task_changed"})
        return
    if (expected is None or expected.task_key != proposal.task_key or expected.run_key != proposal.run_key
            or expected.token != proposal.token):
        raise ConflictError("请针对当前提案明确确认操作", extra={"code": "rich_review_task_changed"})


def require_finished_review_identity(task: Task, expected: ReviewIdentity | None) -> None:
    review = (task.checkpoint or {}).get("rich_review") if isinstance(task.checkpoint, dict) else None
    if not isinstance(review, dict):
        if expected is not None:
            raise ConflictError("原提案轮次已变化，请刷新后重试", extra={"code": "rich_review_task_changed"})
        return
    if (expected is None or task.statistics_key != expected.task_key or review.get("run_key") != expected.run_key
            or review.get("token") != expected.token):
        raise ConflictError("请针对当前提案明确确认操作", extra={"code": "rich_review_task_changed"})


async def discard_pending(db: AsyncSession, task: Task) -> bool:
    """调用方已通过 task UUID/status CAS 取得任务写权，和任务终态同事务提交。"""
    proposal = await pending_for_task(db, task, lock=True)
    if proposal is None:
        return False
    proposal.status = "discarded"
    checkpoint = deepcopy(task.checkpoint or {})
    review = checkpoint.get("rich_review") or {}
    if review.get("token") == proposal.token and review.get("run_key") == proposal.run_key:
        review["phase"] = "discarded"
        checkpoint["rich_review"] = review
        if task.kind == "chapter.batch" and checkpoint.get("phase") == "awaiting_review":
            checkpoint["phase"], checkpoint["current"] = "generate", None
        task.checkpoint = checkpoint
    return True


async def persist_pending(
    db: AsyncSession, chapter: Chapter, job: "ChapterJob", plans: dict[int, SceneProposalMapping],
    log_values: dict[str, Any],
) -> dict[str, Any]:
    """提案、断点与释放生成抢占同事务落库；任何失败均回滚整个转换。"""
    try:
        return await _persist_pending(db, chapter, job, plans, log_values)
    except SQLAlchemyError as exc:
        failure = _safe_storage_error(exc)
        await chapter_claim._rollback(db)
    except BaseException:
        # 包括取消及写入分块中途失败；调用方捕获异常后也不能提交半个转换。
        await chapter_claim._rollback(db)
        raise
    # 离开 except 作用域后抛出，避免 __context__ 持有 SQL 与参数。
    raise failure from None


def _round_time(value: datetime) -> str:
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()


async def _persist_pending(
    db: AsyncSession, chapter: Chapter, job: "ChapterJob", plans: dict[int, SceneProposalMapping],
    log_values: dict[str, Any],
) -> dict[str, Any]:
    from app.services.rich_document_store import capture_documents

    if job.task_id is None or not job.task_key or job.context_stamp is None:
        raise ConflictError("此操作需要持久任务才能安全核对富文档", extra={"code": "rich_review_task_required"})
    claim = getattr(job, "chapter_claim", None)
    with db.no_autoflush:
        if (chapter.id != job.chapter_id or chapter.project_id != job.project_id
                or job.context_stamp.project_id != job.project_id or job.context_stamp.chapter_id != chapter.id):
            raise ConflictError("章节身份已变化，候选稿未保存", extra={"code": "rich_review_task_changed"})
        if claim is not None:
            if (not isinstance(claim, chapter_claim.ChapterClaim)
                    or (claim.task_id, claim.task_key, claim.actor_id, claim.project_id, claim.chapter_id,
                        claim.prev_status) != (job.task_id, job.task_key, job.user_id, job.project_id,
                                               chapter.id, job.prev_status)):
                raise ConflictError("章节执行轮次已变化，候选稿未保存", extra={"code": "rich_review_task_changed"})
            await chapter_claim.require_running_chapter(db, claim)
        else:
            # 旧作者 batch 尚未完成逐次 claim 接线；严禁它覆盖已有新占用。
            await setting_ai_visibility.lock_author_visibility_write(db, job.user_id, job.project_id, manage=False)
        task = await db.scalar(select(Task).where(
            Task.id == job.task_id, Task.statistics_key == job.task_key, Task.user_id == job.user_id,
            Task.project_id == job.project_id, Task.status == "running", Task.admin_cancel_operation_id.is_(None),
        ).with_for_update().execution_options(populate_existing=True))
        if task is None:
            raise ConflictError("任务状态已变化，候选稿未保存", extra={"code": "rich_review_task_changed"})
        if claim is None and (task.kind != "chapter.batch" or chapter.status != "generating" or any((
            chapter.generation_claim_key, chapter.generation_claim_task_key, chapter.generation_claim_prev_status,
        ))):
            raise ConflictError("章节执行身份不可验证，候选稿未保存", extra={"code": "rich_review_task_changed"})
        await scene_ai_context.require_stamp(db, job.context_stamp)
        scenes = await scene_service.list_active_scenes(db, chapter.id)
        if ([scene.id for scene in scenes] != [scene.get("id") for scene in job.scenes]
                or set(plans) != {scene.id for scene in scenes}
                or any(scene.separator_before != snapshot.get("separator_before")
                       for scene, snapshot in zip(scenes, job.scenes, strict=True))):
            raise ConflictError("场景结构已变化，候选稿未保存", extra={"code": "scene_context_changed"})
        source, _ = await capture_documents(db, scenes)
        if any(source[scene.id] != plans[scene.id].original for scene in scenes):
            raise ConflictError("原始文档已变化，候选稿未保存", extra={"code": "scene_context_changed"})
    checkpoint = deepcopy(task.checkpoint or {})
    if checkpoint.get("rich_review", {}).get("phase") == "awaiting_review":
        raise ConflictError("该任务已有待核对提案", extra={"code": "rich_review_pending"})
    token = str(uuid4())
    payload = {
        "mapping_version": 1, "stamp": job.context_stamp.log_data(), "mode": job.mode, "prev_status": job.prev_status,
        "target": job.target.model_dump(mode="json"), "log_values": log_values,
        "scenes": [{"scene_id": scene["id"], "separator_before": scene["separator_before"],
                    "original": plans[scene["id"]].original.model_dump(mode="json"),
                    "candidate": plans[scene["id"]].candidate,
                    "template": plans[scene["id"]].template,
                    "unresolved_sections": plans[scene["id"]].unresolved_sections,
                    "format_loss": plans[scene["id"]].format_loss}
                   for scene in job.scenes],
    }
    binding = getattr(job, "model_binding", None)
    if claim is not None and binding is None:
        raise ConflictError("候选稿缺少可信模型来源", extra={"code": "rich_review_task_changed"})
    if binding is not None:
        encoded = project_ai_config.serialize_project_model(binding)
        owner_id = await db.scalar(select(Project.user_id).where(Project.id == job.project_id))
        checked = project_ai_config.deserialize_project_model(
            encoded, actor_id=job.user_id, project_id=job.project_id, project_owner_id=owner_id,
        )
        if (checked is None or checked.model_type != "text"
                or checked.grant_identity != chapter_claim._grant(task.input)
                or type(log_values.get("ai_config_id")) is not int
                or log_values.get("ai_config_id") != checked.config_id):
            raise ConflictError("候选稿模型来源与任务不一致", extra={"code": "rich_review_task_changed"})
        # 配置来源和指纹仅存于私有提案分块，不进入任务事件、预览或日志参数。
        payload["_model_binding"] = encoded
    if claim is not None:
        # execution 在暂停后会被引擎清空；将原执行体事实只保存在私有分块中。
        payload["execution_receipt"] = {
            "version": 1, "task_id": claim.task_id, "task_key": claim.task_key, "actor_id": claim.actor_id,
            "project_id": claim.project_id, "chapter_id": claim.chapter_id, "queued_at": _round_time(claim.queued_at),
            "execution_key": claim.execution_key, "claim_key": claim.claim_key,
            "grant": list(claim.grant_identity) if claim.grant_identity is not None else None,
            "kind": claim.kind,
        }
        if not await chapter_claim.release_chapter_claim(db, claim):
            raise ConflictError("章节执行轮次已变化，候选稿未保存", extra={"code": "rich_review_task_changed"})
        # batch 释放会同时清除 checkpoint.current，不能用释放前的副本覆盖它。
        checkpoint = deepcopy(task.checkpoint or {})
    proposal = SceneReviewProposal(task_id=task.id, chapter_id=chapter.id, task_key=job.task_key,
                                   run_key=job.run_key, token=token, content_revision=chapter.content_revision,
                                   status="pending", payload={})
    db.add(proposal)
    await _write_payload(db, proposal, payload)
    checkpoint["rich_review"] = {"phase": "awaiting_review", "token": token, "run_key": job.run_key,
                                 "chapter_id": chapter.id}
    if task.kind == "chapter.batch":
        checkpoint["phase"], checkpoint["current"] = "awaiting_review", None
    task.checkpoint = checkpoint
    if claim is None:
        chapter.status = job.prev_status
    await db.flush()
    return review_identity(proposal)


async def _owned_proposal(
    db: AsyncSession, user_id: int, task_id: int, task_key: str, run_key: str, token: str, *, lock: bool = False,
) -> tuple[Task, SceneReviewProposal]:
    from app.services.task_service import get_owned_task

    with db.no_autoflush:
        task = await get_owned_task(db, user_id, task_id)
        if task.statistics_key != task_key:
            raise NotFoundError("提案不存在")
        statement = select(SceneReviewProposal).where(
            SceneReviewProposal.task_id == task_id, SceneReviewProposal.task_key == task_key,
            SceneReviewProposal.run_key == run_key, SceneReviewProposal.token == token,
        )
        if lock:
            task = await db.scalar(select(Task).where(
                Task.id == task_id, Task.user_id == user_id, Task.statistics_key == task_key,
            ).with_for_update().execution_options(populate_existing=True))
            statement = statement.with_for_update().execution_options(populate_existing=True)
        proposal = await db.scalar(statement)
    if task is None or proposal is None:
        raise NotFoundError("提案不存在")
    return task, proposal


def _stored_plans(payload: dict[str, Any]) -> list[tuple[int, SceneProposalMapping]]:
    if payload.get("mapping_version") != 1 or not isinstance(payload.get("scenes"), list):
        raise ConflictError("提案格式已失效，原稿未修改", extra={"code": "rich_review_invalid"})
    plans = []
    seen: set[int] = set()
    try:
        for item in payload["scenes"]:
            scene_id = item["scene_id"]
            if type(scene_id) is not int or scene_id in seen:
                raise ValueError("场景身份无效")
            seen.add(scene_id)
            source = SceneDocument.model_validate(item["original"])
            plan = build_scene_proposal(source, item["candidate"])
            if (plan.template != item["template"] or plan.unresolved_sections != item["unresolved_sections"]
                    or plan.format_loss is not item["format_loss"]):
                raise ValueError("映射版本不一致")
            plans.append((scene_id, plan))
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ConflictError("提案结构或映射已失效，请保留原稿并核对", extra={
            "code": "rich_review_invalid",
        }) from exc
    return plans


@dataclass(frozen=True, slots=True)
class _ReviewReadIdentity:
    """读取前捕获不可变事实；不得用后续刷新的任务替旧结果重新签发权限。"""

    task_id: int
    task_key: str
    project_id: int | None
    queued_at: datetime | None
    task_status: str
    grant: tuple[str, int] | None
    proposal_id: int
    chapter_id: int
    run_key: str
    token: str
    proposal_status: str
    content_revision: int
    payload_header: dict[str, Any]


async def _require_review_readable(
    db: AsyncSession, user_id: int, identity: _ReviewReadIdentity, *, scene_ids: tuple[int, ...] = (),
) -> None:
    """独立事务的单条 SQL 复核本人任务、原轮次和提案身份，不能沿用旧事务快照。"""
    factory = visibility_sessionmaker(db)
    async with factory() as check:
        # 必须复用请求认证时的原始版本，不从当前数据库重建旧令牌身份。
        if "authenticated_actor" in db.info:
            check.info["authenticated_actor"] = db.info["authenticated_actor"]
        statement = (
            select(Task.input, Task.queued_at, SceneReviewProposal.payload)
            .join(SceneReviewProposal, SceneReviewProposal.task_id == Task.id)
            .join(Chapter, Chapter.id == SceneReviewProposal.chapter_id)
            .where(
                Task.id == identity.task_id, Task.statistics_key == identity.task_key,
                Task.project_id == identity.project_id, Task.project_id.is_not(None),
                Task.status == identity.task_status,
                task_visible_to_actor(check, user_id, active_grant_required=identity.task_status in OPEN_TASK_STATUSES),
                SceneReviewProposal.id == identity.proposal_id, SceneReviewProposal.task_key == identity.task_key,
                SceneReviewProposal.chapter_id == identity.chapter_id, Chapter.project_id == Task.project_id,
                SceneReviewProposal.run_key == identity.run_key, SceneReviewProposal.token == identity.token,
                SceneReviewProposal.status == identity.proposal_status,
                SceneReviewProposal.content_revision == identity.content_revision,
            )
        )
        # 历史 JSON 的场景身份也必须属于提案章节；分组检查但仍在同一 SQL 快照中完成。
        for ids in batched(scene_ids, 400):
            count = select(func.count(Scene.id)).where(
                Scene.id.in_(ids), Scene.chapter_id == identity.chapter_id,
            ).scalar_subquery()
            statement = statement.where(count == len(ids))
        with check.no_autoflush:
            row = (await check.execute(statement)).first()
        if (row is None or row.queued_at != identity.queued_at or task_grant_identity(row.input) != identity.grant
                or not _same_json(row.payload, identity.payload_header)):
            # 先区分令牌失效和不可见资源；不能透露其他成员的任务或提案身份。
            await authorization_service.require_authenticated(check, user_id)
            raise NotFoundError("提案不存在")


async def get_review(
    db: AsyncSession, user_id: int, task_id: int, task_key: str, run_key: str, token: str,
) -> dict[str, Any]:
    with db.no_autoflush:
        task, proposal = await _owned_proposal(db, user_id, task_id, task_key, run_key, token)
        identity = _ReviewReadIdentity(
            task_id=task.id, task_key=task_key, project_id=task.project_id, queued_at=task.queued_at,
            task_status=task.status, grant=task_grant_identity(task.input), proposal_id=proposal.id,
            chapter_id=proposal.chapter_id, run_key=proposal.run_key, token=proposal.token,
            proposal_status=proposal.status, content_revision=proposal.content_revision,
            payload_header=deepcopy(proposal.payload),
        )
        try:
            plans = _stored_plans(await _read_payload(db, proposal))
        except AppError:
            # 提案损坏等错误也不得在读取期间失权后泄露已存在的提案状态。
            await _require_review_readable(db, user_id, identity)
            raise
        output = {**review_identity(proposal), "status": proposal.status, "task_status": task.status,
                  "content_revision": proposal.content_revision,
                  "scenes": [{"scene_id": scene_id, "original": plan.original.model_dump(mode="json"),
                              "candidate": plan.candidate, "proposed_formatting": plan.template["formatting"],
                              "proposed_markers": plan.template["markers"],
                              "proposed_sections": [
                                  {"key": item["key"], "anchor": item["anchor"], "order": item["order"]}
                                  for item in plan.template["sections"]
                              ],
                              "unresolved_sections": plan.unresolved_sections, "format_loss": plan.format_loss}
                             for scene_id, plan in plans]}
    await _require_review_readable(db, user_id, identity, scene_ids=tuple(scene_id for scene_id, _ in plans))
    return output


async def accept_review(
    db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int, token: str, data: ReviewAccept,
) -> dict[str, Any]:
    try:
        return await _accept_review(db, engine, user_id, task_id, token, data)
    except SQLAlchemyError as exc:
        failure = _safe_storage_error(exc)
        await chapter_claim._rollback(db)
    except BaseException:
        # 版本、正文、日志或断点的中途失败都不能被外层误提交。
        await chapter_claim._rollback(db)
        raise
    raise failure from None


def _require_proposal_round(
    task: Task, proposal: SceneReviewProposal, chapter: Chapter, payload: dict[str, Any], *, running: bool = False,
) -> None:
    """旧提案按历史规则兼容；新提案必须匹配生成时保存在私有分块的执行事实。"""
    invalid = ConflictError("提案或任务执行轮次已变化，请保留提案", extra={"code": "rich_review_task_changed"})
    if (not isinstance(task.input, dict) or (running and (
        task.status != "running" or not chapter_claim._uuid(task.execution_key)
    )) or (not running and task.execution_key is not None)):
        raise invalid
    request = task.input.get("request")
    if not isinstance(request, dict):
        raise invalid
    if task.kind == "chapter.batch":
        ids = task.input.get("chapter_ids")
        if (task.chapter_id is not None or task.resource_key != f"batch:{task.project_id}"
                or not isinstance(ids, list) or any(type(cid) is not int or cid <= 0 for cid in ids)
                or len(ids) != len(set(ids)) or chapter.id not in ids
                or type(request.get("project_id")) is not int or request["project_id"] != task.project_id):
            raise invalid
        kind = "batch"
    else:
        if (task.kind not in {"chapter.generate", "chapter.polish", "chapter.revise"}
                or task.chapter_id != chapter.id or task.resource_key != f"chapter:{chapter.id}"
                or type(task.input.get("chapter_id")) is not int or task.input["chapter_id"] != chapter.id
                or type(request.get("chapter_id")) is not int or request["chapter_id"] != chapter.id):
            raise invalid
        kind = "single"
    if "execution_receipt" not in payload:
        if running:
            raise invalid
        return
    receipt = payload["execution_receipt"]
    grant = chapter_claim._grant(task.input)
    if not isinstance(receipt, dict) or not isinstance(task.queued_at, datetime):
        raise invalid
    expected = {
        "version": 1, "task_id": task.id, "task_key": task.statistics_key, "actor_id": task.user_id,
        "project_id": task.project_id, "chapter_id": chapter.id, "queued_at": _round_time(task.queued_at),
        "execution_key": task.execution_key if running else receipt.get("execution_key"),
        "claim_key": receipt.get("claim_key"),
        "grant": list(grant) if grant is not None else None, "kind": kind,
    }
    if proposal.task_key != task.statistics_key or not _same_json(receipt, expected):
        raise invalid
    try:
        execution, claim_key = receipt["execution_key"], receipt["claim_key"]
        if (not isinstance(execution, str) or str(UUID(execution)) != execution
                or not isinstance(claim_key, str) or str(UUID(claim_key)) != claim_key
                or execution == claim_key or execution == proposal.task_key or claim_key == proposal.task_key):
            raise invalid
    except (KeyError, ValueError, TypeError):
        raise invalid from None
    if kind == "single" and "_chapter_claim" in task.input:
        raise invalid


async def _accept_review(
    db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int, token: str, data: ReviewAccept,
) -> dict[str, Any]:
    from app.services import analysis_service, batch_service, generation_log_service, generation_service, task_service
    from app.services.rich_document_service import write_mapped_document
    from app.services.rich_document_store import capture_documents

    authenticated_actor = db.info.get("authenticated_actor")
    task, proposal = await _owned_proposal(db, user_id, task_id, data.task_key, data.run_key, token)
    project_id, chapter_id = task.project_id, proposal.chapter_id
    if project_id is None:
        raise NotFoundError("提案不存在")
    initial = (task.user_id, task.project_id, task.queued_at, task.execution_key, proposal.id, proposal.chapter_id)
    initial_input = deepcopy(task.input)
    original_grant = chapter_claim._grant(initial_input)
    try:
        project = await project_access.require_project(
            db, user_id, project_id, "ai.use", for_update=True, visibility_write=True, grant_identity=original_grant,
        )
    except ForbiddenError:
        # 旧提案属于原邀请；撤权、降级或重新邀请都不能通过管理员旁路复用。
        raise NotFoundError("提案不存在") from None
    if (project.user_id == user_id) != (original_grant is None):
        raise NotFoundError("提案不存在")
    if project.user_id != user_id and (
        type(authenticated_actor) is not authorization_service.AuthenticatedActor
        or authenticated_actor.id != user_id or type(initial_input.get("_task_actor_token_version")) is not int
        or initial_input["_task_actor_token_version"] != authenticated_actor.token_version
    ):
        raise NotFoundError("提案不存在")
    task, proposal = await _owned_proposal(db, user_id, task_id, data.task_key, data.run_key, token, lock=True)
    if ((task.user_id, task.project_id, task.queued_at, task.execution_key, proposal.id, proposal.chapter_id) != initial
            or not _same_json(task.input, initial_input)):
        raise ConflictError("提案或任务执行轮次已变化，请保留提案", extra={"code": "rich_review_task_changed"})
    chapter = await db.scalar(select(Chapter).where(
        Chapter.id == chapter_id, Chapter.project_id == project_id,
    ).with_for_update().execution_options(populate_existing=True))
    if chapter is None:
        raise NotFoundError("提案不存在")
    positions: dict[tuple[int, str], int] = {}
    for item in data.sections:
        key = item.scene_id, item.section_key
        if key in positions:
            raise ConflictError("区块位置不能重复指定", extra={"code": "rich_review_invalid_positions"})
        positions[key] = item.anchor
    decision = {"sections": [[scene_id, key, positions[(scene_id, key)]] for scene_id, key in sorted(positions)],
                "confirm_format_loss": data.confirm_format_loss}
    if proposal.status == "applied":
        if proposal.payload.get("accepted_decision") != decision:
            raise ConflictError("提案已按其他选择采纳", extra={"code": "rich_review_already_applied"})
        return proposal.payload["applied_result"]
    proposal_data = await _read_payload(db, proposal)
    checkpoint = deepcopy(task.checkpoint or {})
    review = checkpoint.get("rich_review") or {}
    if (proposal.status != "pending" or task.status != "paused" or task.admin_cancel_operation_id is not None
            or task.admin_cancel_requested_at is not None
            or review.get("phase") != "awaiting_review" or review.get("token") != token
            or review.get("run_key") != data.run_key or review.get("chapter_id") != chapter_id):
        raise ConflictError("提案或任务状态已变化，请重新核对", extra={"code": "rich_review_task_changed"})
    _require_proposal_round(task, proposal, chapter, proposal_data)
    values = proposal_data.get("log_values") or {}
    if (not isinstance(values, dict)
            or any(type(values.get(key)) is not int or values[key] != expected for key, expected in (
                ("project_id", project_id), ("chapter_id", chapter_id), ("task_id", task_id),
            )) or values.get("kind") != proposal_data.get("mode")
            or type(values.get("actor_user_id", task.user_id)) is not int
            or values.get("actor_user_id", task.user_id) != task.user_id):
        raise ConflictError("提案生成记录身份无效", extra={"code": "rich_review_invalid"})
    model_binding = None
    if "_model_binding" in proposal_data:
        model_binding = project_ai_config.deserialize_project_model(
            proposal_data["_model_binding"], actor_id=task.user_id, project_id=project_id,
            project_owner_id=project.user_id,
        )
        if (model_binding is None or model_binding.model_type != "text"
                or model_binding.grant_identity != chapter_claim._grant(task.input)
                or type(values.get("ai_config_id")) is not int
                or model_binding.config_id != values.get("ai_config_id")):
            raise ConflictError("提案模型来源与任务不一致", extra={"code": "rich_review_invalid"})
    if project.user_id != user_id and (
        "execution_receipt" not in proposal_data or model_binding is None
        or not _same_json(initial_input.get("_project_model"), proposal_data.get("_model_binding"))
    ):
        raise ConflictError("提案缺少原执行及模型来源", extra={"code": "rich_review_invalid"})
    if (chapter.status != proposal_data.get("prev_status") or any((chapter.generation_claim_key,
            chapter.generation_claim_task_key, chapter.generation_claim_prev_status))):
        raise ConflictError("章节状态已变化，请保留提案并重新核对", extra={"code": "scene_context_changed"})
    await scene_ai_context.require_stamp(db, proposal_data.get("stamp") or {})
    if task.kind == "chapter.batch":
        await batch_service.revalidate(db, engine.ctx, task)
    else:
        from app.services.generation_service import ensure_outside_batch

        await ensure_outside_batch(db, chapter)
    plans = _stored_plans(proposal_data)
    scenes = await scene_service.list_active_scenes(db, chapter.id)
    if [scene.id for scene in scenes] != [scene_id for scene_id, _ in plans]:
        raise ConflictError("场景结构已变化，请保留提案并重新核对", extra={"code": "scene_context_changed"})
    source, _ = await capture_documents(db, scenes)
    expected_positions = {(scene_id, key) for scene_id, plan in plans for key in plan.unresolved_sections}
    if set(positions) != expected_positions:
        raise ConflictError("请为每个待核对区块明确选择位置", extra={"code": "rich_review_invalid_positions"})
    mapped: dict[int, SceneDocument] = {}
    for scene_id, plan in plans:
        if source[scene_id] != plan.original:
            raise ConflictError("原始文档已变化，请保留提案并重新核对", extra={"code": "scene_context_changed"})
        try:
            mapped[scene_id] = resolve_scene_proposal(
                plan, {key: anchor for (owner, key), anchor in positions.items() if owner == scene_id},
                confirm_format_loss=data.confirm_format_loss,
            )
        except ProposalMappingError as exc:
            raise ConflictError(str(exc), extra={"code": "rich_review_invalid_positions"}) from exc
    await version_service.ensure_baseline(db, chapter, force=True)
    rich_ids = await scene_service._rich_scene_ids(db, [scene.id for scene in scenes])
    for scene in scenes:
        document = mapped[scene.id]
        if document == source[scene.id]:
            continue
        if scene.id in rich_ids:
            await write_mapped_document(db, project_id, scene, source[scene.id], document, proposal.content_revision)
        else:
            scene.content = document.content
    await scene_service.refresh_projection(db, chapter, expected_revision=proposal.content_revision)
    chapter.status = "done"
    # 升级前已持久化的提案没有 actor；绑定的任务本人是可信历史身份。
    values["actor_user_id"] = task.user_id
    if model_binding is None:
        # 历史提案没有模型实例证明，不能把旧整数 ID 关联到后来重建的配置。
        values["ai_config_id"] = None
        log = await generation_log_service.write_log(db, **values)
    else:
        log = await generation_log_service.write_log(db, **values, model_binding=model_binding)
    version = await version_service.record(db, chapter, proposal_data["mode"], log_id=log.id)
    await db.flush()
    await db.refresh(chapter, ["updated_at"])
    review["phase"] = "applied"
    checkpoint["rich_review"] = review
    analysis_warning = None
    if task.kind == "chapter.batch":
        items = checkpoint.get("items") or []
        if task.step_cursor >= len(items) or items[task.step_cursor].get("chapter_id") != chapter.id:
            raise ConflictError("批量断点已变化，请保留提案", extra={"code": "rich_review_task_changed"})
        item = items[task.step_cursor]
        item.update(state="done", words=chapter.word_count, error="", warning=values.get("error") or "")
        checkpoint["phase"], checkpoint["current"] = "analyze", None
        from app.services.rich_document_ai import analysis_fingerprints

        fingerprint = (await analysis_fingerprints(db, [chapter]))[chapter.id]
        checkpoint["rich_analysis"] = {"run_key": data.run_key, "task_key": data.task_key,
                                       "chapter_id": chapter.id, "content_hash": fingerprint}
        task.status, task.result, task.error = "queued", None, ""
        task.stage, task.finished_at = task_service.STAGE_LABELS["queued"], None
        task.queued_at = task_service._next_queued_at(task)
        task.worker_key, task.recovered_at = None, None
        await task_statistics_service.record_state_change(db, task, "queued")
    else:
        chapter_view = await generation_service.chapter_ai_view(db, user_id, project_id, chapter)
        payload: dict[str, Any] = {"chapter": chapter_view.model_dump(mode="json")}
        target = proposal_data.get("target") or {}
        if target.get("kind") == "scene":
            scene = await scene_service.require_scene(db, chapter, target.get("scene_id"))
            payload["scene"] = (await scene_service.scene_out(db, chapter, scene)).model_dump(mode="json")
        else:
            payload["scene_ids"] = [scene.id for scene in scenes]
            if engine.ctx.settings.auto_analyze_enabled and project.auto_analyze:
                if (type(authenticated_actor) is not authorization_service.AuthenticatedActor
                        or authenticated_actor.id != user_id):
                    raise UnauthorizedError("未登录或登录已过期")
                try:
                    if project.user_id != user_id:
                        if original_grant is None or task.queued_at is None or task.statistics_key is None:
                            raise ConflictError("提案的自动分析来源无效", extra={"code": "rich_review_task_changed"})
                        source = ReviewAutoSource(
                            task_id=task.id, task_key=task.statistics_key, actor_id=user_id,
                            actor_token_version=authenticated_actor.token_version, project_id=project.id,
                            owner_id=project.user_id, chapter_id=chapter.id, queued_at=_round_time(task.queued_at),
                            grant_identity=original_grant, proposal_id=proposal.id, proposal_token=proposal.token,
                            run_key=proposal.run_key, source_revision=proposal.content_revision,
                            applied_revision=chapter.content_revision, log_id=log.id, version_id=version.id,
                            version_source=proposal_data["mode"], input_json=_source_json(initial_input),
                            checkpoint_json=_source_json(task.checkpoint),
                            proposal_header_json=_source_json(proposal.payload),
                            receipt_json=_source_json(proposal_data["execution_receipt"]),
                            accepted_decision_json=_source_json(decision),
                        )
                        await analysis_service.enqueue_auto_in_transaction(
                            db, project, chapter.id, actor=authenticated_actor, cipher=engine.ctx.cipher,
                            allow_private=engine.ctx.settings.allow_private_ai_endpoints, review_source=source,
                        )
                    elif not _trusted_owner_review_origin(task, initial_input, authenticated_actor):
                        # 历史任务缺少原执行身份或原授权已被撤销：不以本次会话或当前数据库值补签派生任务。
                        analysis_warning = REVIEW_AUTO_ORIGIN_UNTRUSTED
                    else:
                        await analysis_service.enqueue_auto_in_transaction(
                            db, project, chapter.id, actor=authenticated_actor, cipher=engine.ctx.cipher,
                            allow_private=engine.ctx.settings.allow_private_ai_endpoints,
                        )
                except analysis_service.AutoAnalysisUnavailable:
                    analysis_warning = "自动分析未排队，请检查模型设置后手动分析"
        stamp = task_statistics_service.now_utc()
        task.status, task.result, task.error, task.progress = "succeeded", payload, "", 100
        task.stage, task.finished_at = task_service.STAGE_LABELS["succeeded"], stamp
        await task_statistics_service.record_state_change(db, task, "succeeded", finished_at=stamp)
    task.checkpoint = checkpoint
    outcome = {"status": "applied", "task_status": task.status, "chapter_id": chapter.id,
               "content_revision": chapter.content_revision}
    if analysis_warning is not None:
        outcome["analysis_warning"] = analysis_warning
    proposal.status = "applied"
    proposal.payload = {**proposal.payload, "accepted_decision": decision, "applied_result": outcome}
    try:
        await commit_versioned(db)
    except IntegrityError:
        await db.rollback()
        failure = ConflictError("任务资源已被占用，请保留提案并稍后再试", extra={
            "code": "rich_review_task_changed",
        })
    else:
        engine.notify()
        return outcome
    raise failure from None
