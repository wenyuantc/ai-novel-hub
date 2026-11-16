"""章节 AI 生成 / 润色 / 按建议重写（作为后台任务 chapter.generate / chapter.polish / chapter.revise 运行）。

分三段：
- validate_and_claim：创建任务的请求内同步执行鉴权、配置解析、SSRF 与参数校验；单章在 Task flush 后绑定占用。
  任何业务错误（404/400/409）在此抛出，以普通 JSON 响应返回。
- build_chapter_job：任务开始运行时按最新数据组装 provider、上下文与 messages。
- run_chapter_job：流式阶段，使用独立会话落库；产出 (event, data) 事件，由任务引擎消费。
  成功时在同一事务内：ensure_baseline → 覆盖正文 → 写生成记录 → 写 generate/polish/revise 版本。
  未正常完成（模型报错、客户端断开等）时把章节状态回退为原状态，正文与版本不变，只留一条失败/取消记录。
- 整章生成可选自动续写：字数低于目标 CONTINUE_RATIO 时最多续写 MAX_CONTINUE_ROUNDS 轮，
  每轮开始前产出 continue 事件；续写轮报错时保留已生成正文照常落库，done 附带 warning。
- 按建议重写（revise）：原文全文 + 勾选的分析建议 + 补充要求 + 需保留的要素 + 与整章生成相同的上下文；
  analysis_id 须属于本章、suggestion_ids 须是该次分析里的建议；不自动续写；版本来源 / 生成记录类型均为 revise。
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.util import identity_key

from app.ai.prompts import (
    build_chapter_messages,
    build_continuation_messages,
    build_polish_messages,
    build_revise_messages,
)
from app.ai.prompts.registry import prompt_meta
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.ai.streaming import coalesce_buffered_text
from app.ai.templating import PromptOverride, TemplateRenderError
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import AIConfig, Chapter, ChapterAnalysis, ChapterVersion, GenerationLog, Project, Task, WritingStyle
from app.models.entities import ACTIVE_TASK_STATUSES
from app.schemas.chapter import ChapterOut, GenerateChapterRequest, PolishChapterRequest, ReviseChapterRequest
from app.schemas.scene_ai import ChapterTarget, SceneTarget
from app.services import (
    ai_config_service,
    ai_dispatch,
    authorization_service,
    chapter_claim,
    collaboration_ai_guard,
    generation_log_service,
    planning_context_service,
    project_access,
    project_ai_config,
    project_lifecycle,
    prompt_service,
    retrieval_service,
    scene_ai_context,
    scene_service,
    setting_ai_visibility,
    skill_service,
    style_service,
    version_service,
)
from app.services.ai_dispatch import guard_provider, visibility_sessionmaker
from app.services.chapter_claim import ChapterClaim as ChapterClaimIdentity
from app.services.chapter_service import count_words, get_owned_chapter, manual_chapter_views
from app.services.collaboration_ai_guard import _BoundModelProvider
from app.services.context_builder import build_chapter_context, build_setting_context, chapter_task
from app.services.project_service import get_owned_project
from app.services.task_engine.execution_identity import TaskExecutionIdentity, matches_running, matches_running_base

logger = logging.getLogger(__name__)

Event = tuple[Literal["delta", "continue", "done", "error", "stage", "paused"], dict]
Mode = Literal["generate", "polish", "revise"]
ChapterRequest = GenerateChapterRequest | PolishChapterRequest | ReviseChapterRequest

GENERATING_CONFLICT = "该章节正在生成中，请稍候再试"
BATCH_KIND = "chapter.batch"
BATCH_RANGE_CONFLICT = "该章节在批量生成范围内，请等待批量完成或暂停后再操作"

# 字数低于目标的该比例时自动续写
CONTINUE_RATIO = 0.85
MAX_CONTINUE_ROUNDS = 2
# 按有界批次交付模型文本，避免单字符碎片反复进入数据库事件校验和进度落库。
STREAM_BATCH_CHARS = 4096


@dataclass(frozen=True, slots=True, repr=False)
class _ConfigSnapshot:
    """provider、密钥与模型来自同一次读取；不要让 await 混入其他配置代次。"""

    id: int
    instance_key: str
    user_id: int
    name: str
    model_type: str
    provider: str
    base_url: str | None
    api_key_enc: str
    model: str
    thinking_level: str
    context_window_k: int | None
    max_output_k: int | None


def _snapshot_config(config: AIConfig) -> _ConfigSnapshot:
    return _ConfigSnapshot(
        id=config.id, instance_key=config.instance_key, user_id=config.user_id, name=config.name,
        model_type=config.model_type,
        provider=config.provider, base_url=config.base_url, api_key_enc=config.api_key_enc, model=config.model,
        thinking_level=config.thinking_level, context_window_k=config.context_window_k,
        max_output_k=config.max_output_k,
    )


@dataclass(frozen=True, slots=True, repr=False)
class _TransportSnapshot:
    provider: _BoundModelProvider
    inner: LLMProvider
    fingerprint: str


def _transport_fingerprint(provider: _BoundModelProvider) -> str:
    inner = provider._provider
    fields = (provider._model, provider._model_type, provider.api_key, provider.base_url, provider.timeout,
              provider.follow_redirects, inner.api_key, inner.base_url, inner.timeout, inner.follow_redirects)
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _snapshot_transport(provider: _BoundModelProvider) -> _TransportSnapshot:
    return _TransportSnapshot(provider, provider._provider, _transport_fingerprint(provider))


def _check_transport(snapshot: _TransportSnapshot) -> None:
    try:
        valid = (snapshot.provider._provider is snapshot.inner
                 and _transport_fingerprint(snapshot.provider) == snapshot.fingerprint)
    except Exception:
        valid = False
    if not valid:
        raise ai_dispatch.VisibilityPolicyError("模型配置已变化，已停止发送")


@dataclass
class ChapterJob:
    chapter_id: int
    prev_status: str
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None
    mode: Mode = "generate"
    project_id: int = 0
    ai_config_id: int | None = None
    provider_name: str = ""
    target_words: int = 0
    auto_continue: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    task_id: int | None = None
    task_key: str | None = None
    chapter_claim: ChapterClaimIdentity | None = None
    execution: TaskExecutionIdentity | None = field(default=None, repr=False)
    batch_attempt: collaboration_ai_guard.BatchAttemptIdentity | None = field(default=None, repr=False)
    model_binding: project_ai_config.ResolvedProjectModel | None = field(default=None, repr=False)
    embedding_selection: project_ai_config.ResolvedSharedEmbedding | None = field(default=None, repr=False)
    model_cipher: SecretCipher | None = field(default=None, repr=False)
    model_allow_private: bool = False
    model_transport: _TransportSnapshot | None = field(default=None, repr=False)
    run_key: str = field(default_factory=lambda: str(uuid4()))
    # 自动续写指令的用户覆盖（组装 job 时取出）
    continue_override: PromptOverride | None = None
    user_id: int = 0
    target: ChapterTarget | SceneTarget = field(default_factory=ChapterTarget)
    scenes: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    context_stamp: scene_ai_context.SceneContextStamp | None = None
    request: ChapterRequest | None = None
    raw_provider: LLMProvider | None = None
    context_budget: int = 4000
    override: PromptOverride | None = None
    style: str = ""
    skill_guide: str = ""
    embedder: Any = None
    suggestions: list[str] = field(default_factory=list)
    # 任务 handler 发布事件时复用的同一 SendGuard（受控任务流帧来源须固定同一 guard，E5-c1）。
    publication_guard: Any = field(default=None, repr=False)


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def stream_pieces(
    provider: LLMProvider,
    messages: list[ChatMessage],
    model: str,
    *,
    max_tokens: int | None,
    thinking: ThinkingLevel,
    pieces: list[str],
    prefix: str = "",
) -> AsyncIterator[Event]:
    """首片即时、后批有界合并后产出 delta；prefix 只在首个非空分块前发出。"""
    stream_iter = provider.generate_stream(messages, model, max_tokens=max_tokens, thinking=thinking)
    async with aclosing(coalesce_buffered_text(stream_iter, max_chars=STREAM_BATCH_CHARS)) as ready:
        async for piece in ready:
            if prefix:
                yield "delta", {"text": prefix}
                prefix = ""
            pieces.append(piece)
            yield "delta", {"text": piece}


@dataclass
class ChapterClaim:
    """创建任务时的校验 + 抢占结果（可序列化进 tasks.input）。"""

    chapter_id: int
    project_id: int
    prev_status: str
    ai_config_id: int
    chapter_no: int
    chapter_title: str
    # 润色 / 重写时原文字数（用于估算进度）
    source_words: int = 0
    # 选用的 Skill 快照 {id, name, body}（存进 tasks.input.skill；重试沿用）
    skill: dict[str, Any] | None = None
    model_binding: project_ai_config.ResolvedProjectModel | None = field(default=None, repr=False)
    embedding_selection: project_ai_config.ResolvedSharedEmbedding | None = field(default=None, repr=False)


class ChapterCommitUncertain(Exception):
    """正文提交结果未知；保留原执行屏障，不能把任务转成可重试失败。"""


async def prepare_chapter_task_admission(
    db: AsyncSession, ctx: Any, user_id: int, kind: str, params: dict[str, Any],
):
    """在任何写锁前固定真实 actor、章节父作品和模型选择，并完成 DNS 预检。"""
    from app.schemas.scene_ai import parse_target
    from app.services.task_engine.admission import prepare_task_admission

    actor = authorization_service.actor_snapshot(db, user_id)
    if (type(actor) is not authorization_service.AuthenticatedActor or actor.id != user_id
            or db.new or db.dirty or db.deleted or kind not in {
                "chapter.generate", "chapter.polish", "chapter.revise",
            } or type(params.get("chapter_id")) is not int or params["chapter_id"] <= 0):
        raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    # 请求认证会产生只读事务；不能在 DNS 期间占用该连接或丢弃待写值。
    await db.rollback()
    async with ctx.sessionmaker() as read:
        await authorization_service.require_authenticated(read, actor)
        chapter = await read.scalar(select(Chapter).where(Chapter.id == params["chapter_id"]))
        if chapter is None:
            raise NotFoundError("章节不存在")
        project_id = chapter.project_id
        if "project_id" in params and params["project_id"] != project_id:
            raise NotFoundError("章节不存在")
        await project_access.require_project(read, actor.id, project_id, "ai.use")
        target = parse_target(params.get("target"))
        if isinstance(target, SceneTarget):
            await scene_service.require_scene(read, chapter, target.scene_id)
    model = await project_ai_config.preflight_project_model(
        ctx.sessionmaker, actor, project_id, "text", cipher=ctx.cipher,
        config_id=params.get("ai_config_id"), allow_private=ctx.settings.allow_private_ai_endpoints,
    )
    return await prepare_task_admission(ctx, actor, kind, params, model_preflight=model)


async def create_chapter_task(db: AsyncSession, engine: Any, user_id: int, kind: str, params: dict[str, Any]):
    """先做锁外预检，再由中央任务受理在同一写事务消费证明。"""
    from app.services import task_service

    admission = await prepare_chapter_task_admission(db, engine.ctx, user_id, kind, params)
    return await task_service.create_task(db, engine, user_id, kind, params, admission=admission)


async def chapter_ai_view(db: AsyncSession, actor_id: int, project_id: int, chapter: Chapter) -> ChapterOut:
    """AI 输出只暴露真实 actor 可使用的私人引用，不采用管理员人工旁路。"""
    with db.no_autoflush:
        await project_access.require_project(db, actor_id, project_id, "ai.use")
        views = await manual_chapter_views(db, actor_id, project_id, [chapter], full=True)
        output = views[0]
        if chapter.style_id is not None:
            visible = await db.scalar(select(WritingStyle.id).where(
                WritingStyle.id == chapter.style_id,
                (WritingStyle.user_id == actor_id) | WritingStyle.user_id.is_(None),
            ))
            if visible is None:
                output = output.model_copy(update={"style_id": None})
        return ChapterOut.model_validate(output)


async def _actor_chapter_project(
    db: AsyncSession, actor_id: int, chapter_id: int, project_id: int | None,
    grant: tuple[str, int] | None, *, lock: bool = False,
) -> tuple[Chapter, Project]:
    """AI 仅按原作者或原编辑者邀请访问章节，不借管理员人工权限。"""
    if grant is None:
        chapter = await get_owned_chapter(db, actor_id, chapter_id, lock=lock)
        project = await get_owned_project(db, actor_id, chapter.project_id, lock=lock)
        if project_id is not None and project.id != project_id:
            raise NotFoundError("章节不存在")
        return chapter, project
    if project_id is None:
        raise NotFoundError("章节不存在")
    with db.no_autoflush:
        project = await project_access.require_project(
            db, actor_id, project_id, "ai.use", for_update=lock, visibility_write=lock, grant_identity=grant,
        )
        if lock and _cached_dirty(db, Chapter, chapter_id):
            raise ConflictError("章节存在未提交的修改，请先保存")
        statement = select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id)
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        chapter = await db.scalar(statement)
        if chapter is None:
            raise NotFoundError("章节不存在")
        return chapter, project


async def _author_embedding_selection(
    db: AsyncSession, project: Project, actor_id: int,
) -> project_ai_config.ResolvedSharedEmbedding:
    """仅供作者受理；冻结已配置向量身份，运行时仍必须校验配置和密钥。"""
    if project.user_id != actor_id:
        raise ConflictError("任务向量模型绑定已失效，请重新发起任务")
    with db.no_autoflush:
        row = await project_ai_config._default_config(db, actor_id, "embedding")
    model = project_ai_config._bind(project, actor_id, None, row, source="actor") if row is not None else None
    selection = project_ai_config.ResolvedSharedEmbedding(
        project_id=project.id, actor_id=actor_id, project_owner_id=project.user_id, grant_identity=None,
        mode="vector" if model is not None else "keyword", reason=None if model else "embedding_config_missing",
        model=model,
    )
    project_ai_config.serialize_shared_embedding(selection)
    return selection


def _receipt_digest(value: Any) -> str:
    """对持久 JSON 使用确定性编码，不能容忍非有限数值或非 JSON 值。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _version_digest(version: Any) -> str:
    return _receipt_digest({
        "id": version.id, "chapter_id": version.chapter_id, "source": version.source,
        "content": version.content, "scene_snapshot": version.scene_snapshot,
        "word_count": version.word_count, "generation_log_id": version.generation_log_id,
    })


