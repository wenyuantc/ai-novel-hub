"""AI 向导（后台任务 wizard.*）：流式生成大纲 / 角色 / 世界观，结束后解析 JSON 并覆盖落库。

角色向导（R11）输出 {characters: [结构化字段],
relationships: [{a, b, role_a, role_b, category, intimacy, description}]}，
兼容旧格式（角色数组，只有 profile / relations_text）。写入顺序：先整体替换角色（级联清掉旧关系 / 成员 / 出场记录），
再按名字（或别名）建立关系，经 relationship_service.apply_change(source="wizard") 写入；名字对不上的关系忽略。
世界观向导输出 category，中英文映射到六个枚举值，映射不了的归为 other。
职业向导（wizard.careers）根据世界观生成职业与阶梯，整体覆盖（删除全部职业，级联清掉挂接与挂接历史）。
项目已有职业时，角色向导要求每个角色输出 careers: [{name, stage, role, note}]，按职业名 / 阶段名精确匹配后经
career_service.apply_assignment(source="wizard") 写入；职业对不上 / 超出上限的忽略并写入 done.result.warnings，
阶段名对不上的挂在第 1 阶并给警告。项目没有职业时角色向导的提示词与写入行为不变。
大纲向导（outline-pro）：outline_mode = replace（默认）覆盖前先存大纲快照（wizard_replace），
章节经外键解除关联、不删除；
append 不删旧大纲，提示词附上已有大纲末尾 10 条，写入时卷号接最后一卷、章节号接全书最后一章、sort 接末尾。
从中断处继续（continue_from_task_id）：旧任务的部分输出截到最后一个完整对象（最后一个 `}`）作为前缀，
原对话后追加 assistant（前缀）+ user（wizard.continue 续写指令）；运行时先把前缀作为 delta 输出（新任务 output_text
即完整累积文本，再失败可继续接），续写开头缓冲一段做清洗（去代码块标记、去与前缀末尾的重复、补逗号），
拼接后照常解析落库；模型无视指令从头输出完整 JSON 时只用续写部分。
"""

import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import String, cast, delete, inspect, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.util import identity_key

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import (
    build_careers_wizard_messages,
    build_characters_messages,
    build_outline_messages,
    build_wizard_continue_messages,
    build_world_messages,
    format_project_info,
)
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import AppError, BadRequestError, ConflictError
from app.core.security import SecretCipher
from app.models import (
    Base,
    Career,
    Character,
    CharacterCareer,
    KbDocument,
    Outline,
    Project,
    ProjectCollaborator,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
    Task,
    WorldSetting,
)
from app.models.entities import WORLD_CATEGORIES
from app.schemas.career import CareerCreate
from app.schemas.chapter import WizardRequest
from app.schemas.setting_fields import EntityRef
from app.schemas.story import CharacterOut, OutlineOut, WorldSettingOut
from app.services import (
    ai_config_service,
    authorization_service,
    career_service,
    collaboration_ai_guard,
    kb_service,
    outline_service,
    outline_snapshot_service,
    planning_lifecycle,
    project_access,
    project_ai_config,
    project_lifecycle,
    prompt_service,
    relationship_service,
    retrieval_service,
    series_policy,
    setting_ai_visibility,
    setting_field_service,
    story_service,
)
from app.services.ai_dispatch import (
    VisibilityPolicyError,
    VisibilityStamp,
    _wait_send_cleanup,
    authorize_send_actor,
    guard_provider,
    read_visibility_stamp,
    require_visibility_stamp,
    visibility_sessionmaker,
)
from app.services.ai_usage_recording import meter_provider
from app.services.cast_service import is_protagonist_role
from app.services.collaboration_task_access import GRANT_KEY, GrantIdentity, task_grant_identity
from app.services.entity_ai_service import normalize_aliases, normalize_career_assignments, normalize_category
from app.services.generation_service import Event
from app.services.project_service import get_execution_project, project_info
from app.services.setting_ai_context import build_scope, current_character_text, current_world_text
from app.services.task_engine.execution_identity import (
    TaskExecutionIdentity,
    advance_checkpoint,
    require_live_worker,
    require_write_execution,
)

logger = logging.getLogger(__name__)

WizardKind = Literal["outline", "characters", "world", "careers"]
# 追加模式下提示词附上的已有大纲条数（末尾）
APPEND_CONTEXT_OUTLINES = 10
# 可以从中断处继续的任务状态
RESUMABLE_STATUSES = ("failed", "cancelled", "interrupted")
# 续写开头先缓冲这么多字符再清洗输出（覆盖模型重复上一两个条目的情况）
CONTINUE_HEAD_CHARS = 600
# 续写开头与前缀末尾的重复至少这么长才去掉（避免误删一两个合法的括号）
MIN_OVERLAP = 8
_FENCE_RE = re.compile(r"^\s*```[A-Za-z]*\s*")
_WS_RE = re.compile(r"\s+")


@dataclass
class WizardJob:
    project_id: int
    kind: WizardKind
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None
    # 仅 outline：replace = 覆盖（先存快照）；append = 追加到末尾
    outline_mode: str = "replace"
    # 从中断处继续：旧任务截断后的部分输出；空串 = 正常生成
    prefix: str = ""
    resume_binding: "ResumeBinding | None" = None
    model_binding: project_ai_config.ResolvedProjectModel | None = None
    cipher: SecretCipher | None = None
    allow_private: bool = False
    execution: TaskExecutionIdentity | None = None
    visibility_stamp: VisibilityStamp | None = None
    # 续写：构建时可见性戳的 epoch（保存事务已核对来源 epoch == 该值）。保存后本任务自己的覆盖可能推进 epoch，
    # 保存后的核验（done 事件、终态、提交确认、done 帧发布 guard）拿来源 epoch 与此值比较，并另行要求
    # 保存后的新戳仍成立；发送前路径仍与当前 epoch 比较。非续写为 None。
    resume_epoch: int | None = None
    pending: "WizardPending | None" = None
    # 后台执行的发送守卫（领取身份 + 原模型绑定 + 续写来源）；协作执行在检索前与终态前据此锁内重验。
    guard: collaboration_ai_guard.SendGuard | None = None
    # 受控任务流 done 帧的仅发布 guard（E5-c3）：(绑定输入, guard)，按输入对象身份失效；不用于发送或写入。
    publication_guard: tuple[tuple[object, ...], collaboration_ai_guard.SendGuard] | None = None

    @property
    def task_execution_identity(self) -> TaskExecutionIdentity | None:
        return self.execution


@dataclass(frozen=True, slots=True, repr=False)
class WizardPending:
    original: TaskExecutionIdentity
    active: TaskExecutionIdentity
    marker: dict[str, Any]


@dataclass(slots=True)
class WizardCommit:
    execution: TaskExecutionIdentity | None = None
    stamp: VisibilityStamp | None = None
    payload: dict[str, Any] | None = None
    attempted: bool = False


def _wizard_uncertain() -> ConflictError:
    return ConflictError("向导保存结果尚未确认，请核对", extra={"code": "wizard_result_uncertain"})


def _result_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


async def begin_wizard_pending(
    maker: async_sessionmaker[AsyncSession], execution: TaskExecutionIdentity,
) -> WizardPending:
    """外部出站前固定原执行的未决凭据；确认丢失时不继续发送。"""
    checkpoint = deepcopy(execution.checkpoint or {})
    if "_wizard_pending" in checkpoint or "_wizard_completion" in checkpoint:
        raise _wizard_uncertain()
    marker = {
        "version": 1, "nonce": str(uuid4()), "task_key": execution.task_key,
        "execution_key": execution.execution_key, "queued_at": execution.queued_at.isoformat(),
        "actor_id": execution.actor_id, "project_id": execution.project_id, "kind": execution.kind,
        "input_hash": hashlib.sha256(execution.input_json.encode()).hexdigest(),
    }
    checkpoint["_wizard_pending"] = marker
    active = advance_checkpoint(execution, checkpoint)
    try:
        async with maker() as db:
            await require_write_execution(db, execution)
            result = await db.execute(update(Task).where(
                Task.id == execution.task_id, Task.statistics_key == execution.task_key,
                Task.queued_at == execution.queued_at, Task.execution_key == execution.execution_key,
                Task.worker_key == execution.worker_key,
                Task.status == "running", Task.admin_cancel_operation_id.is_(None),
            ).values(checkpoint=checkpoint).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise _wizard_uncertain()
            await db.commit()
    except Exception:
        raise _wizard_uncertain() from None
    return WizardPending(execution, active, marker)


async def _lock_original_task(db: AsyncSession, execution: TaskExecutionIdentity) -> Any:
    """系统清理/核对只触碰原任务断点：先锁作品父行（不改内容、不要求活动状态或当前授权），再锁原轮次 Task 行。"""
    with db.no_autoflush:
        locked = await db.execute(update(Project).where(Project.id == execution.project_id).values(
            id=Project.id, updated_at=Project.updated_at,
        ).execution_options(synchronize_session=False))
        if locked.rowcount != 1:
            return None
        row = (await db.execute(select(Task.checkpoint).where(
            Task.id == execution.task_id, Task.statistics_key == execution.task_key,
            Task.user_id == execution.actor_id, Task.project_id == execution.project_id, Task.kind == execution.kind,
            Task.queued_at == execution.queued_at, Task.execution_key == execution.execution_key,
            Task.worker_key == execution.worker_key,
            Task.status == "running", Task.admin_cancel_operation_id.is_(None),
        ).with_for_update())).one_or_none()
        if row is not None:
            await require_live_worker(db, execution.worker_key)
        return row


async def discard_wizard_pending(maker: async_sessionmaker[AsyncSession], pending: WizardPending) -> bool:
    """仅在明确尚未尝试结果提交时清理原凭据，不能清理替代任务。

    E4-g3c：系统清理不再要求当前授权/令牌。内容保存与凭据替换（pending → completion）在同一事务提交，
    调用方又只在保存从未尝试提交时调用，因此清理只撤销本轮自身的未决标记、不写作品数据；撤权、降级或改密后
    若仍要求原授权，凭据将无法清理，原任务会永久停在 running 并占用向导资源键。
    """
    try:
        async with maker() as db:
            row = await _lock_original_task(db, pending.active)
            if (row is None or not isinstance(row.checkpoint, dict)
                    or row.checkpoint.get("_wizard_pending") != pending.marker
                    or "_wizard_completion" in row.checkpoint):
                return False
            remaining = deepcopy(row.checkpoint)
            remaining.pop("_wizard_pending")
            if remaining != (pending.original.checkpoint or {}):
                return False
            result = await db.execute(update(Task).where(
                Task.id == pending.active.task_id, Task.statistics_key == pending.active.task_key,
                Task.queued_at == pending.active.queued_at, Task.execution_key == pending.active.execution_key,
                Task.worker_key == pending.active.worker_key,
                Task.status == "running", Task.admin_cancel_operation_id.is_(None),
            ).values(checkpoint=pending.original.checkpoint).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                return False
            await db.commit()
            return True
    except Exception:
        return False


def verify_wizard_completion(job: WizardJob, execution: TaskExecutionIdentity, result: object) -> None:
    """比对原未决凭据、同事务完成收据和拟发布的结果；不从重读 Task 重签。"""
    if job.pending is None or job.execution != execution or type(result) is not dict:
        raise _wizard_uncertain()
    checkpoint = execution.checkpoint
    if type(checkpoint) is not dict or "_wizard_pending" in checkpoint:
        raise _wizard_uncertain()
    receipt = checkpoint.get("_wizard_completion")
    if (type(receipt) is not dict or set(receipt) != set(job.pending.marker) | {"result", "result_hash", "stream_hash"}
            or any(type(receipt.get(key)) is not type(value) or receipt.get(key) != value
                   for key, value in job.pending.marker.items())
            or receipt.get("result") != result or receipt.get("result_hash") != _result_hash(result)):
        raise _wizard_uncertain()
    expected = deepcopy(job.pending.active.checkpoint or {})
    expected.pop("_wizard_pending", None)
    expected["_wizard_completion"] = receipt
    if expected != checkpoint:
        raise _wizard_uncertain()


_WIZARD_KINDS = frozenset({"wizard.outline", "wizard.characters", "wizard.world", "wizard.careers"})
_RECEIPT_FIELDS = frozenset({"version", "nonce", "task_key", "execution_key", "queued_at", "actor_id", "project_id",
                             "kind", "input_hash", "result", "result_hash", "stream_hash"})


def _marker_matches(marker: object, task: Task, *, fields: frozenset[str], execution_key: str | None) -> bool:
    """未决凭据 / 完成收据与原 Task 身份逐项一致；任何破损都视为不可证明。"""
    if type(marker) is not dict or set(marker) != fields or type(marker["version"]) is not int \
            or marker["version"] != 1:
        return False
    for key in ("nonce", "task_key", "execution_key"):
        if type(marker[key]) is not str or str(UUID(marker[key])) != marker[key]:
            return False
    queued = task.queued_at.replace(tzinfo=UTC) if task.queued_at.tzinfo is None else task.queued_at.astimezone(UTC)
    return not (marker["task_key"] != task.statistics_key or marker["execution_key"] == marker["task_key"]
                or (execution_key is not None and marker["execution_key"] != execution_key)
                or marker["queued_at"] != queued.isoformat() or type(marker["actor_id"]) is not int
                or marker["actor_id"] != task.user_id or type(marker["project_id"]) is not int
                or marker["project_id"] != task.project_id or marker["kind"] != task.kind
                or task.kind not in _WIZARD_KINDS or marker["input_hash"] != _result_hash(task.input))


def _bound_wizard_input(task: Task, project_owner_id: int | None) -> bool:
    """原任务输入的令牌证据、模型绑定与协作轮次自洽；协作者任务须由调用方提供数据库中的作品作者。"""
    actor_version = task.input.get("_task_actor_token_version")
    if (type(actor_version) is not int or actor_version < 0
            or type(task.input.get("_wizard_actor_token_version")) is not int
            or task.input["_wizard_actor_token_version"] != actor_version):
        return False
    grant = task_grant_identity(task.input)
    if GRANT_KEY in task.input:
        if grant is None or type(project_owner_id) is not int or project_owner_id == task.user_id:
            return False
        owner_id = project_owner_id
    else:
        owner_id = task.user_id
    binding = project_ai_config.deserialize_project_model(
        task.input.get("_project_model"), actor_id=task.user_id, project_id=task.project_id,
        project_owner_id=owner_id,
    )
    return bool(binding is not None and binding.model_type == "text" and binding.grant_identity == grant
                and type(task.input.get("ai_config_id")) is int and binding.config_id == task.input["ai_config_id"])


def verified_wizard_terminal_receipt(task: Task, *, project_owner_id: int | None = None) -> bool:
    """仅对成功终态提供可清理的持久证明；无证或损坏一律保留。

    协作者任务（E4-g3c）须由调用方从数据库传入作品作者，才能按原 grant 核对模型绑定。
    """
    try:
        if (not isinstance(task, Task) or task.status != "succeeded" or not isinstance(task.input, dict)
                or not isinstance(task.checkpoint, dict) or "_wizard_pending" in task.checkpoint
                or not isinstance(task.result, dict) or task.project_id is None
                or not isinstance(task.queued_at, datetime)):
            return False
        receipt = task.checkpoint.get("_wizard_completion")
        if (not _marker_matches(receipt, task, fields=_RECEIPT_FIELDS, execution_key=None)
                or task.execution_key not in (None, receipt["execution_key"])
                or receipt["result"] != task.result or receipt["result_hash"] != _result_hash(task.result)
                or receipt["stream_hash"] != hashlib.sha256((task.output_text or "").encode()).hexdigest()):
            return False
        return _bound_wizard_input(task, project_owner_id)
    except (AppError, ValueError, TypeError, OverflowError, RecursionError):
        return False


async def completed_wizard_receipt(db: AsyncSession, task: Task) -> dict[str, Any] | None:
    """原轮次仍为 running 的向导任务：在同一事务锁作品 → Task 后核验完成收据（E4-g3c 受限收尾）。

    内容替换与收据在同一事务提交（保存时 output_text 也一并写入），收据逐字可证即证明本轮结果已提交；不要求当前
    授权/令牌/可见性。返回收据中的结果（终态删除/裁剪证明要求与之逐字一致）；无法证明返回 None。
    """
    if (task.kind not in _WIZARD_KINDS or task.statistics_key is None or task.project_id is None
            or task.execution_key is None or not isinstance(task.queued_at, datetime)):
        return None
    with db.no_autoflush:
        locked = await db.execute(update(Project).where(Project.id == task.project_id).values(
            id=Project.id, updated_at=Project.updated_at,
        ).execution_options(synchronize_session=False))
        if locked.rowcount != 1:
            return None
        owner_id = await db.scalar(select(Project.user_id).where(Project.id == task.project_id))
        row = await db.scalar(select(Task).where(
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
            Task.project_id == task.project_id, Task.kind == task.kind, Task.queued_at == task.queued_at,
            Task.execution_key == task.execution_key, Task.worker_key == task.worker_key, Task.status == "running",
            Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
        ).with_for_update().execution_options(populate_existing=True))
    try:
        if (row is None or not isinstance(row.input, dict) or not isinstance(row.checkpoint, dict)
                or "_wizard_pending" in row.checkpoint):
            return None
        receipt = row.checkpoint.get("_wizard_completion")
        if (not _marker_matches(receipt, row, fields=_RECEIPT_FIELDS, execution_key=row.execution_key)
                or type(receipt["result"]) is not dict or receipt["result"].get("kind") != row.kind.removeprefix(
                    "wizard.")
                or receipt["result_hash"] != _result_hash(receipt["result"])
                or receipt["stream_hash"] != hashlib.sha256((row.output_text or "").encode()).hexdigest()
                or not _bound_wizard_input(row, owner_id)):
            return None
        return deepcopy(receipt["result"])
    except (AppError, ValueError, TypeError, OverflowError, RecursionError):
        return None


async def release_uncommitted_pending(db: AsyncSession, task: Task) -> bool:
    """启动恢复：原轮次 running 只带逐字可证的未决凭据（无完成收据）时，内容保存从未提交（二者同事务替换），
    只撤销本轮自身的标记以便按普通中断收尾；不写作品数据、不要求当前授权。调用方负责提交。"""
    if (task.kind not in _WIZARD_KINDS or task.statistics_key is None or task.project_id is None
            or task.execution_key is None or not isinstance(task.queued_at, datetime)):
        return False
    with db.no_autoflush:
        locked = await db.execute(update(Project).where(Project.id == task.project_id).values(
            id=Project.id, updated_at=Project.updated_at,
        ).execution_options(synchronize_session=False))
        if locked.rowcount != 1:
            return False
        row = await db.scalar(select(Task).where(
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
            Task.project_id == task.project_id, Task.kind == task.kind, Task.queued_at == task.queued_at,
            Task.execution_key == task.execution_key, Task.worker_key == task.worker_key, Task.status == "running",
            Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
        ).with_for_update().execution_options(populate_existing=True))
    try:
        if (row is None or not isinstance(row.input, dict) or not isinstance(row.checkpoint, dict)
                or "_wizard_completion" in row.checkpoint
                or not _marker_matches(row.checkpoint.get("_wizard_pending"), row,
                                       fields=_RECEIPT_FIELDS - {"result", "result_hash", "stream_hash"},
                                       execution_key=row.execution_key)):
            return False
    except (AppError, ValueError, TypeError, OverflowError, RecursionError):
        return False
    remaining = deepcopy(row.checkpoint)
    remaining.pop("_wizard_pending")
    changed = await db.execute(update(Task).where(
        Task.id == row.id, Task.statistics_key == row.statistics_key, Task.queued_at == row.queued_at,
        Task.execution_key == row.execution_key, Task.worker_key == task.worker_key, Task.status == "running",
        Task.admin_cancel_operation_id.is_(None),
    ).values(checkpoint=remaining or None).execution_options(synchronize_session=False))
    return changed.rowcount == 1


async def confirm_wizard_commit(
    maker: async_sessionmaker[AsyncSession], job: WizardJob, state: WizardCommit,
) -> bool:
    """只凭原执行体、同事务 receipt 和原模型重新确认已提交的结果。"""
    if (state.execution is None or state.stamp is None or state.payload is None
            or job.model_binding is None or job.cipher is None):
        return False
    previous = job.execution
    try:
        job.execution = state.execution
        async with maker() as db:
            await require_write_execution(db, state.execution)
            await require_visibility_stamp(db, state.stamp)
            if job.resume_binding is not None:
                # 保存后：新戳（state.stamp）仍成立 + 来源 epoch 等于构建戳 epoch，证明中间唯一变化是本次保存。
                if type(job.resume_epoch) is not int:
                    return False
                await verify_resume_source(
                    db, job.resume_binding, for_update=True, expected_epoch=job.resume_epoch,
                )
            await project_ai_config.revalidate_project_model(
                db, job.model_binding, cipher=job.cipher, allow_private=job.allow_private,
            )
            verify_wizard_completion(job, state.execution, {"kind": job.kind, **state.payload})
        return True
    except Exception:
        job.execution = previous
        return False


@dataclass(frozen=True, slots=True, repr=False)
class ResumeBinding:
    actor_id: int
    project_id: int
    kind: WizardKind
    task_id: int
    task_key: str
    fingerprint: str
    actor_token_version: int
    # E4-g3c：协作者续写只能接本人同一邀请轮次的来源（指纹已含来源 input 中的原 grant）；作者为 None。
    grant_identity: GrantIdentity | None = None


class WizardResumeChanged(VisibilityPolicyError):
    def __init__(self) -> None:
        super().__init__("原向导任务已变化，请重新选择", extra={"code": "wizard_resume_changed"})


# ---------------- 从中断处继续 ----------------


def trim_to_resume_point(text: str) -> str:
    """部分输出截到最后一个 `}`（丢掉写了一半的条目）；没有完整对象时返回空串。"""
    idx = text.rfind("}")
    return text[: idx + 1] if idx >= 0 else ""


def _json_head(text: str) -> str:
    """去掉代码块标记与全部空白后的开头，用来判断续写是否从头重来。"""
    return _WS_RE.sub("", _FENCE_RE.sub("", text, count=1))[:16]


def clean_continuation_head(prefix: str, head: str) -> tuple[str, bool]:
    """清洗续写开头：返回 (接在前缀后面的文本, 是否从头重来)。

    去掉代码块标记；模型从头输出完整 JSON → (原样, True)；去掉与前缀末尾重复的部分；
    前缀以 `}` 结尾而续写以 `{` 开头时补逗号。
    """
    body = _FENCE_RE.sub("", head, count=1)
    start = _json_head(prefix)
    if len(start) >= 2 and _WS_RE.sub("", body).startswith(start):
        return body, True
    body = body.lstrip()
    for k in range(len(body), MIN_OVERLAP - 1, -1):
        if prefix.endswith(body[:k]):
            body = body[k:].lstrip()
            break
    if body.startswith("{") and prefix.rstrip().endswith("}"):
        body = "," + body
    return body, False


async def load_resume_source(
    db: AsyncSession, *, user_id: int, project_id: int, kind: WizardKind, task_id: int, task_key: str,
    for_update: bool = False, grant_identity: GrantIdentity | None = None, expected_epoch: int | None = None,
) -> Task:
    """以客户端原 UUID 找本人同作品同类型任务，不采用复用后的整数 ID。

    来源的协作轮次须与本次执行逐字一致：作者只能接无 grant 的本人任务；协作者只能接本人同一邀请轮次的任务，
    撤权重邀后的旧轮次来源不能借新邀请续写（E4-g3c）。

    ``expected_epoch`` 为 None（受理、构建、发送前、保存事务内）时来源 epoch 与当前 epoch 比较；仅保存后的核验
    传入构建戳 epoch——本任务自己的覆盖保存可能已推进当前 epoch，调用方须同时要求保存后的新戳仍成立。
    """
    with db.no_autoflush:
        cached = db.identity_map.get(identity_key(Task, task_id))
        identity_fields = ("id", "statistics_key", "user_id", "project_id", "kind", "status", "input", "output_text")
        if cached is not None and any(inspect(cached).attrs[name].history.has_changes() for name in identity_fields):
            raise WizardResumeChanged()
        statement = select(Task).where(
            Task.id == task_id, Task.statistics_key == task_key, Task.user_id == user_id,
            Task.project_id == project_id, Task.kind == f"wizard.{kind}",
        ).execution_options(populate_existing=True)
        if for_update and db.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(read=True)
        task = await db.scalar(statement)
        if (task is None or not isinstance(task.input, dict)
                or (GRANT_KEY in task.input) != (grant_identity is not None)
                or task_grant_identity(task.input) != grant_identity):
            raise BadRequestError("要继续的向导任务不存在")
        if task.status not in RESUMABLE_STATUSES:
            raise BadRequestError("只有失败、已取消或已中断的向导任务可以从中断处继续")
        if not trim_to_resume_point(task.output_text or ""):
            raise BadRequestError("该任务没有可续写的部分输出，请重新生成")
        if expected_epoch is None:
            expected_epoch = (await read_visibility_stamp(db, project_id)).epoch
        elif type(expected_epoch) is not int or expected_epoch < 0:
            raise WizardResumeChanged()
        if task.input.get("ai_history_epoch", 0) != expected_epoch:
            raise ConflictError("AI 可见设置已收紧，旧向导结果不能继续发送，请重新生成", extra={
                "code": "ai_visibility_changed",
            })
        return task


def _resume_fingerprint(source: Task) -> str:
    """比较原输出及生成参数，防止同一个任务身份的内容被迟到写入替换。"""
    payload = {
        "id": source.id, "key": source.statistics_key, "user": source.user_id, "project": source.project_id,
        "kind": source.kind, "status": source.status, "input": source.input, "output": source.output_text,
        "queued_at": source.queued_at.isoformat() if source.queued_at is not None else None,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def bind_resume_source(source: Task) -> dict[str, Any]:
    return {"version": 1, "task_id": source.id, "task_key": source.statistics_key,
            "fingerprint": _resume_fingerprint(source)}


def parse_resume_binding(
    payload: Any, req: WizardRequest, *, user_id: int, project_id: int, kind: WizardKind,
    grant_identity: GrantIdentity | None = None,
) -> ResumeBinding | None:
    raw = payload.get("_wizard_resume") if isinstance(payload, dict) else None
    if req.continue_from_task_id is None:
        if raw is not None:
            raise WizardResumeChanged()
        return None
    actor_version = payload.get("_wizard_actor_token_version")
    if (type(actor_version) is not int or actor_version < 0
            or not isinstance(raw, dict) or set(raw) != {"version", "task_id", "task_key", "fingerprint"}
            or type(raw["version"]) is not int or raw["version"] != 1
            or type(raw["task_id"]) is not int or raw["task_id"] != req.continue_from_task_id
            or raw["task_key"] != req.continue_from_task_key
            or not isinstance(raw["fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", raw["fingerprint"])):
        raise WizardResumeChanged()
    return ResumeBinding(user_id, project_id, kind, raw["task_id"], raw["task_key"], raw["fingerprint"], actor_version,
                         grant_identity)


async def verify_resume_source(
    db: AsyncSession, binding: ResumeBinding, *, for_update: bool = False, expected_epoch: int | None = None,
) -> Task:
    """``expected_epoch`` 只供保存后的核验传入构建戳 epoch（见 load_resume_source）；其余路径保持 None。"""
    try:
        with db.no_autoflush:
            if binding.grant_identity is None:
                statement = select(Project.id).where(
                    Project.id == binding.project_id, Project.user_id == binding.actor_id,
                    *project_lifecycle.active_conditions(),
                )
                if for_update and db.get_bind().dialect.name == "postgresql":
                    statement = statement.with_for_update(read=True)
                if await db.scalar(statement) is None:
                    raise WizardResumeChanged()
            else:
                # 协作者来源：作品仍活动，且真实 actor 仍以原邀请轮次持 ai.use（不借当前邀请补签）。
                await project_access.require_project(
                    db, binding.actor_id, binding.project_id, "ai.use", grant_identity=binding.grant_identity,
                )
            source = await load_resume_source(
                db, user_id=binding.actor_id, project_id=binding.project_id, kind=binding.kind,
                task_id=binding.task_id, task_key=binding.task_key, for_update=for_update,
                grant_identity=binding.grant_identity, expected_epoch=expected_epoch,
            )
            token = source.input.get("_wizard_actor_token_version")
            if (type(token) is not int or token != binding.actor_token_version
                    or _resume_fingerprint(source) != binding.fingerprint):
                raise WizardResumeChanged()
            return source
    except VisibilityPolicyError:
        raise
    except Exception:
        # 身份、SQL 和存储错误均失败关闭，不能降级为普通向量故障或泄露内部参数。
        raise WizardResumeChanged() from None


def resume_request(source: Task, req: WizardRequest) -> WizardRequest:
    """续写沿用旧任务的原请求（卷数、章数、模式、补充指令等）；AI 配置以本次选择为准。"""
    params = dict(source.input.get("request") or {})
    params.pop("project_id", None)
    if req.ai_config_id is not None:
        params["ai_config_id"] = req.ai_config_id
    params["continue_from_task_id"] = source.id
    params["continue_from_task_key"] = source.statistics_key
    return WizardRequest.model_validate(params)


async def create_wizard_task(db: AsyncSession, engine, user_id: int, kind: str, params: dict[str, Any]):
    """任何写锁前完成能力短读、模型预检与 DNS（受理证明），再交中央受理在同一写事务内消费（E4-g3c）。

    作者与 accepted editor 共用（同 E4-g3a/b）：本人模型优先，仅本人无文本配置且作者授权时回退作者配置，
    本人配置无效不回退；viewer 403、待接受/非成员/已撤权/仅管理员身份 404，均不建任务、不调用模型。
    续写且未显式选择模型时，预检沿用本人来源任务原请求的配置（与 resume_request 的语义一致）。
    """
    from app.services import task_service
    from app.services.task_engine.admission import prepare_task_admission

    project_id = params.get("project_id")
    if type(project_id) is not int or project_id <= 0:
        raise BadRequestError("项目不存在")
    actor = authorization_service.actor_snapshot(db, user_id)
    if (type(actor) is not authorization_service.AuthenticatedActor or actor.id != user_id
            or type(actor.token_version) is not int or actor.token_version < 0 or db.new or db.dirty or db.deleted):
        raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    # 请求认证产生的只读事务在 DNS 前结束，不占用连接或锁。
    await db.rollback()
    ctx = engine.ctx
    config_id = params.get("ai_config_id")
    wkind: WizardKind = kind.removeprefix("wizard.")  # type: ignore[assignment]
    async with ctx.sessionmaker() as read:
        await authorization_service.require_authenticated(read, actor)
        project = await project_access.require_project(read, actor.id, project_id, "ai.use")
        grant: GrantIdentity | None = None
        if project.user_id != actor.id:
            member = await read.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == actor.id,
            ))
            grant = (member.invitation_id, member.revision) if member is not None else None
        # 无锁预检，保持原受理的报错顺序（续写来源 → 覆盖冲突 → 模型）；受理锁内仍完整重验，此处结论不授权。
        req = WizardRequest.model_validate(params)
        if req.continue_from_task_id is not None:
            source = await load_resume_source(
                read, user_id=actor.id, project_id=project_id, kind=wkind, task_id=req.continue_from_task_id,
                task_key=req.continue_from_task_key, grant_identity=grant,
            )
            version = source.input.get("_wizard_actor_token_version")
            if type(version) is not int or version != actor.token_version:
                raise BadRequestError("要继续的向导任务不存在")
            req = resume_request(source, req)
            # 续写且未显式选模型：预检沿用来源原请求的配置（与 resume_request 语义一致）。
            if config_id is None and req.ai_config_id is not None:
                config_id = req.ai_config_id
        await validate_wizard_replacement(read, project_id=project_id, kind=wkind, req=req)
    if config_id != params.get("ai_config_id"):
        params = {**params, "ai_config_id": config_id}
    model = await project_ai_config.preflight_project_model(
        ctx.sessionmaker, actor, project_id, "text", cipher=ctx.cipher,
        config_id=config_id, allow_private=ctx.settings.allow_private_ai_endpoints,
    )
    proof = await prepare_task_admission(ctx, actor, kind, params, model_preflight=model)
    return await task_service.create_task(db, engine, user_id, kind, params, admission=proof)


async def lock_wizard_project(
    db: AsyncSession, *, user_id: int, project_id: int, accepted_model: project_ai_config.ResolvedProjectModel,
) -> Project:
    """受理事务内：按中央接受绑定的原 grant 在 认证 → 发送写门禁 → 作品 屏障内重验 ai.use，并核对绑定与作者。"""
    binding = accepted_model
    if type(binding) is not project_ai_config.ResolvedProjectModel:
        raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
    with db.no_autoflush:
        project = await project_access.require_project(
            db, user_id, project_id, "ai.use", for_update=True, visibility_write=True,
            grant_identity=binding.grant_identity,
        )
    if (binding.actor_id != user_id or binding.project_id != project_id
            or binding.project_owner_id != project.user_id or binding.model_type != "text"
            # 作者本人只可能使用本人配置；作者配置回退（source=owner）只属于带原 grant 的协作者。
            or (binding.grant_identity is None and (binding.source != "actor" or project.user_id != user_id))
            or (binding.grant_identity is not None and project.user_id == user_id)):
        raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
    return project


async def validate_wizard_replacement(
    db: AsyncSession, *, project_id: int, kind: WizardKind, req: WizardRequest,
) -> None:
    """受理时的覆盖预检（调用方持项目写锁）；大纲历史路径在保存时才做级联校验，保持任务受理及错误事件语义。"""
    if kind != "outline":
        await _require_safe_replacement(db, project_id, kind, outline_mode=req.outline_mode)


_WIZARD_DELETE_FKS = frozenset({
    ("career_stages", "character_careers", "stage_id", "id", ""),
    ("careers", "career_history", "career_id", "id", "CASCADE"),
    ("careers", "career_stages", "career_id", "id", "CASCADE"),
    ("careers", "character_careers", "career_id", "id", "CASCADE"),
    ("careers", "planning_unit_careers", "career_id", "id", "CASCADE"),
    ("careers", "setting_reference_handles", "career_id", "id", "NO ACTION"),
    ("careers", "setting_thumbnail_bindings", "career_id", "id", "CASCADE"),
    ("careers", "setting_thumbnail_bindings", "project_id", "project_id", "CASCADE"),
    ("careers", "setting_timelines", "career_id", "id", "CASCADE"),
    ("chapter_plans", "chapters", "plan_id", "id", "SET NULL"),
    ("chapter_plans", "planning_units", "plan_id", "id", "SET NULL"),
    ("characters", "career_history", "character_id", "id", "CASCADE"),
    ("characters", "chapter_characters", "character_id", "id", "CASCADE"),
    ("characters", "character_careers", "character_id", "id", "CASCADE"),
    ("characters", "foreshadow_characters", "character_id", "id", "CASCADE"),
    ("characters", "organization_members", "character_id", "id", "CASCADE"),
    ("characters", "planning_unit_characters", "character_id", "id", "CASCADE"),
    ("characters", "planning_units", "pov_character_id", "id", "SET NULL"),
    ("characters", "relationships", "a_id", "id", "CASCADE"),
    ("characters", "relationships", "b_id", "id", "CASCADE"),
    ("characters", "series_entry_uses", "character_id", "id", ""),
    ("characters", "series_entry_uses", "project_id", "project_id", ""),
    ("characters", "setting_reference_handles", "character_id", "id", "NO ACTION"),
    ("characters", "setting_thumbnail_bindings", "character_id", "id", "CASCADE"),
    ("characters", "setting_thumbnail_bindings", "project_id", "project_id", "CASCADE"),
    ("characters", "setting_timelines", "character_id", "id", "CASCADE"),
    ("characters", "setting_timelines", "related_character_id", "id", "CASCADE"),
    ("outlines", "chapter_plans", "outline_id", "id", "CASCADE"),
    ("outlines", "chapters", "outline_id", "id", "SET NULL"),
    ("relationships", "relationship_history", "relationship_id", "id", "CASCADE"),
    ("setting_reference_handles", "setting_ai_links", "project_id", "project_id", "CASCADE"),
    ("setting_reference_handles", "setting_ai_links", "source_handle_id", "id", "CASCADE"),
    ("setting_reference_handles", "setting_ai_links", "target_handle_id", "id", "CASCADE"),
    ("setting_progressions", "setting_progressions", "superseded_by_id", "id", "SET NULL"),
    ("setting_text_fields", "setting_progressions", "custom_field_id", "id", ""),
    ("setting_timelines", "planning_unit_relationships", "timeline_id", "id", "CASCADE"),
    ("setting_timelines", "setting_progressions", "timeline_id", "id", "CASCADE"),
    ("setting_timelines", "setting_text_fields", "timeline_id", "id", "CASCADE"),
    ("world_settings", "planning_unit_world", "world_id", "id", "CASCADE"),
    ("world_settings", "series_entry_uses", "project_id", "project_id", ""),
    ("world_settings", "series_entry_uses", "world_setting_id", "id", ""),
    ("world_settings", "setting_reference_handles", "world_setting_id", "id", "NO ACTION"),
    ("world_settings", "setting_thumbnail_bindings", "world_setting_id", "id", "CASCADE"),
    ("world_settings", "setting_thumbnail_bindings", "project_id", "project_id", "CASCADE"),
    ("world_settings", "setting_timelines", "world_setting_id", "id", "CASCADE"),
})


def _wizard_delete_fk_graph() -> frozenset[tuple[str, str, str, str, str]]:
    # 句柄虽以 NO ACTION 指向目标，但本操作会显式删除无引用句柄。
    pending = {"characters", "world_settings", "careers", "outlines", "setting_reference_handles"}
    visited, edges = set(), set()
    while pending:
        parent = pending.pop()
        if parent in visited:
            continue
        visited.add(parent)
        for table in Base.metadata.tables.values():
            for fk in table.foreign_keys:
                if fk.column.table.name == parent:
                    action = (fk.ondelete or "").upper()
                    edges.add((parent, table.name, fk.parent.name, fk.column.name, action))
                    if action == "CASCADE":
                        pending.add(table.name)
    return frozenset(edges)


async def _assert_replacement_isolation(db: AsyncSession, project_id: int, kind: WizardKind) -> None:
    """覆盖目标及级联引用均须能证明属于当前作品；新外键未经审查一律阻止。"""
    if _wizard_delete_fk_graph() != _WIZARD_DELETE_FKS:
        raise ConflictError("向导关联结构已变化，无法安全覆盖", extra={"code": "wizard_reference_invalid"})
    tables = Base.metadata.tables
    target = tables[{"characters": "characters", "world": "world_settings", "careers": "careers",
                     "outline": "outlines"}[kind]]
    ids = select(target.c.id).where(target.c.project_id == project_id)
    checks = []

    def owner_ref(child_name: str, target_column: str, owner_name: str, owner_column: str):
        child, owner = tables[child_name], tables[owner_name]
        return select(1).select_from(child.outerjoin(owner, child.c[owner_column] == owner.c.id)).where(
            child.c[target_column].in_(ids), owner.c.project_id.is_distinct_from(project_id),
        )

    if kind == "characters":
        for child, owner, owner_col in (
            ("chapter_characters", "chapters", "chapter_id"),
            ("foreshadow_characters", "foreshadows", "foreshadow_id"),
            ("organization_members", "organizations", "organization_id"),
            ("planning_unit_characters", "planning_units", "unit_id"),
        ):
            checks.append(owner_ref(child, "character_id", owner, owner_col))
        unit, relations = tables["planning_units"], tables["relationships"]
        checks.append(select(1).where(unit.c.pov_character_id.in_(ids), unit.c.project_id.is_distinct_from(project_id)))
        a, b = target.alias("wizard_relation_a"), target.alias("wizard_relation_b")
        checks.append(select(1).select_from(relations.outerjoin(a, relations.c.a_id == a.c.id)
                      .outerjoin(b, relations.c.b_id == b.c.id)).where(
            or_(relations.c.a_id.in_(ids), relations.c.b_id.in_(ids)),
            or_(relations.c.project_id.is_distinct_from(project_id), a.c.project_id.is_distinct_from(project_id),
                b.c.project_id.is_distinct_from(project_id)),
        ))
    elif kind in ("world", "careers"):
        child = "planning_unit_world" if kind == "world" else "planning_unit_careers"
        column = "world_id" if kind == "world" else "career_id"
        checks.append(owner_ref(child, column, "planning_units", "unit_id"))
    else:
        plans, chapters, units = tables["chapter_plans"], tables["chapters"], tables["planning_units"]
        plan_ids = select(plans.c.id).where(plans.c.outline_id.in_(ids))
        checks.extend([
            select(1).where(plans.c.outline_id.in_(ids), plans.c.project_id.is_distinct_from(project_id)),
            select(1).where(or_(chapters.c.outline_id.in_(ids), chapters.c.plan_id.in_(plan_ids)),
                            chapters.c.project_id.is_distinct_from(project_id)),
            select(1).where(units.c.plan_id.in_(plan_ids), units.c.project_id.is_distinct_from(project_id)),
            select(1).select_from(plans.outerjoin(chapters, plans.c.chapter_id == chapters.c.id)).where(
                plans.c.id.in_(plan_ids), plans.c.chapter_id.is_not(None),
                chapters.c.project_id.is_distinct_from(project_id),
            ),
        ])

    if kind in ("characters", "careers"):
        characters, careers = tables["characters"], tables["careers"]
        assignments, stages, history = tables["character_careers"], tables["career_stages"], tables["career_history"]
        target_column = "character_id" if kind == "characters" else "career_id"
        assignment_target = assignments.c[target_column].in_(ids)
        if kind == "careers":
            stage_ids = select(stages.c.id).where(stages.c.career_id.in_(ids))
            assignment_target = or_(assignment_target, assignments.c.stage_id.in_(stage_ids))
        assignment_owners = (assignments.outerjoin(characters, assignments.c.character_id == characters.c.id)
                             .outerjoin(careers, assignments.c.career_id == careers.c.id)
                             .outerjoin(stages, assignments.c.stage_id == stages.c.id))
        checks.append(select(1).select_from(assignment_owners).where(
            assignment_target,
            or_(characters.c.project_id.is_distinct_from(project_id), careers.c.project_id.is_distinct_from(project_id),
                stages.c.career_id.is_distinct_from(assignments.c.career_id)),
        ))
        checks.append(select(1).select_from(history.outerjoin(characters, history.c.character_id == characters.c.id)
                      .outerjoin(careers, history.c.career_id == careers.c.id)).where(
            history.c[target_column].in_(ids), or_(characters.c.project_id.is_distinct_from(project_id),
                (history.c.career_id.is_not(None) & careers.c.project_id.is_distinct_from(project_id))),
        ))

    if kind != "outline":
        ref_column = {"characters": "character_id", "world": "world_setting_id", "careers": "career_id"}[kind]
        bindings, resources = tables["setting_thumbnail_bindings"], tables["setting_image_resources"]
        # 只允许本作品的单主体绑定级联；保留不可变图片资源供历史或撤销使用。
        # 损坏/旧库中的跨作品主体或资源不得因覆盖被默默删除。
        checks.append(select(1).select_from(
            bindings.outerjoin(resources, bindings.c.image_resource_id == resources.c.id)
        ).where(
            bindings.c[ref_column].in_(ids),
            or_(
                bindings.c.project_id.is_distinct_from(project_id),
                bindings.c.series_id.is_not(None),
                bindings.c.series_entry_id.is_not(None),
                resources.c.project_id.is_distinct_from(project_id),
                resources.c.series_id.is_not(None),
                *(bindings.c[column].is_not(None) for column in (
                    "character_id", "world_setting_id", "organization_id", "career_id",
                ) if column != ref_column),
            ),
        ))
        handles, timelines = tables["setting_reference_handles"], tables["setting_timelines"]
        predicates = [timelines.c[ref_column].in_(ids)]
        if kind == "characters":
            predicates.append(timelines.c.related_character_id.in_(ids))
        timeline_ids = select(timelines.c.id).where(or_(*predicates))
        handle_ids = select(handles.c.id).where(handles.c[ref_column].in_(ids))
        checks.extend([
            select(1).where(handles.c[ref_column].in_(ids), handles.c.project_id.is_distinct_from(project_id)),
            select(1).where(or_(*predicates), timelines.c.project_id.is_distinct_from(project_id)),
        ])
        # 时间线的主体可由不同类型组合而成，逐个验证实体确属本作品。
        for column, table in (
            ("character_id", "characters"), ("related_character_id", "characters"),
            ("world_setting_id", "world_settings"), ("career_id", "careers"),
            ("organization_id", "organizations"),
        ):
            subject = tables[table].alias(f"wizard_subject_{column}")
            joined = timelines.outerjoin(subject, timelines.c[column] == subject.c.id)
            checks.append(select(1).select_from(joined).where(
                or_(*predicates), timelines.c[column].is_not(None), subject.c.project_id.is_distinct_from(project_id),
            ))
        fields, events = tables["setting_text_fields"], tables["setting_progressions"]
        field_ids = select(fields.c.id).where(fields.c.timeline_id.in_(timeline_ids))
        event_ids = select(events.c.id).where(events.c.timeline_id.in_(timeline_ids))
        checks.extend([
            select(1).where(events.c.custom_field_id.in_(field_ids), events.c.timeline_id.not_in(timeline_ids)),
            select(1).where(events.c.superseded_by_id.in_(event_ids), events.c.timeline_id.not_in(timeline_ids)),
        ])
        # 字段/历史以 JSON 保存句柄而非 FK，包括停用历史；不能让其他作品留下悬空引用。
        string_ids = select(cast(handles.c.id, String)).where(handles.c[ref_column].in_(ids))
        for data, value in ((fields, fields.c.base_value), (events, events.c.value)):
            checks.append(select(1).select_from(data.outerjoin(timelines, data.c.timeline_id == timelines.c.id)).where(
                timelines.c.project_id.is_distinct_from(project_id),
                cast(value["reference_handle_id"].as_string(), String).in_(string_ids),
            ))
        links = tables["setting_ai_links"]
        checks.append(select(1).where(
            or_(links.c.source_handle_id.in_(handle_ids), links.c.target_handle_id.in_(handle_ids)),
            links.c.project_id.is_distinct_from(project_id),
        ))
        associations, units = tables["planning_unit_relationships"], tables["planning_units"]
        checks.append(select(1).select_from(associations.outerjoin(units, associations.c.unit_id == units.c.id)).where(
            associations.c.timeline_id.in_(timeline_ids), units.c.project_id.is_distinct_from(project_id),
        ))

    with db.no_autoflush:
        for check in checks:
            if await db.scalar(check.limit(1)) is not None:
                if kind == "outline":
                    raise ConflictError(
                        "大纲关联数据不一致，无法执行操作", extra={"code": "outline_reference_conflict"},
                    )
                raise ConflictError("向导覆盖存在无法安全处理的关联", extra={"code": "wizard_reference_invalid"})


async def _require_safe_replacement(
    db: AsyncSession, project_id: int, kind: WizardKind, *, outline_mode: str = "replace",
) -> list[EntityRef]:
    """调用方持项目写锁；避免覆盖及其级联删除丢失现存变化和自定义字段。"""
    if kind != "outline" or outline_mode == "replace":
        await _assert_replacement_isolation(db, project_id, kind)
    if kind == "characters":
        model, entry_kind = Character, "character"
        affected = or_(SettingTimeline.character_id.is_not(None), SettingTimeline.related_character_id.is_not(None))
    elif kind == "world":
        model, entry_kind = WorldSetting, "world"
        affected = SettingTimeline.world_setting_id.is_not(None)
    elif kind == "careers":
        model, entry_kind = Career, "career"
        affected = SettingTimeline.career_id.is_not(None)
    else:
        return []
    if entry_kind in ("character", "world") and await series_policy.used_local_ids(db, project_id, entry_kind):
        raise ConflictError(
            "本书已采用系列设定，不能用向导整体覆盖此类型",
            extra={"code": "series_setting_managed"},
        )
    targets = [EntityRef(kind=entry_kind, id=entry_id) for entry_id in await db.scalars(
        select(model.id).where(model.project_id == project_id)
    )]
    await setting_field_service.assert_targets_unreferenced(db, project_id, targets)
    progression = select(SettingProgression.id).where(
        SettingProgression.timeline_id == SettingTimeline.id, SettingProgression.active.is_(True)
    ).exists()
    custom_field = select(SettingTextField.id).where(SettingTextField.timeline_id == SettingTimeline.id).exists()
    conflict = await db.scalar(
        select(SettingTimeline.id)
        .where(SettingTimeline.project_id == project_id, affected, or_(progression, custom_field))
        .limit(1)
    )
    if conflict is not None:
        raise ConflictError(
            "覆盖会删除设定的剧情变化或自定义字段，请先在设定详情中处理这些记录",
            extra={"code": "wizard_history_conflict"},
        )
    return targets


async def build_wizard_job(
    db: AsyncSession,
    *,
    user_id: int,
    project_id: int,
    kind: WizardKind,
    req: WizardRequest,
    ai_config_id: int | None,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    resume_binding: ResumeBinding | None = None,
    model_binding: project_ai_config.ResolvedProjectModel | None = None,
    shared_embedding: project_ai_config.ResolvedSharedEmbedding | None = None,
    execution: TaskExecutionIdentity | None = None,
    pending: WizardPending | None = None,
) -> WizardJob:
    """任务开始运行时按项目最新的世界观 / 角色 / 大纲组装 messages。

    ``shared_embedding``：受理时冻结的共享向量选择（E6-d 协作执行；E6-f 裁决 13 起作者任务同样冻结）。
    引擎执行无该选择（升级前入队 / 受理时作者本人向量配置本地不可用）时只用关键词；直调（无执行身份）
    不接受该参数。
    """
    grant = execution.grant_identity if execution is not None else None
    if shared_embedding is not None and (
        execution is None or shared_embedding.grant_identity != grant or shared_embedding.actor_id != user_id
        or shared_embedding.project_id != project_id
    ):
        raise ConflictError("向导原模型身份无法验证，请重新发起", extra={"code": "wizard_model_unavailable"})
    if execution is not None:
        if (execution.actor_id != user_id or execution.project_id != project_id
                or execution.kind != f"wizard.{kind}"
                or pending is None or pending.active != execution):
            raise ConflictError("向导任务执行身份已变化", extra={"code": "wizard_execution_changed"})
        # 作者与 accepted editor 共用（E4-g3c）：锁内按原领取身份重验令牌、原邀请轮次与作者模型授权。
        await require_write_execution(db, execution)
    # 作者（含无执行身份的直调）严格归属；协作者只按原领取 grant 读取 ai.use，不借当前邀请补签。
    project = await get_execution_project(db, user_id, project_id, execution)
    if (req.continue_from_task_id is None) != (resume_binding is None):
        raise WizardResumeChanged()
    prefix = ""
    if resume_binding is not None:
        if (resume_binding.actor_id != user_id or resume_binding.project_id != project_id or resume_binding.kind != kind
                or resume_binding.task_id != req.continue_from_task_id
                or resume_binding.task_key != req.continue_from_task_key
                or resume_binding.grant_identity != grant):
            raise WizardResumeChanged()
        source = await verify_resume_source(db, resume_binding)
        # 只使用本次重验通过的原输出，不在后续 await 后从 ORM 重新推断身份。
        prefix = trim_to_resume_point(source.output_text or "")
    if model_binding is not None:
        if (model_binding.actor_id != user_id or model_binding.project_id != project_id
                or model_binding.project_owner_id != project.user_id or model_binding.model_type != "text"
                or model_binding.config_id != ai_config_id or model_binding.grant_identity != grant
                # 作者本人只可能使用本人配置；作者配置回退（source=owner）只属于带原 grant 的协作者。
                or (grant is None and (model_binding.source != "actor" or project.user_id != user_id))):
            raise ConflictError("向导原模型身份无法验证，请重新发起", extra={"code": "wizard_model_unavailable"})
        await project_ai_config.revalidate_project_model(
            db, model_binding, cipher=cipher, allow_private=allow_private, for_update=True,
        )
    cfg = None
    if execution is None:
        cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    if model_binding is not None:
        # 配置取值在绑定锁内固定；构造 provider 或等待 DNS 前必须释放写锁。
        await db.commit()
    provider = None
    if execution is None:
        provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)

    info = await project_info(db, project, actor_user_id=user_id)
    kinds = {
        "outline": ("world", "character"),
        "characters": ("world", "career"),
        "careers": ("world",),
        "world": ("character",),
    }[kind]
    scope = await build_scope(
        db, project_id, texts=(format_project_info(info), req.instruction), kinds=kinds, legacy_all=True,
    )
    send_guard = None
    if execution is not None:
        if model_binding is None:
            raise ConflictError("向导原模型身份无法验证，请重新发起", extra={"code": "wizard_model_unavailable"})
        send_guard = collaboration_ai_guard.for_task(
            execution, model_binding, scope.stamp, cipher=cipher, allow_private=allow_private, resume=resume_binding,
            extra_bindings=(shared_embedding,) if shared_embedding is not None else (),
        )
        bound = await send_guard.prepare_provider(visibility_sessionmaker(db), factory, request_timeout=ai_timeout)
        # 模型取值来自原绑定锁内固定的配置快照（作者回退时协作者无权按本人读取作者配置行）。
        provider, model_name, thinking = bound.provider, bound.model, bound.thinking_level
        max_tokens = bound.max_output_k * ai_config_service.TOKENS_PER_K if bound.max_output_k else None
    else:
        model_name, thinking, max_tokens = cfg.model, cfg.thinking_level, ai_config_service.max_output_tokens(cfg)
    override = await prompt_service.get_override(db, user_id, f"wizard.{kind}")
    # 知识库参考资料：查询 = 作品信息 + 本步骤的额外要求
    query = "\n".join([format_project_info(info), req.instruction])

    async def authorize(send_db: AsyncSession) -> None:
        try:
            actor = authorization_service.AuthenticatedActor(user_id, resume_binding.actor_token_version)
            await authorize_send_actor(send_db, actor)
        except Exception:
            raise WizardResumeChanged() from None

    async def verify(send_db: AsyncSession) -> None:
        if resume_binding is None:
            raise WizardResumeChanged()
        await verify_resume_source(send_db, resume_binding, for_update=True)

    if execution is not None:
        # E4-g3c：引擎执行（作者与协作者共用）检索前按原领取身份锁内重验（令牌、原轮次、模型绑定、冻结的共享
        # 向量选择、续写来源），随即结束该写事务。E6-d（裁决 9）/ E6-f（裁决 13）：冻结选择为 vector 时用作者
        # 共享向量查询，每次发送经 for_task 守卫在租约内重验；否则（含无冻结选择的旧任务）只用关键词。
        # 协作者本人的向量配置永不参与。构造 provider 的普通故障按裁决 17 锁内复核后退回关键词。
        maker = visibility_sessionmaker(db)
        async with maker() as verify_db:
            await send_guard.require_task_write(verify_db)
            await verify_db.rollback()
        embedder = None
        if shared_embedding is not None and shared_embedding.mode == "vector":
            vector_guard = collaboration_ai_guard.for_task(
                execution, shared_embedding, scope.stamp, cipher=cipher, allow_private=allow_private,
                resume=resume_binding, extra_bindings=(model_binding,),
            )
            bound = await collaboration_ai_guard.prepare_shared_embedding(
                vector_guard, maker, factory, request_timeout=ai_timeout,
            )
            if bound is not None:
                # 裁决 1：只读作者旧键 provider:model:dim（作者与授权编辑者的索引任务都写这个空间）。
                embedder = retrieval_service.guard_embedder(
                    retrieval_service.Embedder(provider=bound.provider, provider_name=bound.provider_name,
                                               model=bound.model),
                    scope.stamp, maker,
                )
        knowledge = await kb_service.knowledge_text(db, project, query, embedder=embedder, purpose=f"向导：{kind}")
    elif resume_binding is None:
        knowledge = await kb_service.knowledge_for_project(
            db, project, query, cipher=cipher, factory=factory, ai_timeout=ai_timeout,
            allow_private=allow_private, purpose=f"向导：{kind}",
        )
    else:
        settings = await kb_service.load_settings(db, project.id)
        has_docs = await db.scalar(select(KbDocument.id).where(KbDocument.project_id == project.id).limit(1))
        knowledge = ""
        if settings.auto_inject and query.strip() and has_docs is not None:
            embedder = await retrieval_service.get_embedder(
                db, user_id, cipher=cipher, factory=factory, ai_timeout=ai_timeout, allow_private=allow_private,
                project_id=project.id,
            )
            embedder = retrieval_service.guard_embedder(
                embedder, scope.stamp, visibility_sessionmaker(db), authorize=authorize, verify=verify,
            )
            knowledge = await kb_service.knowledge_text(db, project, query, embedder=embedder, purpose=f"向导：{kind}")
    world_text = current_world_text(
        scope, await story_service.list_items(db, story_service.WORLD, project_id),
    ) if "world" in kinds else ""
    characters_text = current_character_text(
        scope, await story_service.list_items(db, story_service.CHARACTERS, project_id),
    ) if "character" in kinds else ""
    outline_text = story_service.format_outlines(await story_service.list_items(db, story_service.OUTLINES, project_id))

    if kind == "outline":
        append_kwargs: dict[str, Any] = {}
        if req.outline_mode == "append":
            outlines = await story_service.list_items(db, story_service.OUTLINES, project_id)
            if outlines:
                append_kwargs = {
                    "existing_text": story_service.format_outlines(outlines[-APPEND_CONTEXT_OUTLINES:]),
                    "start_volume_no": (await outline_service.last_volume(db, project_id))[0] + 1,
                    "start_chapter_no": await outline_service.next_chapter_no(db, project_id),
                }
        messages = build_outline_messages(
            info,
            volume_count=req.volume_count,
            chapters_per_volume=req.chapters_per_volume,
            world_text=world_text,
            characters_text=characters_text,
            instruction=req.instruction,
            knowledge=knowledge,
            override=override,
            **append_kwargs,
        )
    elif kind == "characters":
        messages = build_characters_messages(
            info,
            character_count=req.character_count,
            world_text=world_text,
            outline_text=outline_text,
            instruction=req.instruction,
            careers_text=await career_service.careers_reference(db, project_id, scope=scope),
            knowledge=knowledge,
            override=override,
        )
    elif kind == "careers":
        messages = build_careers_wizard_messages(
            info,
            career_count=req.career_count,
            world_text=world_text,
            instruction=req.instruction,
            knowledge=knowledge,
            override=override,
        )
    else:
        messages = build_world_messages(
            info,
            outline_text=outline_text,
            characters_text=characters_text,
            instruction=req.instruction,
            knowledge=knowledge,
            override=override,
        )
    if resume_binding is not None:
        messages = build_wizard_continue_messages(
            messages=messages,
            generated=prefix,
            override=await prompt_service.get_override(db, user_id, "wizard.continue"),
        )
    await scope.verify(db)
    if execution is None:
        maker = visibility_sessionmaker(db)
        if resume_binding is None:
            provider = guard_provider(provider, scope.stamp, maker)
        else:
            provider = guard_provider(provider, scope.stamp, maker, authorize=authorize, verify=verify)
        provider = meter_provider(
            provider, cfg, maker, actor_id=user_id, kind=f"wizard.{kind}",
            project_id=project.id, project_owner_id=project.user_id,
        )
    return WizardJob(
        project_id=project_id,
        kind=kind,
        provider=provider,
        model=model_name,
        messages=messages,
        thinking=thinking,  # type: ignore[arg-type]
        max_tokens=max_tokens,
        outline_mode=req.outline_mode,
        prefix=prefix,
        resume_binding=resume_binding,
        model_binding=model_binding,
        cipher=cipher if model_binding is not None else None,
        allow_private=allow_private,
        execution=execution,
        visibility_stamp=scope.stamp,
        resume_epoch=scope.stamp.epoch if resume_binding is not None else None,
        pending=pending,
        guard=send_guard,
    )


# ---------------- 解析与规范化 ----------------


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


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _unwrap_list(obj: Any, keys: tuple[str, ...]) -> list:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in keys:
            if isinstance(obj.get(key), list):
                return obj[key]
        # 只有一个列表字段时直接取
        lists = [v for v in obj.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    raise JSONExtractError("JSON 结构不符合预期")


def normalize_outline(obj: Any) -> list[dict]:
    items = _unwrap_list(obj, ("volumes", "outline", "outlines", "chapters"))
    rows: list[dict] = []
    if items and all(isinstance(v, dict) and isinstance(v.get("chapters"), list) for v in items):
        for vi, vol in enumerate(items, start=1):
            volume_no = _int(vol.get("volume_no"), vi)
            volume_title = _text(vol.get("volume_title") or vol.get("title"))[:200]
            for ch in vol["chapters"]:
                if isinstance(ch, dict):
                    rows.append({"volume_no": volume_no, "volume_title": volume_title, **_chapter_fields(ch)})
    else:
        for ch in items:
            if isinstance(ch, dict):
                rows.append(
                    {
                        "volume_no": _int(ch.get("volume_no"), 1),
                        "volume_title": _text(ch.get("volume_title"))[:200],
                        **_chapter_fields(ch),
                    }
                )
    rows = [r for r in rows if r["title"]]
    if not rows:
        raise JSONExtractError("未解析到任何大纲章节")
    # 章节号统一为全书连续编号，避免模型按卷重复编号
    for i, r in enumerate(rows, start=1):
        r["chapter_no"] = i
        r["sort"] = i
    return rows


def _chapter_fields(ch: dict) -> dict:
    return {
        "title": _text(ch.get("title") or ch.get("name"))[:200],
        "summary": _text(ch.get("summary") or ch.get("content") or ch.get("description")),
    }


def normalize_characters(obj: Any) -> list[dict]:
    items = _unwrap_list(obj, ("characters", "roles", "items"))
    rows = []
    for c in items:
        if not isinstance(c, dict):
            continue
        name = _text(c.get("name"))[:100]
        if not name:
            continue
        role_type = _text(c.get("role_type") or c.get("role"))[:50]
        rows.append(
            {
                "name": name,
                "role_type": role_type,
                "profile": _text(c.get("profile") or c.get("description")),
                # 旧格式的自然语言关系保留为旧关系备注
                "relations_text": _text(c.get("relations_text") or c.get("relations")),
                "aliases": [a for a in normalize_aliases(c.get("aliases")) if a != name],
                "age": _text(c.get("age"))[:50],
                "gender": _text(c.get("gender"))[:20],
                "appearance": _text(c.get("appearance")),
                "personality": _text(c.get("personality")),
                "background": _text(c.get("background")),
                "always_in_context": is_protagonist_role(role_type),
            }
        )
    if not rows:
        raise JSONExtractError("未解析到任何角色")
    for i, r in enumerate(rows, start=1):
        r["sort"] = i
    return rows


def normalize_character_relations(obj: Any) -> list[dict]:
    """角色向导的 relationships（可选）：「a 是 b 的 role_a，b 是 a 的 role_b」。缺两端或称呼的丢弃。"""
    items = obj.get("relationships") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return []
    rows = []
    for r in items:
        if not isinstance(r, dict):
            continue
        a = _text(r.get("a") or r.get("a_name") or r.get("from"))[:100]
        b = _text(r.get("b") or r.get("b_name") or r.get("to"))[:100]
        role_a = _text(r.get("role_a"))[:20]
        role_b = _text(r.get("role_b"))[:20] or role_a
        role_a = role_a or role_b
        if not a or not b or not role_a or a == b:
            continue
        rows.append(
            {
                "a": a,
                "b": b,
                "role_a": role_a,
                "role_b": role_b,
                "category": normalize_category(r.get("category")),
                "intimacy": max(-100, min(100, _int(r.get("intimacy"), 0))),
                "description": _text(r.get("description")),
            }
        )
    return rows


_WORLD_CATEGORY_ALIASES = (
    ("place", ("地点", "地理", "地域", "地图", "场景", "城市", "区域", "location")),
    ("faction", ("势力", "组织", "门派", "宗门", "国家", "阵营", "帮派", "种族")),
    ("item", ("物品", "道具", "法宝", "装备", "神器", "宝物", "artifact")),
    ("rule", ("规则", "体系", "力量", "法则", "科技", "制度", "修炼", "魔法", "system")),
    ("history", ("历史", "事件", "传说", "往事", "纪元", "年表")),
    ("other", ("其他",)),
)


def normalize_world_category(value: Any) -> str:
    v = _text(value).lower()
    if v in WORLD_CATEGORIES:
        return v
    for cat, keys in _WORLD_CATEGORY_ALIASES:
        if any(k in v for k in keys):
            return cat
    return "other"


def normalize_world(obj: Any) -> list[dict]:
    items = _unwrap_list(obj, ("world", "settings", "items", "world_settings"))
    rows = []
    for w in items:
        if not isinstance(w, dict):
            continue
        title = _text(w.get("title") or w.get("name"))[:200]
        if not title:
            continue
        rows.append(
            {
                "title": title,
                "content": _text(w.get("content") or w.get("description")),
                "category": normalize_world_category(w.get("category") or w.get("type")),
            }
        )
    if not rows:
        raise JSONExtractError("未解析到任何世界观条目")
    for i, r in enumerate(rows, start=1):
        r["sort"] = i
    return rows


async def _save_character_relations(
    db: AsyncSession, project_id: int, entities: list[Character], rels: list[dict]
) -> list[Any]:
    """按名字（再按别名）匹配新建的角色并写入关系；对不上 / 指向自己 / 同一对已有关系的跳过。"""
    lookup: dict[str, int] = {}
    for c in entities:
        lookup.setdefault(c.name, c.id)
    for c in entities:
        for a in c.aliases or []:
            lookup.setdefault(a, c.id)
    created = []
    for r in rels:
        a_id, b_id = lookup.get(r["a"]), lookup.get(r["b"])
        if a_id is None or b_id is None or a_id == b_id:
            continue
        if await relationship_service.find_pair(db, a_id, b_id) is not None:
            continue
        created.append(
            await relationship_service.create_relation(
                db,
                project_id,
                a_id=a_id,
                b_id=b_id,
                role_a=r["role_a"],
                role_b=r["role_b"],
                category=r["category"],
                intimacy=r["intimacy"],
                description=r["description"],
                source="wizard",
            )
        )
    return created


def normalize_character_careers(obj: Any) -> list[tuple[str, list[dict[str, Any]]]]:
    """角色向导输出中每个角色的 careers → [(角色名, [{name, stage, role, note}])]。"""
    try:
        items = _unwrap_list(obj, ("characters", "roles", "items"))
    except JSONExtractError:
        return []
    out = []
    for c in items:
        if not isinstance(c, dict):
            continue
        name = _text(c.get("name"))[:100]
        careers = normalize_career_assignments({"careers": c.get("careers")})
        if name and careers:
            out.append((name, careers))
    return out


async def _save_character_careers(
    db: AsyncSession, project_id: int, entities: list[Character], obj: Any
) -> tuple[list[CharacterCareer], list[str]]:
    """按职业名 / 阶段名匹配写入挂接；没有职业的项目直接跳过。返回 (挂接, 警告)。"""
    careers = await career_service.list_project_careers(db, project_id)
    if not careers:
        return [], []
    by_name = {c.name: c for c in careers}
    stages = await career_service.stages_of(db, [c.id for c in careers])
    lookup: dict[str, Character] = {}
    for e in entities:
        lookup.setdefault(e.name, e)
    created: list[CharacterCareer] = []
    warnings: list[str] = []
    for char_name, items in normalize_character_careers(obj):
        character = lookup.get(char_name)
        if character is None:
            continue
        for r in items:
            career = by_name.get(r["name"])
            if career is None:
                warnings.append(f"角色「{char_name}」的职业「{r['name']}」不存在，已忽略")
                continue
            ladder = stages.get(career.id, [])
            stage = next((s for s in ladder if s.name == r["stage"]), ladder[0] if ladder else None)
            if stage is None:
                continue
            if r["stage"] and stage.name != r["stage"]:
                warnings.append(
                    f"角色「{char_name}」的职业「{career.name}」没有名为「{r['stage']}」的阶段，"
                    f"已设为第 1 阶「{stage.name}」"
                )
            try:
                created.append(
                    await career_service.assign_new(
                        db, character, career, stage_id=stage.id, note=r["note"], source="wizard"
                    )
                )
            except AppError as exc:
                warnings.append(f"角色「{char_name}」的职业「{career.name}」未能挂接：{exc.detail}")
    return created, warnings


async def _save_careers(db: AsyncSession, project_id: int, obj: Any) -> dict[str, Any]:
    drafts, warnings = career_service.normalize_career_drafts(obj)
    if not drafts:
        raise JSONExtractError("未解析到任何职业")
    await db.execute(delete(Career).where(Career.project_id == project_id))
    careers = [
        await career_service.new_career(db, project_id, CareerCreate.model_validate({**d, "sort": i}))
        for i, d in enumerate(drafts, start=1)
    ]
    await planning_lifecycle.bump_revision(db, project_id)
    for c in careers:
        await db.refresh(c)
    outs = await career_service.career_outs(db, careers)
    return {"items": [o.model_dump(mode="json") for o in outs], "warnings": warnings}


_KIND_SPEC = {
    "outline": (Outline, normalize_outline, OutlineOut),
    "characters": (Character, normalize_characters, CharacterOut),
    "world": (WorldSetting, normalize_world, WorldSettingOut),
}


async def _prepare_outline_rows(db: AsyncSession, project_id: int, rows: list[dict], outline_mode: str) -> None:
    """大纲写入前：覆盖模式先存快照（已有大纲时）并删除旧大纲；追加模式把卷号 / 章节号 / 排序接到末尾。"""
    if outline_mode != "append":
        existing = await story_service.list_items(db, story_service.OUTLINES, project_id)
        if existing:
            await outline_snapshot_service.save_snapshot(db, project_id, "wizard_replace", "大纲向导覆盖前")
        await planning_lifecycle.detach_outlines(db, project_id)
        await db.execute(delete(Outline).where(Outline.project_id == project_id))
        return
    last_volume = (await outline_service.last_volume(db, project_id))[0]
    start_no = await outline_service.next_chapter_no(db, project_id)
    start_sort = await story_service.next_sort(db, Outline, project_id)
    volumes: dict[int, int] = {}
    for i, r in enumerate(rows):
        volumes.setdefault(r["volume_no"], last_volume + len(volumes) + 1)
        r["volume_no"] = volumes[r["volume_no"]]
        r["chapter_no"] = start_no + i
        r["sort"] = start_sort + i


async def save_wizard_result(
    db: AsyncSession, project_id: int, kind: WizardKind, raw_text: str, *, outline_mode: str = "replace",
    resume_binding: ResumeBinding | None = None,
    model_binding: project_ai_config.ResolvedProjectModel | None = None,
    cipher: SecretCipher | None = None,
    allow_private: bool = False,
    execution: TaskExecutionIdentity | None = None,
    visibility_stamp: VisibilityStamp | None = None,
    pending: WizardPending | None = None,
    commit_state: WizardCommit | None = None,
    output_text: str | None = None,
) -> dict[str, Any]:
    """解析模型输出并写入，返回 done 载荷 {items[, relationships, assignments], warnings?}。

    大纲覆盖模式（默认）与其他类型一样覆盖旧数据（覆盖前存大纲快照）；大纲追加模式不删旧数据，items 只含新增条目。
    """
    from app.services.ai_dispatch import lock_visibility_write

    # SAVEPOINT 会先 flush 待写对象；不可把未经本操作验证的调用方改动写到屏障之前。
    if db.new or db.dirty or db.deleted:
        raise ConflictError("存在未提交的修改，请先保存后重试")
    if resume_binding is not None and (
        resume_binding.project_id != project_id or resume_binding.kind != kind
        or (execution is not None and (
            resume_binding.actor_id != execution.actor_id
            or resume_binding.actor_token_version != execution.actor_token_version
            or resume_binding.grant_identity != execution.grant_identity
        ))
    ):
        raise WizardResumeChanged()
    if execution is not None:
        if (execution.project_id != project_id or execution.kind != f"wizard.{kind}"
                or pending is None or pending.active != execution
                or commit_state is None or type(output_text) is not str):
            raise ConflictError("向导任务执行身份已变化", extra={"code": "wizard_execution_changed"})
        await require_write_execution(db, execution)
        if visibility_stamp is None or visibility_stamp.project_id != project_id:
            raise ConflictError("向导可见性身份无法验证", extra={"code": "wizard_execution_changed"})
        await require_visibility_stamp(db, visibility_stamp)
    else:
        if resume_binding is not None:
            with db.no_autoflush:
                await authorization_service.require_authenticated(
                    db, authorization_service.AuthenticatedActor(
                        resume_binding.actor_id, resume_binding.actor_token_version,
                    ),
                    write=True,
                )
        if kind != "outline" or resume_binding is not None:
            await lock_visibility_write(db, project_id)
        await project_lifecycle.lock_active(db, project_id)
    if resume_binding is not None:
        await verify_resume_source(db, resume_binding, for_update=True)
    if model_binding is not None:
        if model_binding.project_id != project_id or cipher is None:
            raise ConflictError("向导原模型身份无法验证，请重新发起", extra={"code": "wizard_model_unavailable"})
        await project_ai_config.revalidate_project_model(db, model_binding, cipher=cipher, allow_private=allow_private)
    obj = extract_json(raw_text)
    # 父项目的外层物理事务已就位；内部任一步失败都撤销本次删除、创建、级联和快照。
    # 即使调用方捕获异常后提交外层事务，也不能保留失败操作的前半部分。
    async with db.begin_nested():
        targets = await _require_safe_replacement(db, project_id, kind, outline_mode=outline_mode)
        if kind != "outline":
            # 同时记录被整体替换的候选与其他类型的潜在竞争；仅在真实收紧时更新历史代次。
            from app.services.mentions.context import load_candidates

            previous_candidates = await load_candidates(db, project_id)
            previous_stamp = await read_visibility_stamp(db, project_id)
        await setting_field_service.cleanup_unreferenced_handles(db, project_id, targets)
        if kind == "careers":
            payload = await _save_careers(db, project_id, obj)
        else:
            model, normalize, out_schema = _KIND_SPEC[kind]
            rows = normalize(obj)
            if kind == "outline":
                await _prepare_outline_rows(db, project_id, rows, outline_mode)
            else:
                if kind == "characters":
                    character_ids = list(await db.scalars(
                        select(Character.id).where(Character.project_id == project_id)
                    ))
                    await planning_lifecycle.remove_character_references(
                        db, project_id, character_ids,
                    )
                await db.execute(delete(model).where(model.project_id == project_id))
            if kind == "characters":
                entities = [await story_service.new_character(db, project_id, row) for row in rows]
            elif kind == "world":
                entities = [await story_service.new_world(db, project_id, row) for row in rows]
            else:
                entities = [model(project_id=project_id, **row) for row in rows]
                db.add_all(entities)
                await db.flush()
            rels = []
            assignments: list[CharacterCareer] = []
            warnings: list[str] = []
            if kind == "characters":
                rels = await _save_character_relations(db, project_id, entities, normalize_character_relations(obj))
                assignments, warnings = await _save_character_careers(db, project_id, entities, obj)
            await planning_lifecycle.bump_revision(db, project_id)
            for entity in entities:
                await db.refresh(entity)
            payload = {"items": [out_schema.model_validate(e).model_dump(mode="json") for e in entities]}
            if kind == "outline":
                payload["mode"] = outline_mode
            if kind == "characters":
                outs = await relationship_service.to_outs(db, rels)
                payload["relationships"] = [o.model_dump(mode="json") for o in outs]
                for assignment in assignments:
                    await db.refresh(assignment)
                payload["assignments"] = [
                    o.model_dump(mode="json") for o in await career_service.assignment_outs(db, assignments)
                ]
                payload["warnings"] = warnings
        if kind != "outline":
            from app.services.card_service import _candidate_competition

            if targets or _candidate_competition(previous_candidates, await load_candidates(db, project_id)):
                # 句柄清理已删除链接时会自行收紧；相同事务中只推进一次历史代次。
                current_stamp = await read_visibility_stamp(db, project_id)
                if current_stamp.epoch == previous_stamp.epoch:
                    await setting_ai_visibility.bump_visibility_revision(db, project_id, tighten=True)
        if execution is not None:
            checkpoint = deepcopy(execution.checkpoint or {})
            if checkpoint.get("_wizard_pending") != pending.marker or "_wizard_completion" in checkpoint:
                raise _wizard_uncertain()
            checkpoint.pop("_wizard_pending")
            result_payload = {"kind": kind, **payload}
            checkpoint["_wizard_completion"] = {
                **pending.marker, "result": result_payload, "result_hash": _result_hash(result_payload),
                "stream_hash": hashlib.sha256(output_text.encode()).hexdigest(),
            }
            # 输出全文与收据同事务写入：撤权/重启后的受限收尾可仅凭持久事实证明 stream_hash（E4-g3c）。
            changed = await db.execute(update(Task).where(
                Task.id == execution.task_id, Task.statistics_key == execution.task_key,
                Task.queued_at == execution.queued_at, Task.execution_key == execution.execution_key,
                Task.worker_key == execution.worker_key,
                Task.status == "running", Task.admin_cancel_operation_id.is_(None),
            ).values(checkpoint=checkpoint, output_text=output_text).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise _wizard_uncertain()
            completed_execution = advance_checkpoint(execution, checkpoint)
            completed_stamp = await read_visibility_stamp(db, project_id)
    if commit_state is not None:
        if execution is not None:
            commit_state.execution = completed_execution
            commit_state.stamp = completed_stamp
            commit_state.payload = payload
        commit_state.attempted = True
    await db.commit()
    return payload


async def run_wizard_job(job: WizardJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    pieces: list[str] = []
    commit_state = WizardCommit()
    # 续写：开头先缓冲，清洗后再输出；正常生成直接输出
    pending: str | None = "" if job.prefix else None
    restarted = False
    try:
        if job.execution is None:
            raise ConflictError("向导任务执行身份已变化", extra={"code": "wizard_execution_changed"})
        if job.prefix:
            if job.resume_binding is None:
                raise WizardResumeChanged()
            async with sessionmaker() as db:
                await authorization_service.require_authenticated(
                    db,
                    authorization_service.AuthenticatedActor(
                        job.resume_binding.actor_id, job.resume_binding.actor_token_version,
                    ),
                )
                await verify_resume_source(db, job.resume_binding)
            yield "stage", {"stage": "从中断处继续"}
            yield "delta", {"text": job.prefix}
        stream_iter = job.provider.generate_stream(
            job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking
        )
        async with aclosing(stream_iter) as stream:
            async for piece in stream:
                if pending is None:
                    pieces.append(piece)
                    yield "delta", {"text": piece}
                    continue
                pending += piece
                if len(pending) >= CONTINUE_HEAD_CHARS:
                    text, restarted = clean_continuation_head(job.prefix, pending)
                    pending = None
                    if text:
                        pieces.append(text)
                        yield "delta", {"text": text}
        if pending:
            text, restarted = clean_continuation_head(job.prefix, pending)
            if text:
                pieces.append(text)
                yield "delta", {"text": text}
        generated = "".join(pieces)
        if not generated.strip():
            raise AIProviderError("模型未返回任何内容")
        raw = generated if restarted or not job.prefix else job.prefix + generated
        try:
            async with sessionmaker() as db:
                payload = await save_wizard_result(
                    db, job.project_id, job.kind, raw, outline_mode=job.outline_mode,
                    resume_binding=job.resume_binding, model_binding=job.model_binding, cipher=job.cipher,
                    allow_private=job.allow_private, execution=job.execution, visibility_stamp=job.visibility_stamp,
                    pending=job.pending, commit_state=commit_state,
                    output_text=job.prefix + generated,
                )
        except Exception:
            if not commit_state.attempted:
                raise
            if not await confirm_wizard_commit(sessionmaker, job, commit_state):
                raise _wizard_uncertain() from None
            payload = commit_state.payload
            if payload is None:
                raise _wizard_uncertain() from None
        job.execution = commit_state.execution
        job.visibility_stamp = commit_state.stamp
        yield "done", {"kind": job.kind, **payload}
    except JSONExtractError as exc:
        yield "error", {"detail": f"AI 输出解析失败：{exc}，请重新生成"}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except AppError as exc:
        yield "error", {"detail": exc.detail, **(exc.extra or {})}
    except Exception:  # noqa: BLE001
        logger.exception("向导生成失败 project=%s kind=%s", job.project_id, job.kind)
        yield "error", {"detail": "生成失败：服务器内部错误"}
    finally:
        pending = job.pending
        if not commit_state.attempted and pending is not None:
            async def cleanup_pending() -> None:
                if await discard_wizard_pending(sessionmaker, pending):
                    job.execution = pending.original

            await _wait_send_cleanup(cleanup_pending())