def _receipt_input_digest(value: object) -> str:
    if not isinstance(value, dict):
        raise ValueError("任务输入无效")
    # 管理员取消可能与成功收尾竞争；终态确认会验证并清理此一次性意图。
    return _receipt_digest({key: item for key, item in value.items() if key != "_admin_chapter_cancel"})


def _completion_receipt(
    claim: ChapterClaimIdentity, version_id: int, content_revision: int,
    version_hash: str, result_hash: str, input_hash: str,
    *, log_instance_key: str | None = None, model_config_instance_key: str | None = None,
) -> dict[str, Any]:
    receipt = {
        "schema": 3 if claim.grant_identity is not None else 2,
        "phase": "completed", "task_id": claim.task_id, "task_key": claim.task_key,
        "actor_id": claim.actor_id, "project_id": claim.project_id, "chapter_id": claim.chapter_id,
        "queued_at": chapter_claim._utc(claim.queued_at).isoformat(), "execution_key": claim.execution_key,
        "claim_key": claim.claim_key, "prev_status": claim.prev_status,
        "version_id": version_id, "content_revision": content_revision,
        "version_hash": version_hash, "result_hash": result_hash, "input_hash": input_hash,
    }
    if claim.grant_identity is not None:
        receipt["grant"] = {"invitation_id": claim.grant_identity[0], "revision": claim.grant_identity[1]}
        receipt["log_instance_key"] = log_instance_key
        receipt["model_config_instance_key"] = model_config_instance_key
    return receipt


async def _write_completion_receipt(
    db: AsyncSession, claim: ChapterClaimIdentity, version: ChapterVersion, content_revision: int,
    result: dict[str, Any], *, log: GenerationLog, binding: project_ai_config.ResolvedProjectModel | None,
) -> dict[str, Any]:
    """保留已有断点；仅在原运行 Task 和原 claim 已精确释放的事务中写入。"""
    task = await db.scalar(select(Task).where(
        Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.user_id == claim.actor_id,
        Task.project_id == claim.project_id, Task.chapter_id == claim.chapter_id, Task.status == "running",
        Task.queued_at == claim.queued_at, Task.execution_key == claim.execution_key,
    ).with_for_update())
    if (task is None or not isinstance(task.input, dict) or "_chapter_claim" in task.input
            or chapter_claim._grant(task.input) != claim.grant_identity):
        raise ConflictError("任务状态已变化，生成结果未保存", extra={"code": "chapter_claim_changed"})
    if task.checkpoint is not None and not isinstance(task.checkpoint, dict):
        raise ConflictError("任务断点已变化，生成结果未保存", extra={"code": "chapter_claim_changed"})
    checkpoint = deepcopy(task.checkpoint or {})
    if "_chapter_claim" in checkpoint:
        raise ConflictError("任务已有无法覆盖的完成记录", extra={"code": "chapter_claim_changed"})
    log_key = config_key = None
    if claim.grant_identity is not None:
        if (type(binding) is not project_ai_config.ResolvedProjectModel or binding.model_type != "text"
                or binding.actor_id != claim.actor_id or binding.project_id != claim.project_id
                or binding.grant_identity != claim.grant_identity
                or _receipt_digest(task.input.get(collaboration_ai_guard.MODEL_KEY))
                != _receipt_digest(project_ai_config.serialize_project_model(binding))):
            raise ConflictError("任务模型来源已变化，生成结果未保存")
        log_key = getattr(log, "log_instance_key", None)
        config_key = getattr(log, "model_config_instance_key", None)
        if (not project_ai_config._instance_key_valid(log_key)
                or not project_ai_config._instance_key_valid(config_key)
                or config_key != binding.config_instance_key
                or not _completion_log_matches(log, claim, task.kind, binding, log_key)):
            raise ConflictError("任务日志来源无法验证，生成结果未保存")
    receipt = _completion_receipt(
        claim, version.id, content_revision, _version_digest(version), _receipt_digest(result),
        _receipt_input_digest(task.input), log_instance_key=log_key, model_config_instance_key=config_key,
    )
    checkpoint["_chapter_claim"] = receipt
    task.checkpoint = checkpoint
    await db.flush()
    return receipt


def _receipt_hash(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _cached_dirty(db: AsyncSession, model: type, identity: int) -> bool:
    cached = db.identity_map.get(identity_key(model, identity))
    return cached is not None and db.is_modified(cached, include_collections=False)


async def _completed_version(
    db: AsyncSession, claim: ChapterClaimIdentity, kind: str, version_id: int,
    *, binding: project_ai_config.ResolvedProjectModel | None = None, receipt: dict[str, Any] | None = None,
) -> Any | None:
    """按项目→任务→章节→版本→日志的顺序锁住全部持久证据。"""
    if _cached_dirty(db, Chapter, claim.chapter_id) or _cached_dirty(db, ChapterVersion, version_id):
        return None
    chapter = await chapter_claim._chapter(db, claim.chapter_id, write=True)
    if (chapter is None or chapter.project_id != claim.project_id
            or chapter.generation_claim_key == claim.claim_key
            or chapter.generation_claim_task_key == claim.task_key):
        return None
    version = (await db.execute(select(
        ChapterVersion.id, ChapterVersion.chapter_id, ChapterVersion.source, ChapterVersion.content,
        ChapterVersion.scene_snapshot, ChapterVersion.word_count, ChapterVersion.generation_log_id,
    ).where(ChapterVersion.id == version_id).with_for_update())).one_or_none()
    if (version is None or version.chapter_id != claim.chapter_id
            or version.source != kind.removeprefix("chapter.") or type(version.generation_log_id) is not int
            or _cached_dirty(db, GenerationLog, version.generation_log_id)):
        return None
    log_fields = [
        GenerationLog.id, GenerationLog.task_id, GenerationLog.actor_user_id, GenerationLog.project_id,
        GenerationLog.chapter_id, GenerationLog.kind, GenerationLog.status,
    ]
    if claim.grant_identity is not None:
        if (binding is None or receipt is None or not hasattr(GenerationLog, "log_instance_key")
                or not hasattr(GenerationLog, "model_config_instance_key")):
            return None
        log_fields.extend((GenerationLog.log_instance_key, GenerationLog.model_config_instance_key,
                           GenerationLog.model_source, GenerationLog.model_config_owner_id))
    log = (await db.execute(select(*log_fields).where(
        GenerationLog.id == version.generation_log_id,
    ).with_for_update())).one_or_none()
    if (log is None or log.task_id != claim.task_id or log.actor_user_id != claim.actor_id
            or log.project_id != claim.project_id or log.chapter_id != claim.chapter_id
            or log.kind != kind.removeprefix("chapter.") or log.status != "success"):
        return None
    if (claim.grant_identity is not None
            and not _completion_log_matches(log, claim, kind, binding, receipt.get("log_instance_key"))):
        return None
    return version


def _completion_log_matches(
    log: Any, claim: ChapterClaimIdentity, kind: str, binding: project_ai_config.ResolvedProjectModel,
    log_key: object,
) -> bool:
    return bool(
        project_ai_config._instance_key_valid(log_key)
        and getattr(log, "log_instance_key", None) == log_key
        and getattr(log, "model_config_instance_key", None) == binding.config_instance_key
        and getattr(log, "model_source", None) == binding.source
        and type(getattr(log, "model_config_owner_id", None)) is int
        and log.model_config_owner_id == binding.config_owner_id
        and log.task_id == claim.task_id and log.actor_user_id == claim.actor_id
        and log.project_id == claim.project_id and log.chapter_id == claim.chapter_id
        and log.kind == kind.removeprefix("chapter.") and log.status == "success"
    )


def _receipt_binding(
    payload: dict[str, Any], claim: ChapterClaimIdentity, owner_id: int, receipt: dict[str, Any],
) -> project_ai_config.ResolvedProjectModel | None:
    if claim.grant_identity is None:
        return None
    try:
        if chapter_claim._grant({"_collaboration_grant": receipt.get("grant")}) != claim.grant_identity:
            return None
        binding = project_ai_config.deserialize_project_model(
            payload.get(collaboration_ai_guard.MODEL_KEY), actor_id=claim.actor_id,
            project_id=claim.project_id, project_owner_id=owner_id,
        )
        if (binding is None or binding.model_type != "text" or binding.grant_identity != claim.grant_identity
                or not project_ai_config._instance_key_valid(receipt.get("log_instance_key"))
                or receipt.get("model_config_instance_key") != binding.config_instance_key):
            return None
        return binding
    except (AppError, TypeError, ValueError):
        return None


def _terminal_result_matches(
    result: object, version: Any, claim: ChapterClaimIdentity, revision: int, target: object,
) -> bool:
    if not isinstance(result, dict) or set(result) - {"chapter", "scene", "scene_ids", "warning"}:
        return False
    chapter = result.get("chapter")
    snapshot = version.scene_snapshot
    if not isinstance(chapter, dict) or not isinstance(snapshot, dict) or not isinstance(snapshot.get("scenes"), list):
        return False
    if (type(chapter.get("id")) is not int or chapter["id"] != claim.chapter_id
            or type(chapter.get("project_id")) is not int or chapter["project_id"] != claim.project_id
            or type(chapter.get("content_revision")) is not int or chapter["content_revision"] != revision
            or type(chapter.get("word_count")) is not int or chapter["word_count"] != version.word_count
            or chapter.get("content") != version.content or chapter.get("status") != "done"):
        return False
    scenes = snapshot["scenes"]
    if (not scenes or any(not isinstance(scene, dict) or type(scene.get("id")) is not int
                          or scene["id"] <= 0 or not isinstance(scene.get("content"), str)
                          or not isinstance(scene.get("separator_before"), str) for scene in scenes)):
        return False
    ids = [scene["id"] for scene in scenes]
    if len(ids) != len(set(ids)) or "".join(scene["separator_before"] + scene["content"] for scene in scenes) \
            != version.content:
        return False
    if not isinstance(target, dict):
        return False
    if "scene_ids" in result:
        return target.get("kind") == "chapter" and "scene" not in result and isinstance(result["scene_ids"], list) \
            and all(type(value) is int for value in result["scene_ids"]) and result["scene_ids"] == ids
    scene = result.get("scene")
    if (not isinstance(scene, dict) or target.get("kind") != "scene" or type(target.get("scene_id")) is not int
            or type(scene.get("id")) is not int or scene["id"] != target["scene_id"] or scene["id"] not in ids):
        return False
    original = next(item for item in scenes if item["id"] == scene["id"])
    return (type(scene.get("chapter_id")) is int and scene["chapter_id"] == claim.chapter_id
            and scene.get("content") == original["content"])


async def completed_chapter_receipt(db: AsyncSession, claim: ChapterClaimIdentity) -> dict[str, Any] | None:
    """仅确认本次已提交正文；无记录或无法确认都不能推断提交失败。"""
    if not chapter_claim._valid_identity(claim) or claim.kind != "single" or claim.phase != "running":
        return None
    with db.no_autoflush:
        project = await project_lifecycle.lock_record(db, claim.project_id)
        if (project.user_id == claim.actor_id) != (claim.grant_identity is None):
            return None
        task = await chapter_claim._task(db, claim.task_id, write=True)
        if (task is None or task.id != claim.task_id or task.statistics_key != claim.task_key
                or task.user_id != claim.actor_id or task.project_id != claim.project_id
                or task.chapter_id != claim.chapter_id or task.status != "running"
                or not isinstance(task.queued_at, type(claim.queued_at))
                or chapter_claim._utc(task.queued_at) != chapter_claim._utc(claim.queued_at)
                or task.execution_key != claim.execution_key or task.kind not in {
                    "chapter.generate", "chapter.polish", "chapter.revise",
                } or not isinstance(task.input, dict) or "_chapter_claim" in task.input
                or not isinstance(task.checkpoint, dict)):
            return None
        try:
            if chapter_claim._grant(task.input) != claim.grant_identity:
                return None
        except ConflictError:
            return None
        receipt = task.checkpoint.get("_chapter_claim")
        if not isinstance(receipt, dict):
            return None
        if any(type(receipt.get(field)) is not int for field in (
            "schema", "task_id", "actor_id", "project_id", "chapter_id", "version_id", "content_revision",
        )):
            return None
        version_id, revision = receipt.get("version_id"), receipt.get("content_revision")
        if version_id <= 0 or revision < 0:
            return None
        version_hash, result_hash, input_hash = (
            receipt.get("version_hash"), receipt.get("result_hash"), receipt.get("input_hash"),
        )
        if not _receipt_hash(version_hash) or not _receipt_hash(result_hash) or not _receipt_hash(input_hash):
            return None
        binding = _receipt_binding(task.input, claim, project.user_id, receipt)
        if claim.grant_identity is not None and binding is None:
            return None
        if (receipt != _completion_receipt(
                claim, version_id, revision, version_hash, result_hash, input_hash,
                log_instance_key=receipt.get("log_instance_key"),
                model_config_instance_key=receipt.get("model_config_instance_key"),
            )
                or _receipt_input_digest(task.input) != input_hash):
            return None
        version = await _completed_version(db, claim, task.kind, version_id, binding=binding, receipt=receipt)
        if version is None or _version_digest(version) != version_hash:
            return None
        return receipt


async def _completed_identity_chapter_receipt(
    db: AsyncSession, execution: TaskExecutionIdentity, *, status: Literal["running", "succeeded"],
    pending_result: object = None, operation_id: str | None = None,
) -> dict[str, Any] | None:
    """以原领取输入与完整收据验证已提交结果。"""
    try:
        if (type(execution) is not TaskExecutionIdentity or execution.kind not in {
            "chapter.generate", "chapter.polish", "chapter.revise",
        }
                or not all(chapter_claim._positive(value) for value in (
                    execution.task_id, execution.actor_id, execution.project_id, execution.chapter_id,
                )) or not chapter_claim._uuid(execution.task_key) or not chapter_claim._uuid(execution.execution_key)
                or not isinstance(execution.queued_at, datetime) or type(execution.actor_token_version) is not int
                or not 0 <= execution.actor_token_version <= 2**31 - 1 or type(execution.step_cursor) is not int
                or execution.step_cursor < 0 or _cached_dirty(db, Project, execution.project_id)
                or _cached_dirty(db, Task, execution.task_id)):
            return None
        if operation_id is not None and (status != "running" or not chapter_claim._uuid(operation_id)):
            return None
        original = execution.input
        if not isinstance(original, dict):
            return None
        pointer = original.get("_chapter_claim")
        if (not isinstance(pointer, dict) or set(pointer) != {"chapter_id", "claim_key", "prev_status"}
                or type(pointer.get("chapter_id")) is not int or pointer["chapter_id"] != execution.chapter_id
                or type(original.get("chapter_id")) is not int or original["chapter_id"] != execution.chapter_id
                or original.get("prev_status") != pointer.get("prev_status")
                or type(original.get("_task_actor_token_version")) is not int
                or original.get("_task_actor_token_version") != execution.actor_token_version
                or "_admin_chapter_cancel" in original
                or chapter_claim._grant(original) != execution.grant_identity):
            return None
        claim = ChapterClaimIdentity(
            task_id=execution.task_id, task_key=execution.task_key, actor_id=execution.actor_id,
            project_id=execution.project_id, chapter_id=execution.chapter_id, queued_at=execution.queued_at,
            execution_key=execution.execution_key, claim_key=pointer["claim_key"],
            prev_status=pointer["prev_status"], grant_identity=execution.grant_identity, phase="running", kind="single",
        )
        if not chapter_claim._valid_identity(claim):
            return None
        original.pop("_chapter_claim")
        checkpoint = execution.checkpoint
        if checkpoint is not None and not isinstance(checkpoint, dict):
            return None
        checkpoint = deepcopy(checkpoint or {})
        if "_chapter_claim" in checkpoint:
            return None
        with db.no_autoflush:
            project = await project_lifecycle.lock_record(db, execution.project_id)
            if (project.user_id == execution.actor_id) != (execution.grant_identity is None):
                return None
            conditions = [
                Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
            ] if operation_id is None else [
                Task.admin_cancel_operation_id == operation_id, Task.admin_cancel_requested_at.is_not(None),
            ]
            row = (await db.execute(select(
                Task.input, Task.checkpoint, Task.result, Task.resource_key, Task.step_cursor,
                Task.admin_cancel_requested_at,
            ).where(
                Task.id == execution.task_id, Task.statistics_key == execution.task_key,
                Task.user_id == execution.actor_id, Task.project_id == execution.project_id,
                Task.chapter_id == execution.chapter_id, Task.kind == execution.kind,
                Task.queued_at == execution.queued_at, Task.execution_key == execution.execution_key,
                Task.status == status, *conditions,
            ).with_for_update())).one_or_none()
            if (row is None or not isinstance(row.input, dict) or not isinstance(row.checkpoint, dict)
                    or (operation_id is not None and not isinstance(row.admin_cancel_requested_at, datetime))
                    or row.resource_key != f"chapter:{execution.chapter_id}" or row.step_cursor != execution.step_cursor
                    or _receipt_digest(row.input) != _receipt_digest(original)):
                return None
            receipt = row.checkpoint.get("_chapter_claim")
            if not isinstance(receipt, dict) or any(type(receipt.get(key)) is not int for key in (
                "schema", "task_id", "actor_id", "project_id", "chapter_id", "version_id", "content_revision",
            )):
                return None
            version_id, revision = receipt["version_id"], receipt["content_revision"]
            binding = _receipt_binding(original, claim, project.user_id, receipt)
            if claim.grant_identity is not None and binding is None:
                return None
            if (version_id <= 0 or revision < 0 or not _receipt_hash(receipt.get("version_hash"))
                    or not _receipt_hash(receipt.get("result_hash")) or not _receipt_hash(receipt.get("input_hash"))
                    or receipt != _completion_receipt(
                        claim, version_id, revision, receipt["version_hash"], receipt["result_hash"],
                        receipt["input_hash"], log_instance_key=receipt.get("log_instance_key"),
                        model_config_instance_key=receipt.get("model_config_instance_key"),
                    )):
                return None
            checkpoint["_chapter_claim"] = receipt
            request = original.get("request")
            if (_receipt_digest(checkpoint) != _receipt_digest(row.checkpoint)
                    or _receipt_input_digest(original) != receipt["input_hash"]
                    or not isinstance(request, dict) or type(request.get("chapter_id")) is not int
                    or request["chapter_id"] != execution.chapter_id
                    or ("project_id" in request and (type(request["project_id"]) is not int
                                                    or request["project_id"] != execution.project_id))):
                return None
            version = await _completed_version(db, claim, execution.kind, version_id, binding=binding, receipt=receipt)
            result = row.result if status == "succeeded" else pending_result
            if (version is None or _version_digest(version) != receipt["version_hash"]
                    or _receipt_digest(result) != receipt["result_hash"]
                    or not _terminal_result_matches(result, version, claim, revision, request.get("target"))):
                return None
            return receipt
    except (ConflictError, TypeError, ValueError, UnicodeError, RecursionError, KeyError):
        return None


async def completed_running_chapter_receipt(
    db: AsyncSession, execution: TaskExecutionIdentity, result: object,
) -> dict[str, Any] | None:
    """验证完成事件来自尚在原执行轮次中的已提交结果。"""
    return await _completed_identity_chapter_receipt(db, execution, status="running", pending_result=result)


async def completed_admin_race_chapter_receipt(
    db: AsyncSession, execution: TaskExecutionIdentity, result: object, *, operation_id: str,
) -> dict[str, Any] | None:
    """仅确认正文先提交、后受理此原管理员操作的完成事实。"""
    if not chapter_claim._uuid(operation_id):
        return None
    return await _completed_identity_chapter_receipt(
        db, execution, status="running", pending_result=result, operation_id=operation_id,
    )


async def completed_success_chapter_receipt(
    db: AsyncSession, execution: TaskExecutionIdentity,
) -> dict[str, Any] | None:
    """验证原领取轮次的成功事实，供自动分析在同一事务内使用。"""
    return await _completed_identity_chapter_receipt(db, execution, status="succeeded")


async def completed_terminal_chapter_receipt(db: AsyncSession, task: Task) -> dict[str, Any] | None:
    """只确认原任务成功、版本/日志和最终结果相互匹配的单章收据。"""
    if (not isinstance(task, Task) or db.is_modified(task, include_collections=False)
            or task.kind not in {"chapter.generate", "chapter.polish", "chapter.revise"}
            or task.status != "succeeded" or task.execution_key is not None
            or task.admin_cancel_operation_id is not None or task.admin_cancel_requested_at is not None
            or not chapter_claim._uuid(task.statistics_key) or not isinstance(task.queued_at, datetime)
            or not all(chapter_claim._positive(value)
                       for value in (task.id, task.user_id, task.project_id, task.chapter_id))
            or _cached_dirty(db, Project, task.project_id)):
        return None
    try:
        input_snapshot, checkpoint_snapshot, result_snapshot = (
            _receipt_digest(value) for value in (task.input, task.checkpoint, task.result)
        )
        with db.no_autoflush:
            project = await project_lifecycle.lock_record(db, task.project_id)
            row = (await db.execute(select(
                Task.id, Task.statistics_key, Task.user_id, Task.project_id, Task.chapter_id, Task.kind,
                Task.resource_key, Task.queued_at, Task.input, Task.checkpoint, Task.result,
            ).where(
                Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
                Task.project_id == task.project_id, Task.chapter_id == task.chapter_id, Task.kind == task.kind,
                Task.queued_at == task.queued_at, Task.status == "succeeded", Task.execution_key.is_(None),
                Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
            ).with_for_update())).one_or_none()
            if (row is None or not isinstance(row.input, dict) or not isinstance(row.checkpoint, dict)
                    or any(key in row.input for key in (
                        "_chapter_claim", "_admin_chapter_cancel", "_admin_chapter_receipt",
                    )) or row.resource_key != f"chapter:{task.chapter_id}"
                    or any(_receipt_digest(value) != expected for value, expected in zip(
                        (row.input, row.checkpoint, row.result),
                        (input_snapshot, checkpoint_snapshot, result_snapshot), strict=True,
                    ))):
                return None
            grant = chapter_claim._grant(row.input)
            if (project.user_id == task.user_id) != (grant is None):
                return None
            receipt = row.checkpoint.get("_chapter_claim")
            if not isinstance(receipt, dict) or any(type(receipt.get(key)) is not int for key in (
                "schema", "task_id", "actor_id", "project_id", "chapter_id", "version_id", "content_revision",
            )):
                return None
            version_id, revision = receipt["version_id"], receipt["content_revision"]
            if (version_id <= 0 or revision < 0 or not _receipt_hash(receipt.get("version_hash"))
                    or not _receipt_hash(receipt.get("result_hash")) or not _receipt_hash(receipt.get("input_hash"))):
                return None
            claim = ChapterClaimIdentity(
                task_id=task.id, task_key=task.statistics_key, actor_id=task.user_id, project_id=task.project_id,
                chapter_id=task.chapter_id, queued_at=task.queued_at, execution_key=receipt.get("execution_key"),
                claim_key=receipt.get("claim_key"), prev_status=receipt.get("prev_status"), grant_identity=grant,
                phase="running", kind="single",
            )
            binding = _receipt_binding(row.input, claim, project.user_id, receipt)
            if claim.grant_identity is not None and binding is None:
                return None
            if not chapter_claim._valid_identity(claim) or receipt != _completion_receipt(
                claim, version_id, revision, receipt["version_hash"], receipt["result_hash"], receipt["input_hash"],
                log_instance_key=receipt.get("log_instance_key"),
                model_config_instance_key=receipt.get("model_config_instance_key"),
            ):
                return None
            request = row.input.get("request")
            if (not isinstance(request, dict) or type(row.input.get("chapter_id")) is not int
                    or row.input["chapter_id"] != task.chapter_id or type(request.get("chapter_id")) is not int
                    or request["chapter_id"] != task.chapter_id or row.input.get("prev_status") != claim.prev_status
                    or ("project_id" in request and (type(request["project_id"]) is not int
                                                    or request["project_id"] != task.project_id))):
                return None
            version = await _completed_version(db, claim, row.kind, version_id, binding=binding, receipt=receipt)
            if (version is None or _receipt_input_digest(row.input) != receipt["input_hash"]
                    or _version_digest(version) != receipt["version_hash"]
                    or _receipt_digest(row.result) != receipt["result_hash"]
                    or not _terminal_result_matches(row.result, version, claim, revision, request.get("target"))):
                return None
            return receipt
    except (ConflictError, TypeError, ValueError, UnicodeError):
        return None


async def validate_and_claim(
    db: AsyncSession,
    *,
    user_id: int,
    chapter_id: int,
    mode: Mode,
    req: ChapterRequest,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    skill_snapshot: dict[str, Any] | None = None,
    target: ChapterTarget | SceneTarget | None = None,
    project_id: int | None = None,
    defer_claim: bool = False,
    admission: project_ai_config.ProjectModelPreflight | None = None,
    original_model_binding: project_ai_config.ResolvedProjectModel | None = None,
    original_embedding_selection: project_ai_config.ResolvedSharedEmbedding | None = None,
) -> ChapterClaim:
    """鉴权、配置解析、SSRF 校验与参数校验。

    defer_claim=True 时调用方在 Task flush 后同事务绑定占用；旧直接调用沿用原抢占。
    不 commit：调用方负责与任务插入一起提交或回滚。
    """
    with db.no_autoflush:
        if defer_claim:
            if admission is None:
                raise ConflictError("任务受理凭据缺失，请重新发起")
            candidate = project_ai_config.require_original_preflight(admission).binding
            if candidate.actor_id != user_id or (project_id is not None and candidate.project_id != project_id):
                raise ConflictError("任务受理凭据已变化，请重新发起")
            project = await project_access.require_project(
                db, user_id, candidate.project_id, "ai.use", for_update=True, visibility_write=True,
                grant_identity=candidate.grant_identity,
            )
            if _cached_dirty(db, Chapter, chapter_id):
                raise ConflictError("章节存在未提交的修改，请先保存")
            chapter = await db.scalar(select(Chapter).where(
                Chapter.id == chapter_id, Chapter.project_id == candidate.project_id,
            ).with_for_update().execution_options(populate_existing=True))
            if chapter is None:
                raise NotFoundError("章节不存在")
        else:
            chapter = await get_owned_chapter(db, user_id, chapter_id, lock=True)
            project = await get_owned_project(db, user_id, chapter.project_id, lock=True)
            await db.refresh(chapter)
    if project_id is not None and project.id != project_id:
        raise NotFoundError("章节不存在")
    target = target or ChapterTarget()
    scenes = await scene_service.list_active_scenes(db, chapter.id)
    selected = ([await scene_service.require_scene(db, chapter, target.scene_id)]
                if isinstance(target, SceneTarget) else scenes)
    await scene_ai_context.check_revisions(
        db, chapter, req.expected_content_revision, req.expected_planning_revision,
    )
    await scene_service.capture_snapshot(db, chapter)
    if chapter.status == "generating":
        raise ConflictError(GENERATING_CONFLICT)
    await ensure_outside_batch(db, chapter)

    if original_model_binding is not None and (
        not defer_claim or original_model_binding.actor_id != user_id
        or original_model_binding.project_id != project.id or original_model_binding.project_owner_id != project.user_id
        or (req.ai_config_id is not None and req.ai_config_id != original_model_binding.config_id)
    ):
        raise ConflictError("任务模型绑定已失效，请重新发起任务")
    model_binding = None
    embedding_selection = None
    if defer_claim:
        assert admission is not None
        # 中央已在写屏障内消费原预检并重验；不能持锁再次解析 DNS 或重选默认配置。
        model_binding = admission.binding
        cfg = _snapshot_config(admission.config)
        if (cfg.instance_key != model_binding.config_instance_key
                or project_ai_config._fingerprint(cfg) != model_binding.config_fingerprint
                or (original_model_binding is not None and model_binding != original_model_binding)):
            raise ConflictError("模型配置已变化，请重新发起任务")
        if original_embedding_selection is None:
            embedding_selection = (await _author_embedding_selection(db, project, user_id)
                                   if model_binding.grant_identity is None else
                                   await project_ai_config.resolve_shared_embedding(
                                       db, user_id, project.id, cipher=cipher, allow_private=allow_private,
                                       for_update=True,
                                   ))
        else:
            embedding_selection = original_embedding_selection
            await project_ai_config.revalidate_shared_embedding(
                db, embedding_selection, cipher=cipher, allow_private=allow_private, for_update=True,
            )
            if (embedding_selection.actor_id != user_id or embedding_selection.project_id != project.id
                    or embedding_selection.project_owner_id != project.user_id
                    or embedding_selection.grant_identity != model_binding.grant_identity):
                raise ConflictError("任务向量模型绑定已失效，请重新发起任务")
    else:
        config_id = original_model_binding.config_id if original_model_binding is not None else req.ai_config_id
        cfg = await ai_config_service.resolve_config(db, user_id, config_id)
        await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    await style_service.validate_choice(db, user_id, req.style)
    # 重试沿用原任务的快照（Skill 之后被修改或删除都不影响）
    skill = skill_snapshot or await skill_service.snapshot(db, user_id, req.skill_id, req.skill_inputs)
    if mode == "generate" and isinstance(req, GenerateChapterRequest) and req.target_words < len(selected):
        raise BadRequestError("整章目标字数不足以为每个场景分配正字数，请增大目标字数")
    if mode == "polish" and not any(scene.content.strip() for scene in selected):
        raise BadRequestError("章节正文为空，无法润色")
    if mode == "revise":
        assert isinstance(req, ReviseChapterRequest)
        await resolve_revision(db, chapter, req)
        if not any(scene.content.strip() for scene in selected):
            raise BadRequestError("场景正文为空，无法重写")

    prev_status = chapter.status if defer_claim else await claim_chapter(db, chapter)
    return ChapterClaim(
        chapter_id=chapter.id,
        project_id=project.id,
        prev_status=prev_status,
        ai_config_id=cfg.id,
        chapter_no=chapter.chapter_no,
        chapter_title=chapter.title,
        source_words=sum(scene.word_count for scene in selected) if mode in ("polish", "revise") else 0,
        skill=skill,
        model_binding=model_binding,
        embedding_selection=embedding_selection,
    )


async def claim_chapter(db: AsyncSession, chapter: Chapter) -> str:
    """原子地把章节置为 generating（防止同一章节被并发生成），返回抢占前的状态。

    不 commit（由调用方与后续写入同一事务提交）；已在生成中 → 回滚并 409。
    """
    prev_status = chapter.status
    result = await db.execute(
        update(Chapter).where(Chapter.id == chapter.id, Chapter.status != "generating").values(status="generating")
    )
    if result.rowcount != 1:
        await db.rollback()
        raise ConflictError(GENERATING_CONFLICT)
    return prev_status


async def ensure_outside_batch(db: AsyncSession, chapter: Chapter) -> None:
    """范围锁：同项目排队中 / 运行中的批量生成尚未处理到本章（chapter_ids[step_cursor:]）→ 409。

    读后判断、尽力而为；并发竞争由章节 generating 的原子抢占兜底。待审核（paused）的批量不锁范围。
    """
    rows = await db.execute(
        select(Task.input, Task.step_cursor).where(
            Task.project_id == chapter.project_id,
            Task.kind == BATCH_KIND,
            Task.status.in_(ACTIVE_TASK_STATUSES),
        )
    )
    for task_input, cursor in rows.all():
        ids = (task_input or {}).get("chapter_ids") or []
        if chapter.id in ids[cursor:]:
            raise ConflictError(BATCH_RANGE_CONFLICT)


async def resolve_revision(db: AsyncSession, chapter: Chapter, req: ReviseChapterRequest) -> list[str]:
    """校验重写请求并返回勾选建议的文本（按勾选顺序）。"""
    if not chapter.content.strip():
        raise BadRequestError("章节正文为空，无法重写")
    analysis = await db.scalar(
        select(ChapterAnalysis).where(ChapterAnalysis.id == req.analysis_id, ChapterAnalysis.chapter_id == chapter.id)
    )
    if analysis is None:
        raise BadRequestError("分析结果不存在或不属于该章节")
    by_id = {s.get("id"): s for s in analysis.suggestions or [] if isinstance(s, dict)}
    ids = list(dict.fromkeys(req.suggestion_ids))
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise BadRequestError(f"建议不存在：{'、'.join(str(i) for i in missing)}")
    if not ids and not req.instruction.strip():
        raise BadRequestError("请至少选择一条建议或填写修改要求")
    return [str(by_id[i].get("text") or "") for i in ids]


async def build_chapter_job(
    db: AsyncSession,
    *,
    user_id: int,
    chapter_id: int,
    mode: Mode,
    req: ChapterRequest,
    prev_status: str,
    ai_config_id: int | None,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    context_budget: int,
    task_id: int | None = None,
    task_key: str | None = None,
    claim: ChapterClaimIdentity | None = None,
    model_binding: project_ai_config.ResolvedProjectModel | None = None,
    embedding_selection: project_ai_config.ResolvedSharedEmbedding | None = None,
    execution: TaskExecutionIdentity | None = None,
    batch_attempt: collaboration_ai_guard.BatchAttemptIdentity | None = None,
    skill: dict[str, Any] | None = None,
    target: ChapterTarget | SceneTarget | None = None,
) -> ChapterJob:
    """按稳定 Scene 身份组装第一步；后续步骤只能使用暂存的最新前景。"""
    if batch_attempt is not None and (execution is None or claim != batch_attempt.claim):
        raise ConflictError("批量章节原执行身份缺失，请重新发起任务")
    if execution is not None and (
        claim is None or execution.task_id != task_id or execution.task_key != task_key
        or execution.actor_id != user_id or execution.project_id != claim.project_id
        or execution.grant_identity != claim.grant_identity
    ):
        raise ConflictError("任务执行身份已变化，请重新发起任务")
    # 协作检索只能经原任务执行身份：向量模式只用受理时冻结的共享向量选择（作者向量、按 allow_owner_ai），
    # 每次查询嵌入经 for_task 守卫重验原邀请轮次与授权；关键词模式不发送。命中仍由 verify_retrieval_hits
    # 逐条核对同书来源。无执行身份的直接调用会落到「本人默认向量配置」分支，不得用于协作 claim。
    if (claim is not None and claim.grant_identity is not None and mode in ("generate", "revise")
            and (execution is None or embedding_selection is None)):
        raise ConflictError("协作检索尚未就绪，请稍后重试")
    chapter, project = await _actor_chapter_project(
        db, user_id, chapter_id, claim.project_id if claim is not None else None,
        claim.grant_identity if claim is not None else None,
    )
    if chapter.status != "generating":
        raise ConflictError("章节生成状态已被复位，任务未执行")
    await scene_ai_context.check_revisions(db, chapter, req.expected_content_revision, req.expected_planning_revision)
    stamp = await scene_ai_context.read_stamp(db, project.id, chapter.id)
    snapshot = await scene_service.capture_snapshot(db, chapter)
    scenes = snapshot["scenes"]
    target = target or ChapterTarget()
    if isinstance(target, SceneTarget):
        await scene_service.require_scene(db, chapter, target.scene_id)
    selected = [dict(scene) for scene in scenes
                if not isinstance(target, SceneTarget) or scene["id"] == target.scene_id]
    steps = [scene for scene in selected if mode == "generate" or scene["content"].strip()]
    if not steps:
        raise BadRequestError("章节正文为空，无法润色" if mode == "polish" else "章节正文为空，无法重写")
    if mode == "generate" or (mode == "revise" and req.target_words is not None):
        total_words = req.target_words
        if total_words < len(steps):
            raise BadRequestError("整章目标字数不足以为每个场景分配正字数，请增大目标字数")
        quotient, remainder = divmod(total_words, len(steps))
        for index, step in enumerate(steps):
            step["target_words"] = quotient + (index < remainder)
    else:
        legacy_revise_floor = (mode == "revise" and isinstance(target, ChapterTarget) and len(scenes) == 1)
        for step in steps:
            step["target_words"] = max(count_words(step["content"]), 300 if legacy_revise_floor else 1)
    if model_binding is not None:
        if (model_binding.actor_id != user_id or model_binding.project_id != project.id
                or model_binding.project_owner_id != project.user_id
                or claim is None or model_binding.grant_identity != claim.grant_identity
                or (req.ai_config_id is not None and req.ai_config_id != model_binding.config_id)):
            raise ConflictError("任务模型绑定已失效，请重新发起任务")
        cfg = await db.scalar(select(AIConfig).where(
            AIConfig.id == model_binding.config_id, AIConfig.user_id == model_binding.config_owner_id,
        ))
        if cfg is None:
            if project.user_id == user_id and model_binding.source == "actor":
                raise NotFoundError("模型配置不存在")
            raise ConflictError("任务模型绑定已失效，请重新发起任务")
        await project_ai_config.revalidate_project_model(
            db, model_binding, cipher=cipher, allow_private=allow_private, lock_config=False,
        )
        if (cfg.instance_key != model_binding.config_instance_key
                or project_ai_config._fingerprint(cfg) != model_binding.config_fingerprint):
            raise ConflictError("任务模型绑定已失效，请重新发起任务")
    elif claim is not None and claim.kind == "single":
        raise ConflictError("任务模型绑定缺失，请重新发起任务")
    else:
        cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    if claim is not None:
        cfg = _snapshot_config(cfg)
    checked_factory = ai_dispatch.chapter_provider_factory(factory) if claim is not None else factory
    provider = await ai_config_service.build_provider(
        cfg, cipher, checked_factory, ai_timeout, allow_private=allow_private,
    )
    transport = None
    if claim is not None:
        provider = _BoundModelProvider(provider, cfg.model, "text")
        transport = _snapshot_transport(provider)
    if claim is not None and model_binding is None:
        model_binding = project_ai_config._bind(project, user_id, None, cfg, source="actor")
    resolved = await style_service.resolve(db, user_id, project, chapter, req.style)
    template_key = f"chapter.{mode}"
    override = await prompt_service.get_override(db, user_id, template_key)
    continue_override = (await prompt_service.get_override(db, user_id, "chapter.continue")
                         if mode == "generate" else None)
    embedder = None
    embedding_binding = None
    if mode in ("generate", "revise"):
        if claim is None:
            embedder = await retrieval_service.get_embedder(
                db, user_id, cipher=cipher, factory=checked_factory, ai_timeout=ai_timeout, allow_private=allow_private,
            )
        elif execution is None:
            # 只读取一次默认配置；绑定和 provider 都取同一快照，不能在两次选择间切换密钥。
            embedding_config = await retrieval_service.default_embedding_config(db, user_id)
            if embedding_config is not None:
                embedding_config = _snapshot_config(embedding_config)
                embedding_binding = project_ai_config._bind(project, user_id, None, embedding_config, source="actor")
                embedding_provider = await ai_config_service.build_provider(
                    embedding_config, cipher, checked_factory, ai_timeout, allow_private=allow_private,
                )
                embedding_provider = _BoundModelProvider(embedding_provider, embedding_config.model, "embedding")
                embedder = retrieval_service.Embedder(
                    provider=embedding_provider, provider_name=embedding_config.provider,
                    model=embedding_config.model, config_name=embedding_config.name,
                )
        else:
            if embedding_selection is None:
                raise ConflictError("任务向量模型绑定缺失，请重新发起任务")
            await project_ai_config.revalidate_shared_embedding(
                db, embedding_selection, cipher=cipher, allow_private=allow_private, lock_config=False,
            )
            if embedding_selection.mode == "vector":
                embedding_binding = embedding_selection.model
                assert embedding_binding is not None
                embedding_config = await db.scalar(select(AIConfig).where(
                    AIConfig.id == embedding_binding.config_id, AIConfig.user_id == embedding_binding.config_owner_id,
                ))
                if (embedding_config is None or embedding_config.instance_key != embedding_binding.config_instance_key
                        or project_ai_config._fingerprint(embedding_config) != embedding_binding.config_fingerprint):
                    raise ConflictError("任务向量模型绑定已失效，请重新发起任务")
                embedding_config = _snapshot_config(embedding_config)
                embedding_provider = await ai_config_service.build_provider(
                    embedding_config, cipher, checked_factory, ai_timeout, allow_private=allow_private,
                )
                embedding_provider = _BoundModelProvider(embedding_provider, embedding_config.model, "embedding")
                embedder = retrieval_service.Embedder(
                    provider=embedding_provider, provider_name=embedding_config.provider,
                    model=embedding_config.model, config_name=embedding_config.name,
                )
    if claim is not None and embedder is not None:
        assert embedding_binding is not None
        ai_dispatch.require_chapter_transport(embedder.provider)
        if execution is not None:
            assert embedding_selection is not None
            guard = collaboration_ai_guard.for_task(
                execution, embedding_selection, stamp.visibility, cipher=cipher, allow_private=allow_private,
                scene=stamp, chapter=claim, batch_attempt=batch_attempt,
                extra_bindings=(model_binding,),
            )
            authorize, verify, preflight = guard.authorize, guard.verify, guard.verify_without_one_shot
        else:
            authorize, verify = _claim_send_callbacks(
                claim, stamp, embedding_binding, cipher, allow_private, _snapshot_transport(embedder.provider),
            )
            preflight = None
        sessions = visibility_sessionmaker(db)
        embedder = replace(embedder, provider=ai_dispatch.guard_chapter_provider(
            embedder.provider, stamp.visibility, sessions, chapter=claim, authorize=authorize, verify=verify,
            verify_preflight=preflight,
        ))
        embedder = retrieval_service.guard_embedder(
            embedder, stamp.visibility, sessions, chapter=claim,
        )
    suggestions = await resolve_revision(db, chapter, req) if isinstance(req, ReviseChapterRequest) else []
    params = {
        "instruction": req.instruction, "target": target.model_dump(),
        "context_snapshot": stamp.log_data(), "prompt": prompt_meta(template_key, override),
        "style_choice": req.style, "style": style_service.style_log(resolved), "skill": skill_service.skill_log(skill),
        "scenes": [],
        "target_words": sum(step["target_words"] for step in steps) if mode in ("generate", "revise") else None,
    }
    if isinstance(req, GenerateChapterRequest):
        params.update(auto_continue=req.auto_continue,
                      continue_prompt=prompt_meta("chapter.continue", continue_override))
    if isinstance(req, ReviseChapterRequest):
        params.update(analysis_id=req.analysis_id, suggestion_ids=req.suggestion_ids, preserve=req.preserve)
    job = ChapterJob(
        chapter_id=chapter.id, prev_status=prev_status, provider=provider, raw_provider=provider, model=cfg.model,
        messages=[], thinking=cfg.thinking_level, max_tokens=ai_config_service.max_output_tokens(cfg), mode=mode,
        project_id=project.id, ai_config_id=cfg.id, provider_name=cfg.provider,
        target_words=steps[0]["target_words"],
        auto_continue=isinstance(req, GenerateChapterRequest) and req.auto_continue,
        params=params, task_id=task_id, task_key=task_key, chapter_claim=claim, model_binding=model_binding,
        embedding_selection=embedding_selection, execution=execution, batch_attempt=batch_attempt,
        model_cipher=cipher if claim is not None else None, model_allow_private=allow_private,
        model_transport=transport,
        continue_override=continue_override, user_id=user_id,
        target=target, scenes=scenes, steps=steps, context_stamp=stamp, request=req,
        context_budget=ai_config_service.context_char_budget(cfg, context_budget), override=override,
        style=resolved.block if resolved is not None else "", skill_guide=str((skill or {}).get("body") or ""),
        embedder=embedder, suggestions=suggestions,
    )
    previous = "".join(scene["separator_before"] + scene["content"] for scene in scenes
                       if scene["sort"] < steps[0]["sort"])
    await _prepare_scene_step(db, job, steps[0], previous)
    return job


def _claim_send_callbacks(
    claim: ChapterClaimIdentity, stamp: scene_ai_context.SceneContextStamp,
    binding: project_ai_config.ResolvedProjectModel, cipher: SecretCipher, allow_private: bool,
    transport: _TransportSnapshot,
):
    async def authorize(db: AsyncSession) -> None:
        await ai_dispatch.authorize_send_actor(db, claim.actor_id)

    async def verify(db: AsyncSession) -> None:
        _check_transport(transport)
        await scene_ai_context.require_stamp(db, stamp, sending=True)
        await project_ai_config.revalidate_project_model(
            db, binding, cipher=cipher, allow_private=allow_private, lock_config=True,
        )
        _check_transport(transport)

    return authorize, verify


async def _prepare_scene_step(db: AsyncSession, job: ChapterJob, step: dict[str, Any], preceding: str) -> None:
    """只读重建当前步骤，绝不暂写 Scene 或混入数据库中的旧前场景。"""
    from app.services.rich_document_ai import scene_reference_context

    assert job.context_stamp is not None and job.request is not None and job.raw_provider is not None
    await scene_ai_context.require_stamp(db, job.context_stamp)
    chapter, project = await _actor_chapter_project(
        db, job.user_id, job.chapter_id, job.project_id,
        job.chapter_claim.grant_identity if job.chapter_claim is not None else None,
    )
    req = job.request
    index = next(index for index, scene in enumerate(job.scenes, 1) if scene["id"] == step["id"])
    # 范围块也消耗同一上下文预算；大摘要和衔接明确截断而不是无限追加。
    budget = job.context_budget
    clip = max(0, min(1500, budget // 8))
    def clipped(text: str) -> str:
        return text if len(text) <= clip else text[:clip] + "……[已截断]"
    preceding_tail = preceding[-clip:] if clip else ""
    if len(preceding) > clip:
        preceding_tail = "[前文已截断]……" + preceding_tail
    scene_context = scene_ai_context.scope_block(
        scene_id=step["id"], title=step["title"], summary=clipped(step["summary"]), index=index,
        total=len(job.scenes), target_words=step["target_words"], preceding_text=preceding_tail,
    )
    author_budget = max(0, min(3_000, budget // 4, budget - len(scene_context) - 2))
    author_context = await scene_reference_context(
        db, project.id, [(scene["id"], scene["sort"]) for scene in job.scenes if scene["sort"] <= step["sort"]],
        author_budget,
    )
    if author_context:
        scene_context += "\n\n" + author_context
    remaining = budget - len(scene_context) - 2
    planning = await planning_context_service.load_planning_context(
        db, project, "scene", step["id"], budget=remaining,
        text_hint="\n".join((step["content"], req.instruction, author_context)), stamp=job.context_stamp.visibility,
        preceding_text=preceding if index > 1 else None,
    )
    task = await chapter_task(db, chapter)
    available = remaining - planning.reserved_chars
    common = dict(style=job.style, instruction=req.instruction, skill_guide=job.skill_guide,
                  planning_context=planning.text, scene_context=scene_context, override=job.override)
    if job.mode == "polish":
        setting_context = await build_setting_context(db, project, chapter, budget=available, planning=planning)
        messages = build_polish_messages(content=step["content"], setting_context=setting_context, **common)
    else:
        context = await build_chapter_context(
            db, project, chapter, available, actor_user_id=job.user_id, embedder=job.embedder, planning=planning,
        )
        if job.mode == "generate":
            messages = build_chapter_messages(
                context=context, chapter_no=chapter.chapter_no, title=chapter.title or task.title,
                summary=task.summary, target_words=step["target_words"], key_events=task.key_events,
                goal=task.goal, emotion=task.emotion, **common,
            )
        else:
            assert isinstance(req, ReviseChapterRequest)
            messages = build_revise_messages(
                context=context, chapter_no=chapter.chapter_no, title=chapter.title or task.title,
                content=step["content"], suggestions=job.suggestions, preserve=req.preserve,
                target_words=step["target_words"], **common,
            )
    assert planning.setting_scope is not None
    await planning.setting_scope.scope.verify(db)
    await scene_ai_context.require_stamp(db, job.context_stamp)
    job.messages = messages
    if job.chapter_claim is not None:
        if (job.model_binding is None or job.model_cipher is None or job.model_transport is None
                or job.model_transport.provider is not job.raw_provider):
            raise ConflictError("模型绑定缺失，请重新发起任务")
        if job.execution is not None:
            guard = collaboration_ai_guard.for_task(
                job.execution, job.model_binding, job.context_stamp.visibility, cipher=job.model_cipher,
                allow_private=job.model_allow_private, scene=job.context_stamp, chapter=job.chapter_claim,
                batch_attempt=job.batch_attempt,
                extra_bindings=((job.embedding_selection,) if job.embedding_selection is not None else ()),
            )
            authorize, verify, preflight = guard.authorize, guard.verify, guard.verify_without_one_shot
        else:
            authorize, verify = _claim_send_callbacks(
                job.chapter_claim, job.context_stamp, job.model_binding, job.model_cipher, job.model_allow_private,
                job.model_transport,
            )
            preflight = None
        # 与 provider 的受理快照比较，紧接同步建立 guard；不能把 await 期间被改写的密钥或端点固定进去。
        _check_transport(job.model_transport)
        job.provider = ai_dispatch.guard_chapter_provider(
            job.raw_provider, job.context_stamp.visibility, visibility_sessionmaker(db),
            chapter=job.chapter_claim, authorize=authorize, verify=verify, verify_preflight=preflight,
        )
    else:
        job.provider = guard_provider(
            job.raw_provider, job.context_stamp.visibility, visibility_sessionmaker(db),
            verify=lambda session: scene_ai_context.require_stamp(session, job.context_stamp, sending=True),
        )
    job.target_words = step["target_words"]
    data = {"scene_id": step["id"], "target_words": step["target_words"], "planning": planning.log_data("chapter")}
    data.update(scope_truncated=len(preceding) > clip or len(step["summary"]) > clip, scope_chars=len(scene_context))
    job.params["scenes"].append(data)
    if len(job.params["scenes"]) == 1:
        job.params["planning"] = data["planning"]


async def _restore_status(
    sessionmaker: async_sessionmaker[AsyncSession], chapter_id: int, status: str,
    claim: ChapterClaimIdentity | None = None,
) -> None:
    try:
        async with sessionmaker() as db:
            if claim is not None:
                await chapter_claim.release_chapter_claim(db, claim)
            else:
                # 尚未迁移的作者 batch/直接调用只可回退无身份的旧生成状态，不能触碰新 claim。
                await db.execute(update(Chapter).where(
                    Chapter.id == chapter_id, Chapter.status == "generating",
                    Chapter.generation_claim_key.is_(None), Chapter.generation_claim_task_key.is_(None),
                    Chapter.generation_claim_prev_status.is_(None),
                ).values(status=status))
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.error("回退章节 %s 状态失败", chapter_id)
        if claim is not None:
            raise


async def _is_collaborator_job(job: ChapterJob, sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    if (
        (job.execution is not None and job.execution.grant_identity is not None)
        or (job.execution is not None and job.execution.actor_id != job.user_id)
        or (job.chapter_claim is not None and job.chapter_claim.grant_identity is not None)
        or (job.model_binding is not None
            and (job.model_binding.grant_identity is not None or job.model_binding.source == "owner"))
        or (job.embedding_selection is not None and job.embedding_selection.grant_identity is not None)
    ):
        return True
    try:
        async with sessionmaker() as db:
            owner_id = await db.scalar(select(Project.user_id).where(Project.id == job.project_id))
            return type(owner_id) is not int or owner_id != job.user_id
    except Exception:  # noqa: BLE001
        # 不能证明为作者时不写可能包含正文或模型细节的失败日志。
        return True


async def _restore_collaborator_claim(
    job: ChapterJob, sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """撤权后只释放原执行的原章占用；不写任何提示词、模型、异常或成功日志。"""
    execution, claim = job.execution, job.chapter_claim
    try:
        if (type(execution) is not TaskExecutionIdentity or type(claim) is not ChapterClaimIdentity
                or execution.grant_identity is None or claim.grant_identity != execution.grant_identity
                or not chapter_claim._valid_identity(claim) or claim.phase != "running"
                or (claim.task_id, claim.task_key, claim.actor_id, claim.project_id, claim.execution_key,
                    chapter_claim._utc(claim.queued_at))
                != (execution.task_id, execution.task_key, execution.actor_id, execution.project_id,
                    execution.execution_key, execution.queued_at)
                or (job.task_id, job.task_key, job.user_id, job.project_id, job.chapter_id)
                != (claim.task_id, claim.task_key, claim.actor_id, claim.project_id, claim.chapter_id)):
            return
        if claim.kind == "single":
            pointer = {"chapter_id": claim.chapter_id, "claim_key": claim.claim_key, "prev_status": claim.prev_status}
            if execution.chapter_id != claim.chapter_id or execution.input.get("_chapter_claim") != pointer:
                return
        elif (type(job.batch_attempt) is not collaboration_ai_guard.BatchAttemptIdentity
              or job.batch_attempt.claim != claim):
            return
        async with sessionmaker() as db:
            with db.no_autoflush:
                await project_lifecycle.lock_record(db, claim.project_id)
                task = await db.scalar(select(Task).where(
                    Task.id == claim.task_id, Task.statistics_key == claim.task_key,
                ).with_for_update())
                if claim.kind == "single":
                    valid = matches_running(execution, task)
                else:
                    valid = (matches_running_base(execution, task)
                             and collaboration_ai_guard._matches_batch_attempt(task, execution, job.batch_attempt))
                if not valid or not await chapter_claim.release_chapter_claim(db, claim):
                    return
                await db.commit()
    except Exception:  # noqa: BLE001
        # 身份、轮次或数据库不能证明时保留占用；不把请求或异常写入应用日志。
        logger.error("协作者章节占用回退失败")


async def _drain_cleanup(coro) -> None:
    """独立任务跑完事务和日志；原生 asyncio 取消不能打断数据库游标。"""
    work = asyncio.create_task(coro)
    cancelled = False
    while True:
        try:
            await asyncio.shield(work)
            break
        except asyncio.CancelledError:
            cancelled = True
            if work.done():
                await work
                break
    if cancelled:
        raise asyncio.CancelledError


async def _admin_cancel_pending(sessionmaker: async_sessionmaker[AsyncSession], claim: ChapterClaimIdentity) -> bool:
    """只将本执行轮次已持久化的管理员取消意图记为取消，不猜其他任务的原因。"""
    try:
        async with sessionmaker() as db:
            operation = await db.scalar(select(Task.admin_cancel_operation_id).where(
                Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.user_id == claim.actor_id,
                Task.project_id == claim.project_id, Task.status == "running", Task.queued_at == claim.queued_at,
                Task.execution_key == claim.execution_key,
            ))
            return operation is not None
    except Exception:  # noqa: BLE001
        logger.error("核对章节取消意图失败")
        return False


def _below_target(text: str, target_words: int) -> bool:
    return count_words(text) < CONTINUE_RATIO * target_words


async def _save_chapter_result(
    job: ChapterJob,
    sessionmaker: async_sessionmaker[AsyncSession],
    contents: dict[int, str],
    log_values: dict[str, Any],
) -> dict[str, Any] | None:
    """当前章所有提案在短事务一次 CAS 保存；极晚取消以实际 commit 为准。"""
    cancelled = asyncio.Event()

    async def save() -> dict[str, Any] | None:
        async with sessionmaker() as db:
            if job.chapter_claim is not None:
                if job.execution is not None:
                    if job.model_binding is None or job.model_cipher is None or job.context_stamp is None:
                        raise ConflictError("任务模型绑定缺失，请重新发起任务")
                    guard = collaboration_ai_guard.for_task(
                        job.execution, job.model_binding, job.context_stamp.visibility, cipher=job.model_cipher,
                        allow_private=job.model_allow_private, scene=job.context_stamp, chapter=job.chapter_claim,
                        batch_attempt=job.batch_attempt,
                        extra_bindings=((job.embedding_selection,) if job.embedding_selection is not None else ()),
                    )
                    await guard.require_task_write(db)
                else:
                    await chapter_claim.require_running_chapter(db, job.chapter_claim)
            else:
                await setting_ai_visibility.lock_author_visibility_write(db, job.user_id, job.project_id, manage=False)
            try:
                chapter, _project = await _actor_chapter_project(
                    db, job.user_id, job.chapter_id, job.project_id,
                    job.chapter_claim.grant_identity if job.chapter_claim is not None else None, lock=True,
                )
            except NotFoundError:
                return None
            await db.refresh(chapter)
            if chapter.status != "generating":
                raise ConflictError("章节生成状态已被复位，生成结果未保存")
            assert job.context_stamp is not None
            await scene_ai_context.require_stamp(db, job.context_stamp)
            scenes = await scene_service.list_active_scenes(db, chapter.id)
            if set(contents) != {scene.id for scene in scenes}:
                raise ConflictError("场景结构已变化，请重新发起操作", extra={"code": "scene_context_changed"})
            rich_ids = await scene_service._rich_scene_ids(db, [scene.id for scene in scenes])
            plans = None
            if rich_ids:
                from app.services.rich_document_proposal_mapping import build_scene_proposal
                from app.services.rich_document_store import capture_documents

                documents, _ = await capture_documents(db, scenes)
                plans = {scene.id: build_scene_proposal(documents[scene.id], contents[scene.id]) for scene in scenes}
                if any(plan.requires_review for plan in plans.values()):
                    from app.services.rich_document_review import persist_pending

                    review = await persist_pending(db, chapter, job, plans, log_values)
                    if cancelled.is_set():
                        raise asyncio.CancelledError
                    await db.commit()
                    return {"rich_review": review}
            await version_service.ensure_baseline(db, chapter)
            if plans is not None:
                from app.services.rich_document_proposal_mapping import resolve_scene_proposal
                from app.services.rich_document_service import write_mapped_document

                for current in scenes:
                    plan = plans[current.id]
                    if current.content == contents[current.id]:
                        continue
                    if current.id in rich_ids:
                        mapped = resolve_scene_proposal(plan, {}, confirm_format_loss=False)
                        await write_mapped_document(db, job.project_id, current, plan.original, mapped,
                                                    job.context_stamp.content_revision)
                    else:
                        current.content = contents[current.id]
                await scene_service.refresh_projection(
                    db, chapter, expected_revision=job.context_stamp.content_revision,
                )
            elif isinstance(job.target, SceneTarget):
                scene = await scene_service.require_scene(db, chapter, job.target.scene_id)
                await scene_service.replace_scene_content(
                    db, chapter, scene, contents[scene.id], expected_revision=job.context_stamp.content_revision,
                )
            else:
                await scene_service.replace_chapter_contents(
                    db, chapter, contents, expected_revision=job.context_stamp.content_revision,
                )
            if job.chapter_claim is not None and not await chapter_claim.release_chapter_claim(db, job.chapter_claim):
                raise ConflictError("章节生成状态已变化，生成结果未保存", extra={"code": "chapter_claim_changed"})
            chapter.status = "done"
            log = await generation_log_service.write_log(db, **log_values, model_binding=job.model_binding)
            version = await version_service.record(db, chapter, job.mode, log_id=log.id)
            await db.flush()
            await db.refresh(chapter)
            chapter_view = (await chapter_ai_view(db, job.user_id, job.project_id, chapter)
                            if job.chapter_claim is not None and job.chapter_claim.grant_identity is not None
                            else ChapterOut.model_validate(chapter))
            payload: dict[str, Any] = {"chapter": chapter_view.model_dump(mode="json")}
            if isinstance(job.target, SceneTarget):
                scene = await scene_service.require_scene(db, chapter, job.target.scene_id)
                payload["scene"] = (await scene_service.scene_out(db, chapter, scene)).model_dump(mode="json")
            else:
                payload["scene_ids"] = [scene["id"] for scene in job.scenes]
            if log_values.get("error"):
                payload["warning"] = log_values["error"]
            if cancelled.is_set():
                raise asyncio.CancelledError
            receipt = None
            if job.chapter_claim is not None:
                if job.chapter_claim.kind == "batch":
                    from app.services import batch_service

                    receipt = await batch_service.record_chapter_completion(
                        db, job.chapter_claim, version_id=version.id, content_revision=chapter.content_revision,
                    )
                else:
                    receipt = await _write_completion_receipt(
                        db, job.chapter_claim, version, chapter.content_revision, payload,
                        log=log, binding=job.model_binding,
                    )
            if cancelled.is_set():
                raise asyncio.CancelledError
            try:
                await db.commit()
            except Exception:
                if job.chapter_claim is None:
                    raise
                # COMMIT 可能已经成功：只核验本次原身份的收据，不重发模型也不重写正文。
                try:
                    await db.rollback()
                except Exception:
                    pass
                try:
                    async with sessionmaker() as proof:
                        if job.chapter_claim.kind == "batch":
                            from app.services import batch_service

                            confirmed = await batch_service.confirm_chapter_completion(
                                proof, job.chapter_claim, receipt,
                            )
                        else:
                            confirmed = await completed_chapter_receipt(proof, job.chapter_claim) == receipt
                except Exception:
                    confirmed = False
                if not confirmed:
                    raise ChapterCommitUncertain("章节结果提交状态待核对") from None
            return payload

    saving = asyncio.create_task(save())
    try:
        return await asyncio.shield(saving)
    except asyncio.CancelledError:
        cancelled.set()
        out = await saving
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        return out


async def run_chapter_job(job: ChapterJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    started = time.monotonic()
    finished = False
    commit_uncertain = False
    fail_status, fail_error = "cancelled", ""
    produced: list[str] = []
    rounds = 0
    warnings: list[str] = []
    log_messages: list[ChatMessage] = []
    contents = {scene["id"]: scene["content"] for scene in job.scenes}
    by_step = {step["id"]: step for step in job.steps}

    def log_kwargs(status: str, error: str, output_chars: int) -> dict[str, Any]:
        params = dict(job.params)
        if job.mode == "generate":
            params["continuation_rounds"] = rounds
        return {
            "project_id": job.project_id, "actor_user_id": job.user_id, "chapter_id": job.chapter_id, "kind": job.mode,
            "ai_config_id": job.ai_config_id, "provider": job.provider_name, "model": job.model,
            "params": params, "messages": log_messages or job.messages, "status": status, "error": error,
            "output_chars": output_chars, "duration_ms": elapsed_ms(started), "task_id": job.task_id,
        }

    try:
        previous = ""
        for index, scene in enumerate(job.scenes, 1):
            step = by_step.get(scene["id"])
            if isinstance(job.target, SceneTarget) and step is None:
                if scene["sort"] < job.steps[0]["sort"]:
                    previous += scene["separator_before"] + scene["content"]
                continue
            if not isinstance(job.target, SceneTarget) and scene["separator_before"]:
                produced.append(scene["separator_before"])
                yield "delta", {"text": scene["separator_before"]}
            if step is None:
                if scene["content"]:
                    produced.append(scene["content"])
                    yield "delta", {"text": scene["content"]}
                previous += scene["separator_before"] + scene["content"]
                continue
            if len(job.scenes) > 1 or isinstance(job.target, SceneTarget):
                label = {"generate": "生成", "polish": "润色", "revise": "重写"}[job.mode]
                yield "stage", {"stage": f"场景 {index}/{len(job.scenes)} · {label} · 目标 {step['target_words']} 字"}
            if step is not job.steps[0]:
                async with sessionmaker() as db:
                    await _prepare_scene_step(db, job, step, previous)
            # messages 已绑定同一个快照，provider 的实际发送门禁也再次检查。
            log_messages.extend(job.messages)
            pieces: list[str] = []
            async with aclosing(stream_pieces(
                job.provider, job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking, pieces=pieces,
            )) as events:
                async for event, data in events:
                    produced.append(data["text"])
                    yield event, data
            text = "".join(pieces).strip()
            if not text:
                raise AIProviderError("模型未返回任何内容")
            step_rounds = 0
            while job.auto_continue and step_rounds < MAX_CONTINUE_ROUNDS and _below_target(text, step["target_words"]):
                step_rounds += 1
                rounds += 1
                words = count_words(text)
                yield "continue", {"round": step_rounds, "words": words, "target": step["target_words"]}
                try:
                    messages = build_continuation_messages(
                        messages=job.messages, generated=text, current_words=words,
                        target_words=step["target_words"], override=job.continue_override,
                    )
                except TemplateRenderError as exc:
                    warnings.append(f"自动续写失败：{exc.detail}")
                    break
                log_messages += [{"role": "assistant", "content": f"（续写前已有 {words} 字，正文省略）"}, messages[-1]]
                continuation: list[str] = []
                try:
                    async with aclosing(stream_pieces(
                        job.provider, messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking,
                        pieces=continuation, prefix="\n\n",
                    )) as events:
                        async for event, data in events:
                            produced.append(data["text"])
                            yield event, data
                except AppError:
                    # 过期上下文/策略错误不是可容忍的额外续写失败，不能采纳旧首轮结果。
                    raise
                except AIProviderError as exc:
                    warnings.append(f"自动续写失败：{exc}")
                    break
                except Exception as exc:
                    logger.error("章节 %s 自动续写失败 error_type=%s", job.chapter_id, type(exc).__name__)
                    warnings.append("自动续写失败：服务器内部错误")
                    break
                piece = "".join(continuation).strip()
                if not piece:
                    break
                text = f"{text}\n\n{piece}"
            contents[step["id"]] = text
            previous += scene["separator_before"] + text
        final_text = (contents[job.target.scene_id] if isinstance(job.target, SceneTarget) else
                      "".join(scene["separator_before"] + contents[scene["id"]] for scene in job.scenes))
        warning = "；".join(warnings)
        out = await _save_chapter_result(job, sessionmaker, contents,
                                         log_kwargs("success", warning, count_words(final_text)))
        if out is None:
            raise NotFoundError("章节已被删除，生成结果未保存")
        finished = True
        if "rich_review" in out:
            yield "paused", out["rich_review"]
            return
        if warning:
            out["warning"] = warning
        yield "done", out
    except ChapterCommitUncertain:
        # 不能把未知提交翻成普通失败后触发 cleanup 或允许 retry。
        commit_uncertain = True
        raise
    except AppError as exc:
        fail_status, fail_error = "error", exc.detail
        yield "error", {"detail": exc.detail, **(exc.extra or {}), "retryable": False}
    except AIProviderError as exc:
        fail_status, fail_error = "error", str(exc)
        yield "error", {"detail": fail_error}
    except Exception as exc:
        logger.error("章节 %s 生成失败 error_type=%s", job.chapter_id, type(exc).__name__)
        fail_status, fail_error = "error", "生成失败：服务器内部错误"
        yield "error", {"detail": fail_error}
    finally:
        if not finished and not commit_uncertain:
            async def cleanup() -> None:
                if await _is_collaborator_job(job, sessionmaker):
                    await _restore_collaborator_claim(job, sessionmaker)
                    return
                try:
                    await _restore_status(sessionmaker, job.chapter_id, job.prev_status, job.chapter_claim)
                finally:
                    status, error = fail_status, fail_error
                    if status == "error" and job.chapter_claim is not None and await _admin_cancel_pending(
                        sessionmaker, job.chapter_claim,
                    ):
                        status, error = "cancelled", ""
                    await generation_log_service.save_log(
                        sessionmaker, **log_kwargs(status, error, count_words("".join(produced))),
                        model_binding=job.model_binding,
                    )

            await _drain_cleanup(cleanup())
