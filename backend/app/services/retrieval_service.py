"""统一检索基建：知识库分块与剧情记忆共用的打分、向量存取、缓存与「需要重建」判定。

存储（retrieval_chunks）：每个检索单元一行，source ∈ kb / memory，ref_id 指向业务表。业务写入点只经本模块的
upsert_many / set_flags / delete 维护；不 commit（与业务数据同一事务提交）。

打分（search）：
- 关键词：中文按字符二元组、连续英文数字为一个词（小写）、标点与空白切分、中文单字不算词（tokenize）。
  BM25（k1=1.2，b=0.75），文档频率按「项目 + source」过滤后的候选集合即时统计；原始分数除以本次查询的最高分 → [0, 1]。
  只用关键词时，关键词分为 0 的候选不算命中。
- 向量：查询与文档向量都已归一化（float32），点积即余弦，映射为 (cos + 1) / 2 → [0, 1]。
  只有当**全部**候选都带有当前向量模型（embedding_key = provider:model:dim）的向量时才做混合排序；
  否则（更换了模型、重建 / 建索引尚未完成、某些块向量生成失败）整次查询只用关键词，旧向量一律不用。
- 混合：最终分 = 0.7 × 向量分 + 0.3 × 关键词分；min_score 对最终分生效（阈值以下不返回）。
- 查询向量生成失败（未配置、超时、提供方报错）时退回关键词，并在结果里给出 warning，不抛错。

缓存：进程内按 (project_id, source) 缓存候选矩阵（不含正文），失效标记为
(行数, max(id), sum(version), sum(chapter_no))：插入使 max(id) 增大
（表开启 sqlite_autoincrement，PostgreSQL 序列本就不复用），
删除使行数变化，更新使 version 递增，章节号平移使 sum(chapter_no) 变化。命中的正文按 id 再查一次。
"""

import asyncio
import hashlib
import json
import logging
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import batched
from typing import TYPE_CHECKING, Literal

import numpy as np
from sqlalchemy import Text, and_, case, cast, delete, exists, func, inspect, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.providers import AIProviderError, LLMProvider, ProviderFactory
from app.ai.providers.base import RequestPolicyError
from app.core.errors import BadRequestError, ConflictError
from app.core.security import SecretCipher
from app.models import (
    AIConfig,
    AnalysisIndexReceipt,
    Chapter,
    ChapterAnalysis,
    KbChunk,
    KbDocument,
    Project,
    RetrievalChunk,
    SceneReviewAnalysisReceipt,
    StoryMemory,
    Task,
)
from app.services.ai_dispatch import (
    VisibilityChangedError,
    VisibilityPolicyError,
    VisibilityStamp,
    _bind_chapter_guard,
    _bound_chapter,
    _GuardedProvider,
    visibility_sessionmaker,
)
from app.services.ai_usage_recording import inherit_meter, meter_provider, unmeter_provider

if TYPE_CHECKING:
    from app.services.chapter_claim import ChapterClaim
    from app.services.project_ai_config import ResolvedSharedEmbedding
    from app.services.task_engine.execution_identity import TaskExecutionIdentity

logger = logging.getLogger(__name__)

Source = Literal["kb", "memory"]
SearchMode = Literal["hybrid", "keyword"]

BM25_K1 = 1.2
BM25_B = 0.75
VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3
# 每批最多 32 条、单条截断到 2000 字
EMBED_BATCH = 32
EMBED_TEXT_CHARS = 2000
# 生成提示词时为查询求向量的超时（秒）；超时退回关键词检索
QUERY_EMBED_TIMEOUT_SECONDS = 20

KEYWORD_ONLY_STALE = "部分内容尚未生成当前向量模型的向量（需要重建索引或建索引尚未完成），本次只用关键词检索"

_TOKEN_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]+|[a-z0-9]+")
_ASCII_RE = re.compile(r"[a-z0-9]")


# ---------------- 分词（纯函数） ----------------


def tokenize(text: str) -> list[str]:
    """中文（含日文假名）按字符二元组；连续英文数字为一个词（NFKC + 小写）；单个中文字不算词。"""
    norm = unicodedata.normalize("NFKC", text or "").lower()
    tokens: list[str] = []
    for m in _TOKEN_RE.finditer(norm):
        run = m.group(0)
        if _ASCII_RE.match(run):
            tokens.append(run)
            continue
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def term_freq(text: str) -> dict[str, int]:
    return dict(Counter(tokenize(text)))


# ---------------- 向量（纯函数） ----------------


def normalize_vector(values: Sequence[float]) -> np.ndarray | None:
    """归一化为 float32；全零 / 非有限值返回 None。"""
    vec = np.asarray(values, dtype=np.float32)
    if vec.ndim != 1 or vec.size == 0 or not np.all(np.isfinite(vec)):
        return None
    norm = float(np.linalg.norm(vec))
    if norm <= 0:
        return None
    return (vec / norm).astype(np.float32)


def encode_vector(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype="<f4").tobytes()


def decode_vector(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<f4")


def make_embedding_key(model_key: str, dim: int) -> str:
    return f"{model_key}:{dim}"[:200]


def key_matches_model(embedding_key: str | None, model_key: str | None) -> bool:
    """embedding_key（provider:model:dim）是否属于当前向量模型（provider:model）。模型名本身可以含冒号。"""
    if not embedding_key or not model_key:
        return False
    prefix = f"{model_key}:"
    if not embedding_key.startswith(prefix):
        return False
    dim = embedding_key[len(prefix):]
    return dim.isdigit()


def config_model_key(cfg: AIConfig) -> str:
    return f"{cfg.provider}:{cfg.model}"


# ---------------- 向量模型 ----------------


@dataclass
class Embedder:
    """当前用户的默认向量模型。embed 分批、截断、归一化；失败抛 AIProviderError。"""

    provider: LLMProvider = field(repr=False)
    provider_name: str = field(repr=False)
    model: str = field(repr=False)
    config_name: str = field(default="", repr=False)
    dim: int | None = None
    _cache: dict[str, np.ndarray] = field(default_factory=dict, repr=False)
    _cache_check: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    _guard_state: "_GuardedEmbeddingState | None" = field(default=None, repr=False)

    @property
    def model_key(self) -> str:
        if self._guard_state is not None:
            self._guard_state.check(self)
        return f"{self.provider_name}:{self.model}"

    @property
    def key(self) -> str | None:
        return make_embedding_key(self.model_key, self.dim) if self.dim else None

    async def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        state = self._guard_state
        if state is not None:
            state.check(self, texts)
        if self._cache_check is not None and any(text in self._cache for text in texts):
            # 缓存不是授权证明；撤权后不得由缓存绕过受控检查。
            await self._cache_check()
            if state is not None:
                state.check(self, texts)
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        for start in range(0, len(missing), EMBED_BATCH):
            batch = missing[start : start + EMBED_BATCH]
            if state is not None:
                state.check(self)
            try:
                raw = await self.provider.embed([t[:EMBED_TEXT_CHARS] for t in batch], self.model)
            except Exception:
                if state is not None:
                    state.check(self, texts)
                raise
            if state is not None:
                state.check(self)
            if len(raw) != len(batch):
                raise AIProviderError("模型调用失败：返回的向量数量与请求不符")
            for text, values in zip(batch, raw, strict=True):
                vec = normalize_vector(values)
                if vec is None:
                    raise AIProviderError("模型调用失败：返回了空向量")
                if self.dim is None:
                    self.dim = int(vec.size)
                    if state is not None:
                        state.dim = self.dim
                elif vec.size != self.dim:
                    raise AIProviderError(f"模型调用失败：向量维度不一致（{vec.size} ≠ {self.dim}）")
                self._cache[text] = vec
                if state is not None:
                    state.record(text, vec)
        if state is not None:
            state.check(self, texts)
            # 外层检索可使用结果，但不能通过修改返回数组污染同次执行的缓存。
            return [self._cache[t].copy() for t in texts]
        return [self._cache[t] for t in texts]


class EmbeddingPolicyError(VisibilityPolicyError):
    """授权、轮次或模型校验失败时不可退化为普通网络失败。"""

    def __init__(self) -> None:
        super().__init__("检索授权或模型配置已变化，操作已停止", extra={"code": "permission_denied"})


@dataclass(repr=False)
class _GuardedEmbeddingState:
    """冻结一次执行的模型身份及内部缓存来源；仅绑定 guard 创建的新缓存。"""

    provider: _GuardedProvider
    inner_provider: LLMProvider
    bound_transport: LLMProvider | None
    chapter: "ChapterClaim | None"
    stamp: VisibilityStamp
    sessionmaker: async_sessionmaker[AsyncSession]
    bind: object
    runtime: object
    authorize: Callable[[AsyncSession], Awaitable[None]] | None
    verify: Callable[[AsyncSession], Awaitable[None]] | None
    verify_preflight: Callable[[AsyncSession], Awaitable[None]] | None
    provider_config: tuple[object, ...]
    inner_config: tuple[object, ...]
    transport_config: tuple[object, ...] | None
    model: str
    provider_name: str
    dim: int | None
    cache_check: Callable[[], Awaitable[None]]
    signatures: dict[str, bytes] = field(default_factory=dict)

    @staticmethod
    def _provider_config(provider: LLMProvider) -> tuple[object, ...]:
        return provider.api_key, provider.base_url, provider.follow_redirects, provider.uses_dispatch_hooks

    @staticmethod
    def _signature(vector: np.ndarray, dim: int | None) -> bytes:
        if (type(vector) is not np.ndarray or vector.ndim != 1 or vector.dtype != np.float32
                or dim is None or vector.size != dim):
            raise EmbeddingPolicyError
        return hashlib.sha256(vector.tobytes()).digest()

    def record(self, text: str, vector: np.ndarray) -> None:
        self.signatures[text] = self._signature(vector, self.dim)

    def check(self, embedder: Embedder, texts: Sequence[str] = ()) -> None:
        if (embedder._guard_state is not self or embedder.provider is not self.provider
                or embedder.model != self.model or embedder.provider_name != self.provider_name
                or embedder.dim != self.dim
                or embedder._cache_check is not self.cache_check or self.provider._provider is not self.inner_provider
                or self.provider._stamp != self.stamp or self.provider._sessionmaker is not self.sessionmaker
                or self.provider._chapter is not self.chapter
                or self.sessionmaker.kw.get("bind") is not self.bind
                or self.sessionmaker.kw.get("info", {}).get("authorization_runtime") is not self.runtime
                or self.provider._authorize is not self.authorize or self.provider._verify is not self.verify
                or self.provider._verify_preflight is not self.verify_preflight
                or self._provider_config(self.provider) != self.provider_config
                or self._provider_config(self.inner_provider) != self.inner_config):
            raise EmbeddingPolicyError
        if _bound_chapter(self.provider) is not self.chapter:
            raise EmbeddingPolicyError
        if self.bound_transport is not None:
            from app.services.collaboration_ai_guard import _BoundModelProvider

            if (type(self.inner_provider) is not _BoundModelProvider
                    or self.inner_provider._provider is not self.bound_transport
                    or self._provider_config(self.bound_transport) != self.transport_config):
                raise EmbeddingPolicyError
            self.inner_provider._check(self.model, "embedding")
        for text in set(texts):
            if text in embedder._cache and (
                text not in self.signatures
                or self._signature(embedder._cache[text], self.dim) != self.signatures[text]
            ):
                raise EmbeddingPolicyError


@dataclass
class _EmbeddingGuardProgress:
    checking: bool = False
    verified: bool = False


_EMBEDDING_GUARD_PROGRESS: ContextVar[_EmbeddingGuardProgress | None] = ContextVar(
    "embedding_guard_progress", default=None,
)


def _policy_cause(exc: BaseException) -> RequestPolicyError | None:
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending and len(seen) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, RequestPolicyError):
            return current
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return None


@dataclass(frozen=True, slots=True, repr=False)
class _PolicyCallbacks:
    checks: tuple[Callable[[AsyncSession], Awaitable[None]], ...]

    async def __call__(self, db: AsyncSession) -> None:
        for check in self.checks:
            try:
                await check(db)
            except VisibilityPolicyError:
                raise
            except Exception:
                # 不泄露 DB、配置或账号细节，并保证 SDK 不把拒绝翻成网络失败。
                raise EmbeddingPolicyError from None


def _same_callback(first: Callable, second: Callable) -> bool:
    if first is second:
        return True
    first_owner, first_func = getattr(first, "__self__", None), getattr(first, "__func__", None)
    return (first_owner is not None and first_func is not None
            and first_owner is getattr(second, "__self__", None) and first_func is getattr(second, "__func__", None))


def _callback_items(callback: Callable | None) -> tuple[Callable, ...]:
    if callback is None:
        return ()
    return callback.checks if isinstance(callback, _PolicyCallbacks) else (callback,)


def _policy_callbacks(*callbacks: Callable[[AsyncSession], Awaitable[None]] | None):
    checks: list[Callable[[AsyncSession], Awaitable[None]]] = []
    for callback in callbacks:
        for check in _callback_items(callback):
            if not any(_same_callback(check, previous) for previous in checks):
                checks.append(check)
    return _PolicyCallbacks(tuple(checks)) if checks else None


def _preflight_callback(callback: Callable[[AsyncSession], Awaitable[None]] | None):
    """已知发送守卫必须换用对应的非消费检查；其他附加验证需为无副作用的纯检查。"""
    from app.services.collaboration_ai_guard import SendGuard

    checks = []
    for check in _callback_items(callback):
        owner = getattr(check, "__self__", None)
        if type(owner) is SendGuard and getattr(check, "__func__", None) is SendGuard.verify:
            checks.append(owner.verify_without_one_shot)
        else:
            checks.append(check)
    return _policy_callbacks(*checks)


class _GuardedEmbeddingProvider(_GuardedProvider):
    """连门禁或数据库本身不可用也必须阻止发送，不能误报为普通模型故障。"""

    @asynccontextmanager
    async def _send_lease(self):
        entered = False
        progress = _EMBEDDING_GUARD_PROGRESS.get()
        if progress is not None:
            progress.checking = True
        try:
            async with super()._send_lease():
                entered = True
                if progress is not None:
                    progress.checking, progress.verified = False, True
                yield
        except VisibilityPolicyError:
            raise
        except Exception:
            if entered:
                raise
            raise EmbeddingPolicyError from None

    async def preflight(self) -> None:
        progress = _EMBEDDING_GUARD_PROGRESS.get()
        if progress is not None:
            progress.checking = True
        try:
            await super().preflight()
        except VisibilityPolicyError:
            raise
        except Exception:
            raise EmbeddingPolicyError from None
        if progress is not None:
            progress.checking, progress.verified = False, True

    @asynccontextmanager
    async def _hooks(self):
        entered = False
        progress = _EMBEDDING_GUARD_PROGRESS.get()
        fake = not self._provider.uses_dispatch_hooks
        if progress is not None and fake:
            progress.checking = True
        try:
            async with super()._hooks() as hooks:
                entered = True
                if progress is not None and fake:
                    progress.checking, progress.verified = False, True
                yield hooks
        except VisibilityPolicyError:
            raise
        except Exception:
            if entered:
                raise
            raise EmbeddingPolicyError from None


def guard_embedder(
    embedder: Embedder | None, stamp: VisibilityStamp, sessionmaker: async_sessionmaker[AsyncSession],
    *, authorize: Callable[[AsyncSession], Awaitable[None]] | None = None,
    verify: Callable[[AsyncSession], Awaitable[None]] | None = None,
    chapter: "ChapterClaim | None" = None,
) -> Embedder | None:
    """在同一真实发送 lease 中保留旧可见性、认证及模型绑定检查。"""
    if embedder is None:
        if chapter is not None:
            raise EmbeddingPolicyError
        return None
    if embedder._guard_state is not None:
        embedder._guard_state.check(embedder)
    provider = embedder.provider
    verify_preflight = None
    if isinstance(provider, _GuardedProvider):
        original_chapter = _bound_chapter(provider)
        if chapter is not None and chapter is not original_chapter:
            raise EmbeddingPolicyError
        chapter = original_chapter
        if provider._stamp != stamp or provider._sessionmaker.kw.get("bind") is not sessionmaker.kw.get("bind"):
            raise VisibilityChangedError
        old_info, new_info = provider._sessionmaker.kw.get("info", {}), sessionmaker.kw.get("info", {})
        if old_info.get("authorization_runtime") is not new_info.get("authorization_runtime"):
            raise VisibilityChangedError
        authorize = _policy_callbacks(provider._authorize, authorize)
        # 已有发送验证不得因传入同一 bound method 被重复执行或混入预检。
        if verify is not None and all(
            any(_same_callback(check, previous) for previous in _callback_items(provider._verify))
            for check in _callback_items(verify)
        ):
            verify = None
        # 独立保留原本的非消费预检；额外验证不能只加在实际发送上。
        original_preflight = provider._verify_preflight if provider._verify_preflight is not None else provider._verify
        verify_preflight = _policy_callbacks(original_preflight, _preflight_callback(verify))
        verify = _policy_callbacks(provider._verify, verify)
        sessionmaker, provider = provider._sessionmaker, provider._provider
    else:
        if chapter is not None:
            # 章来源必须已由章节专用工厂和 wrapper 登记，不能只靠 caller 声称。
            raise EmbeddingPolicyError
        authorize = _policy_callbacks(authorize)
        verify_preflight = _preflight_callback(verify)
        verify = _policy_callbacks(verify)

    provider = unmeter_provider(provider)
    guarded = _GuardedEmbeddingProvider(
        provider, stamp, sessionmaker, authorize=authorize, verify=verify, verify_preflight=verify_preflight,
        chapter=chapter,
    )
    if chapter is not None:
        _bind_chapter_guard(guarded, chapter)
    inherit_meter(guarded, embedder.provider)

    async def check_cache() -> None:
        try:
            # 命中也须持同一真实认证／可见性门禁，尤其章节 claim 要求物理 lease 证明。
            # 本次预检结束立即释放，后续真实发送仍需独立检查。
            await guarded.preflight()
        except VisibilityPolicyError:
            raise
        except Exception:
            raise EmbeddingPolicyError from None

    result = replace(embedder, provider=guarded, _cache={}, _cache_check=check_cache, _guard_state=None)
    from app.services.collaboration_ai_guard import _BoundModelProvider

    transport = provider._provider if type(provider) is _BoundModelProvider else None
    provider_config = _GuardedEmbeddingState._provider_config
    result._guard_state = _GuardedEmbeddingState(
        provider=guarded, inner_provider=provider, bound_transport=transport, chapter=chapter,
        stamp=stamp, sessionmaker=sessionmaker,
        bind=sessionmaker.kw.get("bind"), runtime=sessionmaker.kw.get("info", {}).get("authorization_runtime"),
        authorize=authorize, verify=verify, verify_preflight=verify_preflight, provider_config=provider_config(guarded),
        inner_config=provider_config(provider),
        transport_config=provider_config(transport) if transport is not None else None,
        model=result.model, provider_name=result.provider_name,
        dim=result.dim, cache_check=check_cache,
    )
    return result


async def default_embedding_config(db: AsyncSession, user_id: int) -> AIConfig | None:
    """用户的默认向量模型配置（没有默认时取最新一条）；没有配置返回 None。"""
    return await db.scalar(
        select(AIConfig)
        .where(AIConfig.user_id == user_id, AIConfig.model_type == "embedding")
        .order_by(AIConfig.is_default.desc(), AIConfig.id.desc())
        .limit(1)
    )


async def current_model_key(db: AsyncSession, user_id: int) -> str | None:
    cfg = await default_embedding_config(db, user_id)
    return config_model_key(cfg) if cfg is not None else None


async def resolve_embedder(
    db: AsyncSession,
    user_id: int,
    *,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    project_id: int | None = None,
    chapter_id: int | None = None,
    chapter_created_at: datetime | None = None,
    execution: "TaskExecutionIdentity | None" = None,
    usage_kind: str = "retrieval.query",
) -> tuple[Embedder | None, str | None]:
    """构建默认向量模型，返回 (向量模型, 不可用原因)。

    没有配置 → (None, None)；配置了但不可用（解密失败、端点被拦截）→ (None, 原因)，供提示区分两种情况。
    """
    from app.services import ai_config_service

    cfg = await default_embedding_config(db, user_id)
    if cfg is None:
        return None, None
    try:
        provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    except BadRequestError as exc:
        logger.warning("向量模型配置 %s 不可用：%s", cfg.id, exc.detail)
        return None, f"「{cfg.name}」{exc.detail}"
    provider = meter_provider(
        provider, cfg, visibility_sessionmaker(db), actor_id=user_id, kind=usage_kind,
        project_id=project_id, project_owner_id=user_id if project_id is not None else None,
        chapter_id=chapter_id, chapter_created_at=chapter_created_at,
        task_id=execution.task_id if execution is not None else None,
        task_key=execution.task_key if execution is not None else None,
    )
    return Embedder(provider=provider, provider_name=cfg.provider, model=cfg.model, config_name=cfg.name), None


async def get_embedder(
    db: AsyncSession,
    user_id: int,
    *,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    project_id: int | None = None,
    chapter_id: int | None = None,
    chapter_created_at: datetime | None = None,
) -> Embedder | None:
    """构建默认向量模型；没有配置或配置不可用时返回 None（调用方退回关键词检索）。"""
    embedder, _ = await resolve_embedder(
        db, user_id, cipher=cipher, factory=factory, ai_timeout=ai_timeout, allow_private=allow_private,
        project_id=project_id, chapter_id=chapter_id, chapter_created_at=chapter_created_at,
    )
    return embedder


@dataclass(frozen=True)
class EmbedEnv:
    """构建向量模型所需的依赖（路由经 deps.EmbedEnvDep 注入）。"""

    cipher: SecretCipher
    factory: ProviderFactory
    ai_timeout: float
    allow_private: bool

    async def embedder(
        self, db: AsyncSession, user_id: int, *, project_id: int | None = None,
        chapter_id: int | None = None, chapter_created_at: datetime | None = None,
    ) -> Embedder | None:
        return await get_embedder(
            db,
            user_id,
            cipher=self.cipher,
            factory=self.factory,
            ai_timeout=self.ai_timeout,
            allow_private=self.allow_private,
            project_id=project_id,
            chapter_id=chapter_id,
            chapter_created_at=chapter_created_at,
        )


async def embed_query(embedder: Embedder | None, text: str) -> tuple[np.ndarray | None, str]:
    """为查询求向量；失败返回 (None, warning)。"""
    if embedder is None or not text.strip():
        return None, ""
    progress = _EmbeddingGuardProgress() if isinstance(embedder.provider, _GuardedEmbeddingProvider) else None
    token = _EMBEDDING_GUARD_PROGRESS.set(progress)
    try:
        async with asyncio.timeout(QUERY_EMBED_TIMEOUT_SECONDS):
            vec = (await embedder.embed([text]))[0]
    except TimeoutError:
        if embedder._guard_state is not None:
            embedder._guard_state.check(embedder, [text])
        if progress is not None and (progress.checking or not progress.verified):
            raise EmbeddingPolicyError from None
        return None, f"查询向量生成超时（{QUERY_EMBED_TIMEOUT_SECONDS} 秒），本次只用关键词检索"
    except VisibilityPolicyError:
        raise
    except AIProviderError as exc:
        policy = _policy_cause(exc)
        if isinstance(policy, VisibilityPolicyError):
            raise policy from None
        if policy is not None:
            raise EmbeddingPolicyError from None
        return None, f"查询向量生成失败（{exc}），本次只用关键词检索"
    finally:
        _EMBEDDING_GUARD_PROGRESS.reset(token)
    return vec, ""


async def _preflight_guarded_embedder(embedder: Embedder | None, project_id: int) -> None:
    """返回受控检索结果前不能因未用向量而跳过原认证、任务与模型检查。"""
    if embedder is None:
        return
    state = embedder._guard_state
    if state is None:
        if isinstance(embedder.provider, _GuardedEmbeddingProvider):
            raise EmbeddingPolicyError
        return
    state.check(embedder)
    if state.stamp.project_id != project_id:
        raise EmbeddingPolicyError
    await state.cache_check()
    state.check(embedder)


@dataclass(frozen=True, slots=True, repr=False)
class BoundSharedEmbeddingConfig:
    """内部配置快照；只用于绑定空间和构建 provider，绝不返回或记录。"""

    selection: "ResolvedSharedEmbedding"
    cipher: SecretCipher
    allow_private: bool
    config_id: int
    instance_key: str
    owner_id: int
    provider: str
    model: str
    base_url: str | None
    api_key_enc: str

    def __repr__(self) -> str:
        return "<BoundSharedEmbeddingConfig>"


async def load_bound_shared_embedding_config(
    db: AsyncSession, selection: "ResolvedSharedEmbedding", *, cipher: SecretCipher,
    allow_private: bool = False, for_update: bool = False,
) -> BoundSharedEmbeddingConfig:
    """在当前事务内重验原共享选择；写入事务应先持有项目锁并设置 for_update。"""
    from app.services import project_ai_config

    with db.no_autoflush:
        await project_ai_config.revalidate_shared_embedding(
            db, selection, cipher=cipher, allow_private=allow_private, for_update=for_update, lock_config=True,
        )
        binding = selection.model
        if selection.mode != "vector" or binding is None:
            raise EmbeddingPolicyError
        row = (await db.execute(select(
            AIConfig.id, AIConfig.instance_key, AIConfig.user_id, AIConfig.provider, AIConfig.model,
            AIConfig.base_url, AIConfig.api_key_enc,
        ).where(
            AIConfig.id == binding.config_id, AIConfig.user_id == selection.project_owner_id,
            AIConfig.instance_key == binding.config_instance_key, AIConfig.model_type == "embedding",
        ))).one_or_none()
        if row is None:
            raise EmbeddingPolicyError
        return BoundSharedEmbeddingConfig(
            selection=selection, cipher=cipher, allow_private=allow_private, config_id=row.id,
            instance_key=row.instance_key, owner_id=row.user_id, provider=row.provider, model=row.model,
            base_url=row.base_url, api_key_enc=row.api_key_enc,
        )


def _bound_config_identity(config: BoundSharedEmbeddingConfig) -> tuple[object, ...]:
    """绑定配置的身份（配置 ID、实例键、所有者、提供方、端点、模型、密文）；任一变化即不是同一配置。

    只用于写入前比较原快照与锁内重读的当前配置，不作为向量空间键（空间键恒为作者旧键 provider:model:dim，
    E6 裁决 1；原 ``shared:v2`` 空间键原语已于 E6-f 删除）。
    """
    from app.services import project_ai_config

    if (type(config) is not BoundSharedEmbeddingConfig or type(config.config_id) is not int
            or type(config.owner_id) is not int or not 0 < config.config_id <= 2**31 - 1
            or not 0 < config.owner_id <= 2**31 - 1
            or not project_ai_config._instance_key_valid(config.instance_key)
            or type(config.selection) is not project_ai_config.ResolvedSharedEmbedding
            or config.selection.model is None or config.selection.model.config_instance_key != config.instance_key
            or not isinstance(config.provider, str) or not isinstance(config.model, str)
            or not isinstance(config.api_key_enc, str) or not isinstance(config.base_url, (str, type(None)))):
        raise EmbeddingPolicyError
    return (config.config_id, config.instance_key, config.owner_id, config.provider, config.base_url,
            config.model, config.api_key_enc)


@dataclass(frozen=True, slots=True, repr=False)
class EmbeddingInputSnapshot:
    """发送前不可变快照；正文和旧向量不能出现在 repr 或对外响应。"""

    id: int
    project_id: int
    source: Source
    ref_id: int
    version: int
    text: str
    embedding: bytes | None
    embedding_key: str | None
    embed_text: str | None
    source_document_id: int | None
    source_heading: str | None
    source_content: str | None
    source_characters: str | None

    def __repr__(self) -> str:
        return "<EmbeddingInputSnapshot>"


def _invalid_retrieval_reference() -> ConflictError:
    return ConflictError("检索数据关联不一致，无法安全处理", extra={"code": "retrieval_reference_invalid"})


async def snapshot_embedding_inputs(
    db: AsyncSession, project_id: int, ids: Iterable[int], *, allow_orphans: bool = False,
) -> list[EmbeddingInputSnapshot]:
    """批量核验业务来源并返回 SQL 标量快照；调用方先取得本作品访问权限。

    孤儿仅允许显式供后续条件删除；跨作品、错误正文和不能确认归属始终失败关闭。
    """
    requested = list(ids)
    if (type(project_id) is not int or not 0 < project_id <= 2**31 - 1
            or any(type(value) is not int or not 0 < value <= 2**31 - 1 for value in requested)
            or len(set(requested)) != len(requested)):
        raise _invalid_retrieval_reference()
    if not requested:
        return []
    with db.no_autoflush:
        rows = []
        for batch in batched(requested, 400):
            rows.extend((await db.execute(select(
                RetrievalChunk.id, RetrievalChunk.project_id, RetrievalChunk.source, RetrievalChunk.ref_id,
                RetrievalChunk.version, RetrievalChunk.text, RetrievalChunk.embedding, RetrievalChunk.embedding_key,
            ).where(RetrievalChunk.id.in_(batch), RetrievalChunk.project_id == project_id))).all())
        by_id = {row.id: row for row in rows}
        if set(by_id) != set(requested) or any(
            row.source not in ("kb", "memory") or type(row.ref_id) is not int or not 0 < row.ref_id <= 2**31 - 1
            or type(row.version) is not int or not 0 < row.version <= 2**31 - 1 or not isinstance(row.text, str)
            for row in rows
        ):
            raise _invalid_retrieval_reference()

        kb_refs = {row.ref_id for row in rows if row.source == "kb"}
        memory_refs = {row.ref_id for row in rows if row.source == "memory"}
        kb_rows = {}
        for batch in batched(sorted(kb_refs), 400):
            found = (await db.execute(select(
                KbChunk.id, KbChunk.document_id, KbDocument.project_id, KbChunk.heading_path, KbChunk.content,
            ).outerjoin(KbDocument, KbDocument.id == KbChunk.document_id).where(KbChunk.id.in_(batch)))).all()
            kb_rows.update({row.id: row for row in found})
        memory_rows = {}
        for batch in batched(sorted(memory_refs), 400):
            found = (await db.execute(select(
                StoryMemory.id, StoryMemory.project_id, StoryMemory.content, StoryMemory.characters,
                cast(StoryMemory.characters, Text).label("characters_json"),
            ).where(StoryMemory.id.in_(batch)))).all()
            memory_rows.update({row.id: row for row in found})

        snapshots = []
        for row_id in requested:
            row = by_id[row_id]
            source = kb_rows.get(row.ref_id) if row.source == "kb" else memory_rows.get(row.ref_id)
            document_id = heading = content = characters = embed_text = None
            if source is None:
                if not allow_orphans:
                    raise _invalid_retrieval_reference()
            elif source.project_id != project_id:
                raise _invalid_retrieval_reference()
            elif row.source == "kb":
                document_id, heading, content = source.document_id, source.heading_path, source.content
                if not isinstance(heading, str) or not isinstance(content, str):
                    raise _invalid_retrieval_reference()
                embed_text = f"{heading}\n{content}" if heading else content
                if row.text != embed_text:
                    raise _invalid_retrieval_reference()
            else:
                content, characters = source.content, source.characters_json
                names = source.characters or []
                if (not isinstance(content, str) or not isinstance(names, list)
                        or any(not isinstance(name, str) for name in names) or not isinstance(characters, str)):
                    raise _invalid_retrieval_reference()
                indexed = f"{content}\n{' '.join(names)}" if names else content
                if row.text != indexed:
                    raise _invalid_retrieval_reference()
                # 角色名只用于关键词；旧规则发给向量服务的仅是真实记忆正文。
                embed_text = content
            snapshots.append(EmbeddingInputSnapshot(
                id=row.id, project_id=project_id, source=row.source, ref_id=row.ref_id, version=row.version,
                text=row.text, embedding=bytes(row.embedding) if row.embedding is not None else None,
                embedding_key=row.embedding_key, embed_text=embed_text, source_document_id=document_id,
                source_heading=heading, source_content=content, source_characters=characters,
            ))
        return snapshots


def _snapshot_row_condition(snapshot: EmbeddingInputSnapshot):
    return and_(
        RetrievalChunk.id == snapshot.id, RetrievalChunk.project_id == snapshot.project_id,
        RetrievalChunk.source == snapshot.source, RetrievalChunk.ref_id == snapshot.ref_id,
        RetrievalChunk.version == snapshot.version, RetrievalChunk.text == snapshot.text,
        RetrievalChunk.embedding.is_not_distinct_from(snapshot.embedding),
        RetrievalChunk.embedding_key.is_not_distinct_from(snapshot.embedding_key),
    )


def _snapshot_source_condition(snapshot: EmbeddingInputSnapshot):
    if snapshot.source == "kb":
        return exists(select(KbChunk.id).join(KbDocument, KbDocument.id == KbChunk.document_id).where(
            KbChunk.id == snapshot.ref_id, KbChunk.document_id == snapshot.source_document_id,
            KbDocument.project_id == snapshot.project_id, KbChunk.heading_path == snapshot.source_heading,
            KbChunk.content == snapshot.source_content,
        ))
    if snapshot.source == "memory":
        return exists(select(StoryMemory.id).where(
            StoryMemory.id == snapshot.ref_id, StoryMemory.project_id == snapshot.project_id,
            StoryMemory.content == snapshot.source_content,
            cast(StoryMemory.characters, Text) == snapshot.source_characters,
        ))
    raise _invalid_retrieval_reference()


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def analysis_index_memory_ref(snapshot: EmbeddingInputSnapshot, analysis_id: int) -> dict[str, object]:
    """不可变收据只保存来源版本和正文摘要，不保存任何正文。"""
    if (type(snapshot) is not EmbeddingInputSnapshot or snapshot.source != "memory"
            or type(analysis_id) is not int or analysis_id <= 0 or not isinstance(snapshot.text, str)):
        raise EmbeddingPolicyError
    return {"ref_id": snapshot.ref_id, "analysis_id": analysis_id, "retrieval_id": snapshot.id,
            "version": snapshot.version, "content_hash": hashlib.sha256(snapshot.text.encode()).hexdigest()}


def validate_analysis_index_refs(value: object, *, analysis_id: int, memory_hash: str) -> dict[int, dict]:
    """严格校验收据 JSON，拒绝重复或无序的 ID；不能只检查整体哈希。"""
    if (type(value) is not list or len(value) > 10000 or type(analysis_id) is not int or analysis_id <= 0
            or type(memory_hash) is not str or re.fullmatch(r"[0-9a-f]{64}", memory_hash) is None):
        raise EmbeddingPolicyError
    by_ref: dict[int, dict] = {}
    retrieval_ids: set[int] = set()
    for item in value:
        if (type(item) is not dict or set(item) != {
                "ref_id", "analysis_id", "retrieval_id", "version", "content_hash",
            } or any(type(item[key]) is not int or not 0 < item[key] <= 2**31 - 1
                     for key in ("ref_id", "analysis_id", "retrieval_id", "version"))
                or item["analysis_id"] != analysis_id or type(item["content_hash"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", item["content_hash"]) is None
                or item["ref_id"] in by_ref or item["retrieval_id"] in retrieval_ids):
            raise EmbeddingPolicyError
        by_ref[item["ref_id"]] = item
        retrieval_ids.add(item["retrieval_id"])
    if list(by_ref) != sorted(by_ref) or _canonical_digest(value) != memory_hash:
        raise EmbeddingPolicyError
    return by_ref


async def require_analysis_index_receipt(
    db: AsyncSession, execution, selection, snapshots: Sequence[EmbeddingInputSnapshot] = (),
) -> AnalysisIndexReceipt:
    """按原子任务 UUID 证明 0051 收据及不可变来源；调用前须已锁本作品和任务。"""
    from app.services import project_ai_config

    original = execution.input
    receipt_key, refs, analysis_id = (original.get("_analysis_index_receipt"), original.get("_memory_ref_ids"),
                                      original.get("_memory_analysis_id"))
    if (type(receipt_key) is not str or type(refs) is not list or type(analysis_id) is not int):
        raise EmbeddingPolicyError
    row = await db.scalar(select(AnalysisIndexReceipt).where(
        AnalysisIndexReceipt.receipt_key == receipt_key, AnalysisIndexReceipt.child_task_key == execution.task_key,
        AnalysisIndexReceipt.project_id == execution.project_id, AnalysisIndexReceipt.actor_id == execution.actor_id,
        AnalysisIndexReceipt.actor_token_version == execution.actor_token_version,
        AnalysisIndexReceipt.analysis_id == analysis_id, AnalysisIndexReceipt.outcome == "queued",
    ).with_for_update(read=True).execution_options(populate_existing=True))
    if row is None or (row.grant_invitation_id, row.grant_revision) != (
        execution.grant_identity if execution.grant_identity is not None else (None, None)
    ):
        raise EmbeddingPolicyError
    by_ref = validate_analysis_index_refs(row.memory_refs, analysis_id=analysis_id, memory_hash=row.memory_hash)
    if (_canonical_digest(project_ai_config.serialize_shared_embedding(selection)) != row.selection_hash
            or not by_ref):
        raise EmbeddingPolicyError
    if refs != sorted(by_ref):
        raise EmbeddingPolicyError
    if row.source_kind == "batch":
        review = await db.scalar(select(SceneReviewAnalysisReceipt.run_key).where(
            SceneReviewAnalysisReceipt.run_key == row.source_key,
            SceneReviewAnalysisReceipt.task_key == row.source_task_key,
            SceneReviewAnalysisReceipt.chapter_id == row.chapter_id,
            SceneReviewAnalysisReceipt.content_hash == row.content_hash,
            SceneReviewAnalysisReceipt.analysis_id == row.analysis_id,
            SceneReviewAnalysisReceipt.status == "completed",
        ).with_for_update(read=True))
        if review is None:
            raise EmbeddingPolicyError
    elif row.source_kind != "single":
        raise EmbeddingPolicyError
    # Analysis 及其父任务证明的是入队时的来源身份，而非事后按整数 ID 认领；
    # 锁住 Analysis 后普通读取父任务，避免与删除父任务时 FK 置空的锁序倒置。
    from app.services import analysis_service

    analysis = await db.scalar(select(ChapterAnalysis).join(Chapter, Chapter.id == ChapterAnalysis.chapter_id).where(
        ChapterAnalysis.id == row.analysis_id, ChapterAnalysis.project_id == row.project_id,
        ChapterAnalysis.chapter_id == row.chapter_id, ChapterAnalysis.content_hash == row.content_hash,
        Chapter.project_id == row.project_id,
    ).with_for_update(read=True).execution_options(populate_existing=True))
    if (analysis is None or analysis.task_id is None
            or analysis_service._analysis_digest(analysis) != row.analysis_hash):
        raise EmbeddingPolicyError
    parent = await db.scalar(select(Task).where(Task.statistics_key == row.parent_task_key)
                             .execution_options(populate_existing=True))
    if (parent is None or parent.user_id != row.actor_id or parent.project_id != row.project_id
            or not isinstance(parent.queued_at, datetime) or not isinstance(row.parent_queued_at, datetime)
            or _utc_datetime(parent.queued_at) != _utc_datetime(row.parent_queued_at)
            or parent.status not in {"running", "paused", "succeeded", "failed", "cancelled", "interrupted"}
            or (parent.status == "running" and parent.execution_key != row.parent_execution_key)
            or (parent.execution_key is not None and parent.execution_key != row.parent_execution_key)
            or parent.admin_cancel_operation_id is not None or parent.admin_cancel_requested_at is not None
            or analysis_service.analysis_input_hash(parent.input) != row.input_hash):
        raise EmbeddingPolicyError
    if row.source_kind == "single":
        if (parent.kind != "chapter.analyze" or parent.chapter_id != row.chapter_id
                or parent.id != analysis.task_id or row.source_task_key != row.parent_task_key):
            raise EmbeddingPolicyError
    else:
        # 原完成事务已验证 checkpoint 中的完整 ref；任务后续可能推进并清掉当前 ref。
        # 这里重新追溯真实 retry 外键链，禁止用相同整数 ID 或不同作品的任务替代祖先。
        visited: set[int] = set()
        entry = parent
        origin_found = False
        for _ in range(256):
            if (entry is None or entry.id in visited or entry.user_id != row.actor_id
                    or entry.project_id != row.project_id or entry.kind != "chapter.batch"):
                raise EmbeddingPolicyError
            if entry.id != parent.id and (
                entry.status not in {"failed", "cancelled", "interrupted"} or entry.execution_key is not None
                or entry.admin_cancel_operation_id is not None or entry.admin_cancel_requested_at is not None
            ):
                raise EmbeddingPolicyError
            visited.add(entry.id)
            if entry.statistics_key == row.source_task_key:
                origin_found = True
                break
            entry = await db.get(Task, entry.retry_of_id, populate_existing=True) if entry.retry_of_id else None
        if not origin_found or analysis.task_id not in visited:
            raise EmbeddingPolicyError
    for snapshot in snapshots:
        if by_ref.get(snapshot.ref_id) != analysis_index_memory_ref(snapshot, analysis_id):
            raise EmbeddingPolicyError
    return row


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_index_snapshot_identity(execution, selection, snapshots: Sequence[EmbeddingInputSnapshot]) -> None:
    """只接受领取者冻结的内部任务和原共享选择，不能以当前数据库值补签。"""
    from app.services import project_ai_config
    from app.services.collaboration_task_access import GRANT_KEY
    from app.services.task_engine.execution_identity import TaskExecutionIdentity

    if (type(execution) is not TaskExecutionIdentity or type(selection) is not project_ai_config.ResolvedSharedEmbedding
            or execution.kind != "kb.index" or type(execution.project_id) is not int
            or type(execution.actor_token_version) is not int or execution.actor_token_version < 0
            or execution.actor_id != selection.actor_id or execution.project_id != selection.project_id
            or execution.grant_identity != selection.grant_identity or selection.mode != "vector"
            or selection.model is None or type(snapshots) not in (tuple, list)
            or not 0 < len(snapshots) <= EMBED_BATCH
            or any(type(item) is not EmbeddingInputSnapshot or item.project_id != execution.project_id
                   or item.embed_text is None for item in snapshots)
            or len({item.id for item in snapshots}) != len(snapshots)):
        raise EmbeddingPolicyError
    original = execution.input
    request = original.get("request") if isinstance(original, dict) else None
    if (type(request) is not dict or type(request.get("project_id")) is not int
            or request["project_id"] != execution.project_id or request.get("mode") not in (
                "document", "memory", "rebuild",
            ) or original.get("_task_actor_token_version") != execution.actor_token_version):
        raise EmbeddingPolicyError
    mode = request["mode"]
    bound = "_shared_embedding" in original
    analysis_keys = ("_memory_ref_ids", "_memory_analysis_id", "_analysis_index_receipt")
    if mode != "memory" and "_manual_memory_ref_ids" in original:
        raise EmbeddingPolicyError
    if mode == "memory" and not bound:
        # 作者旧记忆任务（_enqueue_memory_locked）：不带任何分析绑定键，只补本作品记忆的空向量。
        if (any(key in original for key in (*analysis_keys, "_manual_memory_ref_ids"))
                or any(item.source != "memory" for item in snapshots)):
            raise EmbeddingPolicyError
    elif mode == "memory" and "_manual_memory_ref_ids" in original:
        # 授权编辑者人工记忆（E6-c，裁决 4）：只能是受理时绑定的本次条目，且必须带原邀请轮次。
        from app.services.kb_index_service import manual_memory_refs

        refs = manual_memory_refs(original)
        if (refs is None or GRANT_KEY not in original or execution.grant_identity is None
                or any(key in original for key in analysis_keys)
                or any(item.source != "memory" or item.ref_id not in refs for item in snapshots)):
            raise EmbeddingPolicyError
    elif mode == "memory":
        refs = original.get("_memory_ref_ids")
        if (type(refs) is not list or not refs or len(refs) > 10000
                or any(type(value) is not int or not 0 < value <= 2**31 - 1 for value in refs)
                or len(refs) != len(set(refs))
                or type(original.get("_memory_analysis_id")) is not int
                or not 0 < original["_memory_analysis_id"] <= 2**31 - 1
                or type(original.get("_analysis_index_receipt")) is not str
                or any(item.source != "memory" or item.ref_id not in refs for item in snapshots)):
            raise EmbeddingPolicyError
    if mode == "document" and (
        type(request.get("document_id")) is not int or request["document_id"] <= 0
        or any(item.source != "kb" or item.source_document_id != request["document_id"] for item in snapshots)
    ):
        raise EmbeddingPolicyError
    expected = project_ai_config.deserialize_shared_embedding(
        original.get("_shared_embedding", project_ai_config.SHARED_EMBEDDING_ABSENT),
        actor_id=execution.actor_id, project_id=execution.project_id, project_owner_id=selection.project_owner_id,
        task_grant=original.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
        allow_legacy_owner=True,
    )
    if expected is None:
        # 作者旧任务不在 Task 中冻结选择：只接受作者本人默认向量的运行时绑定（每次发送/写入仍重验配置）。
        if not legacy_owner_index_selection(selection):
            raise EmbeddingPolicyError
    elif expected != selection:
        raise EmbeddingPolicyError


def legacy_owner_index_selection(selection: object) -> bool:
    """作者 kb.index 任务（文档/重建/旧记忆）在构建期按作者当前默认向量得到的绑定形状。

    仅表示形状合法；配置仍是作者默认、指纹与密钥有效等须在发送/写入屏障内由 revalidate 重验。
    """
    from app.services import project_ai_config

    return bool(
        type(selection) is project_ai_config.ResolvedSharedEmbedding
        and selection.actor_id == selection.project_owner_id and selection.grant_identity is None
        and selection.mode == "vector" and selection.reason is None
        and type(selection.model) is project_ai_config.ResolvedProjectModel
        and selection.model.source == "actor" and selection.model.model_type == "embedding"
        and selection.model.config_owner_id == selection.actor_id and selection.model.grant_identity is None
        and selection.model.actor_id == selection.actor_id and selection.model.project_id == selection.project_id
        and selection.model.project_owner_id == selection.project_owner_id
    )


async def require_sending_embedding_snapshots(
    db: AsyncSession, snapshots: tuple[EmbeddingInputSnapshot, ...], *, actor, execution, selection,
    cipher: SecretCipher, allow_private: bool, preflight: bool,
) -> None:
    """实际发送/预检租约中锁住原来源并重验本批内容；不接受 caller 声称已持锁。"""
    from app.services import ai_dispatch, project_access, project_lifecycle
    from app.services import authorization_service as authz
    from app.services.task_engine.execution_identity import matches_running

    try:
        if (type(snapshots) is not tuple or type(actor) is not authz.AuthenticatedActor
                or type(preflight) is not bool or type(allow_private) is not bool
                or db.get_nested_transaction() is not None or db.new or db.dirty or db.deleted):
            raise EmbeddingPolicyError
        _require_index_snapshot_identity(execution, selection, snapshots)
        if actor.id != execution.actor_id or actor.token_version != execution.actor_token_version:
            raise EmbeddingPolicyError
        require_lease = ai_dispatch.require_preflight_lease if preflight else ai_dispatch.require_actual_send_lease
        await require_lease(db, actor.id, execution.project_id)
        with db.no_autoflush:
            await authz.require_authenticated_for_send(db, actor)
            project_statement = select(Project.id).where(
                Project.id == execution.project_id, *project_lifecycle.active_conditions(),
            )
            if db.get_bind().dialect.name == "postgresql":
                project_statement = project_statement.with_for_update(read=True)
            if await db.scalar(project_statement) is None:
                raise EmbeddingPolicyError
            project = await project_access.require_project(
                db, actor.id, execution.project_id, "ai.use", grant_identity=execution.grant_identity,
            )
            if project.user_id != selection.project_owner_id:
                raise EmbeddingPolicyError
            task = await db.scalar(select(Task).where(
                Task.id == execution.task_id, Task.statistics_key == execution.task_key,
            ).with_for_update(read=True).execution_options(populate_existing=True))
            if not matches_running(execution, task):
                raise EmbeddingPolicyError
            await _lock_embedding_snapshot_sources(
                db, snapshots, execution=execution, selection=selection, cipher=cipher,
                allow_private=allow_private, write=False,
            )
        await require_lease(db, actor.id, execution.project_id)
        return
    except Exception:
        ai_dispatch._invalidate_send_lease(db)
        await db.rollback()
    except BaseException:
        ai_dispatch._invalidate_send_lease(db)
        await db.rollback()
        raise
    raise EmbeddingPolicyError from None


async def _lock_embedding_snapshot_sources(
    db: AsyncSession, snapshots: tuple[EmbeddingInputSnapshot, ...], *, execution, selection,
    cipher: SecretCipher, allow_private: bool, write: bool,
) -> None:
    """调用前持原 Project/Task；在同一事务锁文档、配置、来源和索引并比对快照。"""
    from app.services import ai_config_service, project_ai_config

    project_id = selection.project_id
    original = execution.input
    # 只有分析派生的绑定记忆（带冻结选择）有不可变收据；作者旧记忆任务不带选择，也不得带分析绑定键。
    bound_memory = (original.get("request", {}).get("mode") == "memory" and "_shared_embedding" in original
                    and "_manual_memory_ref_ids" not in original)
    if bound_memory:
        await require_analysis_index_receipt(db, execution, selection, snapshots)
    documents = sorted({s.source_document_id for s in snapshots if s.source == "kb"})
    if any(type(value) is not int or value <= 0 for value in documents):
        raise EmbeddingPolicyError
    if documents:
        locked_docs = set(await db.scalars(select(KbDocument.id).where(
            KbDocument.id.in_(documents), KbDocument.project_id == project_id,
        ).order_by(KbDocument.id).with_for_update(read=not write)))
        if locked_docs != set(documents):
            raise EmbeddingPolicyError
    for owner_id in sorted({selection.actor_id, selection.project_owner_id}):
        await ai_config_service.lock_config_owner(db, owner_id, write=False)
    await project_ai_config.revalidate_shared_embedding(
        db, selection, cipher=cipher, allow_private=allow_private, for_update=write, lock_config=True,
    )
    kb_ids = sorted({s.ref_id for s in snapshots if s.source == "kb"})
    memory_ids = sorted({s.ref_id for s in snapshots if s.source == "memory"})
    if kb_ids:
        locked = set(await db.scalars(select(KbChunk.id).where(
            KbChunk.id.in_(kb_ids), KbChunk.document_id.in_(documents),
        ).order_by(KbChunk.id).with_for_update(read=not write)))
        if locked != set(kb_ids):
            raise EmbeddingPolicyError
    if memory_ids:
        memories = (await db.execute(select(StoryMemory.id, StoryMemory.chapter_id, StoryMemory.analysis_id).where(
            StoryMemory.id.in_(memory_ids), StoryMemory.project_id == project_id,
        ).order_by(StoryMemory.id).with_for_update(read=not write))).all()
        if {row.id for row in memories} != set(memory_ids):
            raise EmbeddingPolicyError
        if bound_memory and any(row.analysis_id != original.get("_memory_analysis_id") for row in memories):
            raise EmbeddingPolicyError
        chapter_ids = sorted({row.chapter_id for row in memories if row.chapter_id is not None})
        if chapter_ids:
            locked = set(await db.scalars(select(Chapter.id).where(
                Chapter.id.in_(chapter_ids), Chapter.project_id == project_id,
            ).order_by(Chapter.id).with_for_update(read=not write)))
            if locked != set(chapter_ids):
                raise EmbeddingPolicyError
    ids = [s.id for s in snapshots]
    locked = set(await db.scalars(select(RetrievalChunk.id).where(
        RetrievalChunk.id.in_(ids), RetrievalChunk.project_id == project_id,
    ).order_by(RetrievalChunk.id).with_for_update(read=not write)))
    if locked != set(ids):
        raise EmbeddingPolicyError
    for source in {s.source for s in snapshots}:
        await validate_project_source_refs(db, project_id, source, [s.ref_id for s in snapshots if s.source == source])
    if tuple(await snapshot_embedding_inputs(db, project_id, ids)) != snapshots:
        raise EmbeddingPolicyError


async def require_writing_embedding_snapshots(
    db: AsyncSession, snapshots: tuple[EmbeddingInputSnapshot, ...], *, actor, execution, selection,
    cipher: SecretCipher, allow_private: bool,
) -> None:
    """同一根事务重取原写屏障和来源锁；只能由原任务写入本批次。"""
    from app.services import authorization_service as authz
    from app.services import project_access
    from app.services.task_engine.execution_identity import require_write_execution

    try:
        if (type(snapshots) is not tuple or type(actor) is not authz.AuthenticatedActor
                or type(allow_private) is not bool or db.get_nested_transaction() is not None
                or db.new or db.dirty or db.deleted):
            raise EmbeddingPolicyError
        _require_index_snapshot_identity(execution, selection, snapshots)
        if actor.id != execution.actor_id or actor.token_version != execution.actor_token_version:
            raise EmbeddingPolicyError
        await authz.require_authenticated(db, actor, write=True, targets=(selection.project_owner_id,))
        await require_write_execution(db, execution)
        with db.no_autoflush:
            project = await project_access.require_project(
                db, actor.id, execution.project_id, "ai.use", grant_identity=execution.grant_identity,
            )
            if project.user_id != selection.project_owner_id:
                raise EmbeddingPolicyError
            await _lock_embedding_snapshot_sources(
                db, snapshots, execution=execution, selection=selection, cipher=cipher,
                allow_private=allow_private, write=True,
            )
    except Exception:
        await db.rollback()
    except BaseException:
        await db.rollback()
        raise
    else:
        return
    raise EmbeddingPolicyError from None


async def store_owner_legacy_embedding_snapshots(
    db: AsyncSession, snapshots: Sequence[EmbeddingInputSnapshot], vectors: Sequence[np.ndarray],
    config: BoundSharedEmbeddingConfig, *, execution,
) -> bool:
    """分析派生绑定记忆只补空向量（作者，或 E6-c 起受理时冻结作者向量选择的授权编辑者）。"""
    from app.services.authorization_service import AuthenticatedActor

    try:
        if type(config) is not BoundSharedEmbeddingConfig:
            raise EmbeddingPolicyError
        _require_index_snapshot_identity(execution, config.selection, snapshots)
        selection = config.selection
        original = execution.input
        # 仅供分析派生的收据绑定记忆：文档/重建/人工记忆条目任务必须走 store_index_legacy_embedding_snapshots。
        if (original.get("request", {}).get("mode") != "memory" or "_memory_analysis_id" not in original
                or "_analysis_index_receipt" not in original or "_manual_memory_ref_ids" in original
                or selection.grant_identity != execution.grant_identity
                or (selection.grant_identity is None) != (selection.actor_id == selection.project_owner_id)
                or selection.model is None or selection.model.config_owner_id != selection.project_owner_id):
            raise EmbeddingPolicyError
        await require_writing_embedding_snapshots(
            db, tuple(snapshots), actor=AuthenticatedActor(execution.actor_id, execution.actor_token_version),
            execution=execution, selection=selection, cipher=config.cipher, allow_private=config.allow_private,
        )
        if (execution.input["request"]["mode"] != "rebuild"
                and any(item.embedding is not None or item.embedding_key is not None for item in snapshots)):
            return False
        return await _store_embedding_snapshots(db, snapshots, vectors, config)
    except BaseException:
        await db.rollback()
        raise


async def store_index_legacy_embedding_snapshots(
    db: AsyncSession, snapshots: Sequence[EmbeddingInputSnapshot], vectors: Sequence[np.ndarray],
    config: BoundSharedEmbeddingConfig, *, execution,
) -> bool:
    """kb.index 文档/重建/作者旧记忆任务：写入作者配置的旧键空间 provider:model:dim（E6 裁决 1）。

    作者本人或受理时冻结作者向量选择的授权编辑者均可；同一根写事务重验原认证、执行、授权轮次、
    作者模型授权与来源快照，再按快照批量 CAS。记忆模式只补空向量；重建（全书）与文档模式（仅本文档分块，
    裁决 12）可 CAS 替换其他空间的旧向量。
    分析派生的绑定记忆不走此入口（仍用 store_owner_legacy_embedding_snapshots 与收据）。
    """
    from app.services.authorization_service import AuthenticatedActor

    try:
        if type(config) is not BoundSharedEmbeddingConfig:
            raise EmbeddingPolicyError
        _require_index_snapshot_identity(execution, config.selection, snapshots)
        selection = config.selection
        request = execution.input["request"]
        if (request["mode"] == "memory" and "_shared_embedding" in execution.input
                and "_manual_memory_ref_ids" not in execution.input) or (
            selection.model is None or selection.model.config_owner_id != selection.project_owner_id
            or (selection.grant_identity is None) != (selection.actor_id == selection.project_owner_id)
        ):
            raise EmbeddingPolicyError
        await require_writing_embedding_snapshots(
            db, tuple(snapshots), actor=AuthenticatedActor(execution.actor_id, execution.actor_token_version),
            execution=execution, selection=selection, cipher=config.cipher, allow_private=config.allow_private,
        )
        # 重建与文档模式（裁决 12，快照身份已限定为本文档分块）可按快照 CAS 替换旧模型向量；记忆模式只补空向量。
        if request["mode"] not in ("rebuild", "document") and any(
            item.embedding is not None or item.embedding_key is not None for item in snapshots
        ):
            return False
        return await _store_embedding_snapshots(db, snapshots, vectors, config)
    except BaseException:
        await db.rollback()
        raise


async def _store_embedding_snapshots(
    db: AsyncSession, snapshots: Sequence[EmbeddingInputSnapshot], vectors: Sequence[np.ndarray],
    config: BoundSharedEmbeddingConfig,
) -> bool:
    """同事务批量 CAS 写入作者旧键空间 provider:model:dim；失效时不写任何向量，不提交或替换原配置。

    调用方应在独立短写事务先确认任务 UUID／执行代次；本函数重验项目与共享配置，
    项目锁、来源行检查和条件更新需与该任务检查保持在同一事务。
    """
    if len(snapshots) != len(vectors) or len(snapshots) > EMBED_BATCH:
        raise ValueError("向量批次无效")
    if not snapshots:
        return True
    from app.services.project_ai_config import ResolvedSharedEmbedding

    if (type(config) is not BoundSharedEmbeddingConfig
            or type(config.selection) is not ResolvedSharedEmbedding
            or any(type(item) is not EmbeddingInputSnapshot for item in snapshots)
            or len({item.id for item in snapshots}) != len(snapshots)
            or any(item.project_id != config.selection.project_id or item.embed_text is None for item in snapshots)):
        raise EmbeddingPolicyError
    if any(item.version >= 2**31 - 1 for item in snapshots):
        return False
    normalized = [normalize_vector(vector) for vector in vectors]
    if (any(vector is None for vector in normalized)
            or len({int(vector.size) for vector in normalized if vector is not None}) != 1):
        raise ValueError("向量批次无效")

    with db.no_autoflush:
        try:
            current_config = await load_bound_shared_embedding_config(
                db, config.selection, cipher=config.cipher, allow_private=config.allow_private, for_update=True,
            )
        except VisibilityPolicyError:
            raise
        except Exception:
            raise EmbeddingPolicyError from None
        if _bound_config_identity(current_config) != _bound_config_identity(config):
            raise EmbeddingPolicyError
        ids = [item.id for item in snapshots]
        try:
            for source in {item.source for item in snapshots}:
                await validate_project_source_refs(db, config.selection.project_id, source,
                                                   [item.ref_id for item in snapshots if item.source == source])
        except ConflictError:
            return False
        locked = set(await db.scalars(select(RetrievalChunk.id).where(
            RetrievalChunk.id.in_(ids), RetrievalChunk.project_id == config.selection.project_id,
        ).order_by(RetrievalChunk.id).with_for_update()))
        if locked != set(ids):
            return False
        try:
            current = await snapshot_embedding_inputs(db, config.selection.project_id, ids)
        except ConflictError:
            return False
        if list(snapshots) != current:
            return False
        dim = int(normalized[0].size)  # 非空批次且上面已经排除空向量
        key = make_embedding_key(f"{current_config.provider}:{current_config.model}", dim)
        encoded = {item.id: encode_vector(vector) for item, vector in zip(snapshots, normalized, strict=True)}
        statement = update(RetrievalChunk).where(or_(*(
            and_(_snapshot_row_condition(item), _snapshot_source_condition(item)) for item in snapshots
        ))).values(
            embedding=case(encoded, value=RetrievalChunk.id), embedding_key=key,
            version=RetrievalChunk.version + 1, updated_at=RetrievalChunk.updated_at,
        ).execution_options(synchronize_session=False)

        # 使用连接级保存点避免 begin_nested() 自动 flush 无关 ORM 草稿；所有鉴权和父行屏障已取得。
        savepoint = await (await db.connection()).begin_nested()
        try:
            result = await db.execute(statement)
            if result.rowcount != len(snapshots):
                await savepoint.rollback()
                return False
            await savepoint.commit()
        except BaseException:
            if savepoint.is_active:
                await savepoint.rollback()
            raise
        return True


async def delete_orphan_snapshot(db: AsyncSession, snapshot: EmbeddingInputSnapshot) -> bool:
    """仅删除仍孤立、身份与版本均不变的检索行；调用方必须先持有项目写屏障。"""
    if type(snapshot) is not EmbeddingInputSnapshot or snapshot.embed_text is not None:
        raise _invalid_retrieval_reference()
    if snapshot.source == "kb":
        source_exists = exists(select(KbChunk.id).where(KbChunk.id == snapshot.ref_id))
    elif snapshot.source == "memory":
        source_exists = exists(select(StoryMemory.id).where(StoryMemory.id == snapshot.ref_id))
    else:
        raise _invalid_retrieval_reference()
    with db.no_autoflush:
        result = await db.execute(delete(RetrievalChunk).where(
            _snapshot_row_condition(snapshot), ~source_exists,
        ).execution_options(synchronize_session=False))
    return result.rowcount == 1


# ---------------- 写入 ----------------


@dataclass
class ChunkInput:
    ref_id: int
    text: str
    chapter_no: int | None = None
    enabled: bool = True
    pinned: bool = False


def _require_clean_source_identity(db: AsyncSession, model, row_id: int, columns: tuple[str, ...]) -> None:
    instance = db.identity_map.get(db.sync_session.identity_key(model, row_id))
    if instance is not None and any(inspect(instance).attrs[name].history.has_changes() for name in columns):
        raise _invalid_retrieval_reference()


async def validate_project_source_refs(
    db: AsyncSession, project_id: int, source: Source, ref_ids: Iterable[int],
) -> None:
    """批量校验业务实体和任何既存检索行的作品归属；缺失或跨书均匿名拒绝。"""
    ids = list(ref_ids)
    if (type(project_id) is not int or not 0 < project_id <= 2**31 - 1 or source not in ("kb", "memory")
            or any(type(value) is not int or not 0 < value <= 2**31 - 1 for value in ids)):
        raise _invalid_retrieval_reference()
    ids = sorted(set(ids))
    with db.no_autoflush:
        for batch in batched(ids, 400):
            if source == "kb":
                rows = (await db.execute(select(KbChunk.id, KbChunk.document_id, KbDocument.project_id).outerjoin(
                    KbDocument, KbDocument.id == KbChunk.document_id,
                ).where(KbChunk.id.in_(batch)))).all()
            else:
                rows = (await db.execute(select(
                    StoryMemory.id, StoryMemory.project_id, StoryMemory.chapter_id,
                    Chapter.project_id.label("chapter_project_id"),
                ).outerjoin(Chapter, Chapter.id == StoryMemory.chapter_id)
                    .where(StoryMemory.id.in_(batch)))).all()
            if {row.id for row in rows} != set(batch) or any(row.project_id != project_id for row in rows):
                raise _invalid_retrieval_reference()
            if source == "memory" and any(
                row.chapter_id is not None and row.chapter_project_id != project_id for row in rows
            ):
                raise _invalid_retrieval_reference()
            for row in rows:
                if source == "kb":
                    _require_clean_source_identity(db, KbChunk, row.id, ("id", "document_id"))
                    _require_clean_source_identity(db, KbDocument, row.document_id, ("id", "project_id"))
                else:
                    _require_clean_source_identity(db, StoryMemory, row.id, ("id", "project_id", "chapter_id"))
                    if row.chapter_id is not None:
                        _require_clean_source_identity(db, Chapter, row.chapter_id, ("id", "project_id"))
            indexed = (await db.execute(select(RetrievalChunk.id, RetrievalChunk.project_id).where(
                RetrievalChunk.source == source, RetrievalChunk.ref_id.in_(batch),
            ))).all()
            if any(row.project_id != project_id for row in indexed):
                raise _invalid_retrieval_reference()
            for row in indexed:
                _require_clean_source_identity(
                    db, RetrievalChunk, row.id, ("id", "project_id", "source", "ref_id", "version"),
                )


async def set_project_flags(
    db: AsyncSession, project_id: int, source: Source, ref_ids: Iterable[int], *,
    enabled: bool | None = None, pinned: bool | None = None, chapter_no: int | None = None,
) -> None:
    """在调用方已持项目锁的事务中修改标志；不依赖单独 ref_id 证明归属。"""
    ids = list(ref_ids)
    await validate_project_source_refs(db, project_id, source, ids)
    values = {name: value for name, value in (
        ("enabled", enabled), ("pinned", pinned), ("chapter_no", chapter_no),
    ) if value is not None}
    if not ids or not values:
        return
    with db.no_autoflush:
        for batch in batched(ids, 400):
            exhausted = await db.scalar(select(RetrievalChunk.id).where(
                RetrievalChunk.project_id == project_id, RetrievalChunk.source == source,
                RetrievalChunk.ref_id.in_(batch), RetrievalChunk.version >= 2**31 - 1,
            ).limit(1))
            if exhausted is not None:
                raise _invalid_retrieval_reference()
        for batch in batched(ids, 400):
            await db.execute(update(RetrievalChunk).where(
                RetrievalChunk.project_id == project_id, RetrievalChunk.source == source,
                RetrievalChunk.ref_id.in_(batch),
            ).values(**values, version=RetrievalChunk.version + 1).execution_options(synchronize_session=False))


async def delete_project_refs(db: AsyncSession, project_id: int, source: Source, ref_ids: Iterable[int]) -> None:
    """删除前在同一项目事务中验证业务来源；不存在的检索行视为幂等。"""
    ids = list(ref_ids)
    await validate_project_source_refs(db, project_id, source, ids)
    with db.no_autoflush:
        for batch in batched(ids, 400):
            await db.execute(delete(RetrievalChunk).where(
                RetrievalChunk.project_id == project_id, RetrievalChunk.source == source,
                RetrievalChunk.ref_id.in_(batch),
            ).execution_options(synchronize_session=False))


async def upsert_project_sources(
    db: AsyncSession, project_id: int, source: Source, items: Iterable[ChunkInput],
) -> None:
    """持项目写锁时，先批量验证已落库业务来源，再同步检索单元；不提交。"""
    items = list(items)
    if len({item.ref_id for item in items}) != len(items):
        raise _invalid_retrieval_reference()
    await validate_project_source_refs(db, project_id, source, [item.ref_id for item in items])
    await upsert_many(db, project_id, source, items)


async def require_unindexed_sources(db: AsyncSession, source: Source, ref_ids: Iterable[int]) -> None:
    """新建来源不能继承历史孤儿索引，即使该孤儿标记属于同一作品。"""
    ids = list(ref_ids)
    if source not in ("kb", "memory") or any(type(value) is not int or not 0 < value <= 2**31 - 1 for value in ids):
        raise _invalid_retrieval_reference()
    with db.no_autoflush:
        for batch in batched(ids, 400):
            exists_row = await db.scalar(select(RetrievalChunk.id).where(
                RetrievalChunk.source == source, RetrievalChunk.ref_id.in_(batch),
            ).limit(1))
            if exists_row is not None:
                raise _invalid_retrieval_reference()


async def upsert_many(db: AsyncSession, project_id: int, source: Source, items: Iterable[ChunkInput]) -> None:
    """按 (source, ref_id) 新建或更新检索单元；正文变化时重算关键词并清空向量。不 commit。"""
    items = list(items)
    if not items:
        return
    with db.no_autoflush:
        stored = []
        rows = []
        for batch in batched([i.ref_id for i in items], 400):
            stored.extend((await db.execute(select(RetrievalChunk.ref_id, RetrievalChunk.project_id).where(
                RetrievalChunk.source == source, RetrievalChunk.ref_id.in_(batch),
            ))).all())
            rows.extend(await db.scalars(select(RetrievalChunk).where(
                RetrievalChunk.source == source, RetrievalChunk.ref_id.in_(batch),
            )))
        if any(row.project_id != project_id for row in stored):
            raise _invalid_retrieval_reference()
        # source/ref_id 全局唯一；异常旧数据不能因为冲突而覆盖另一作品的检索行。
        if any(row.project_id != project_id for row in rows):
            raise _invalid_retrieval_reference()
        existing = {row.ref_id: row for row in rows}
    for item in items:
        row = existing.get(item.ref_id)
        if row is None:
            tokens = term_freq(item.text)
            db.add(
                RetrievalChunk(
                    project_id=project_id,
                    source=source,
                    ref_id=item.ref_id,
                    text=item.text,
                    tokens=tokens,
                    length=sum(tokens.values()),
                    chapter_no=item.chapter_no,
                    enabled=item.enabled,
                    pinned=item.pinned,
                    version=1,
                )
            )
            continue
        changed = False
        if row.text != item.text:
            tokens = term_freq(item.text)
            row.text, row.tokens, row.length = item.text, tokens, sum(tokens.values())
            row.embedding, row.embedding_key = None, None
            changed = True
        for attr in ("chapter_no", "enabled", "pinned"):
            if getattr(row, attr) != getattr(item, attr):
                setattr(row, attr, getattr(item, attr))
                changed = True
        if changed:
            row.version = (row.version or 0) + 1
    await db.flush()


async def set_flags(
    db: AsyncSession,
    source: Source,
    ref_ids: Iterable[int],
    *,
    enabled: bool | None = None,
    pinned: bool | None = None,
    chapter_no: int | None = None,
) -> None:
    ids = list(ref_ids)
    values: dict = {}
    if enabled is not None:
        values["enabled"] = enabled
    if pinned is not None:
        values["pinned"] = pinned
    if chapter_no is not None:
        values["chapter_no"] = chapter_no
    if not ids or not values:
        return
    await db.execute(
        update(RetrievalChunk)
        .where(RetrievalChunk.source == source, RetrievalChunk.ref_id.in_(ids))
        .values(**values, version=RetrievalChunk.version + 1)
        .execution_options(synchronize_session=False)
    )


async def delete_refs(db: AsyncSession, source: Source, ref_ids: Iterable[int]) -> None:
    ids = list(ref_ids)
    if ids:
        await db.execute(
            delete(RetrievalChunk)
            .where(RetrievalChunk.source == source, RetrievalChunk.ref_id.in_(ids))
            .execution_options(synchronize_session=False)
        )


async def store_embeddings(db: AsyncSession, rows: Sequence[RetrievalChunk], vectors: Sequence[np.ndarray], key: str):
    for row, vec in zip(rows, vectors, strict=True):
        row.embedding = encode_vector(vec)
        row.embedding_key = key
        row.version = (row.version or 0) + 1
    await db.flush()


def _stale_condition(model_key: str):
    """缺少向量，或向量不属于当前模型（embedding_key 不以「provider:model:」开头）。"""
    return (
        RetrievalChunk.embedding.is_(None)
        | RetrievalChunk.embedding_key.is_(None)
        | ~RetrievalChunk.embedding_key.startswith(f"{model_key}:", autoescape=True)
    )


async def stale_chunks(
    db: AsyncSession,
    project_id: int,
    model_key: str,
    *,
    source: Source | None = None,
    ref_ids: Sequence[int] | None = None,
    limit: int = EMBED_BATCH,
) -> list[RetrievalChunk]:
    """缺少当前向量模型向量的检索单元（按 id），供建索引任务分批处理。"""
    stmt = select(RetrievalChunk).where(RetrievalChunk.project_id == project_id, _stale_condition(model_key))
    if source is not None:
        stmt = stmt.where(RetrievalChunk.source == source)
    if ref_ids is not None:
        stmt = stmt.where(RetrievalChunk.ref_id.in_(list(ref_ids)))
    return list(await db.scalars(stmt.order_by(RetrievalChunk.id).limit(limit)))


async def stale_counts(db: AsyncSession, project_id: int, model_key: str | None) -> dict[str, int]:
    """各 source 缺少当前模型向量的检索单元数；没有向量模型时都为 0。"""
    counts = {"kb": 0, "memory": 0}
    if model_key is None:
        return counts
    stmt = (
        select(RetrievalChunk.source, func.count(RetrievalChunk.id))
        .where(RetrievalChunk.project_id == project_id, _stale_condition(model_key))
        .group_by(RetrievalChunk.source)
    )
    for source, n in (await db.execute(stmt)).all():
        counts[source] = int(n)
    return counts


# ---------------- 缓存 ----------------


@dataclass
class _Index:
    signature: tuple
    ids: np.ndarray
    ref_ids: np.ndarray
    lengths: np.ndarray
    chapter_nos: np.ndarray  # 无章节号为 -1
    enabled: np.ndarray
    pinned: np.ndarray
    keys: np.ndarray  # object：embedding_key 或 None
    postings: dict[str, tuple[np.ndarray, np.ndarray]]
    # embedding_key → (行下标, 矩阵)
    vectors: dict[str, tuple[np.ndarray, np.ndarray]]


_CACHE: dict[tuple[int, str], _Index] = {}
# 缓存的项目 × source 上限（超出时丢弃最早放入的）
MAX_CACHED = 64


def invalidate(project_id: int | None = None) -> None:
    """清空缓存（测试 / 运维用；正常由签名自动失效）。"""
    if project_id is None:
        _CACHE.clear()
        return
    for key in [k for k in _CACHE if k[0] == project_id]:
        _CACHE.pop(key, None)


async def _signature(db: AsyncSession, project_id: int, source: str) -> tuple:
    row = (
        await db.execute(
            select(
                func.count(RetrievalChunk.id),
                func.max(RetrievalChunk.id),
                func.sum(RetrievalChunk.version),
                func.sum(func.coalesce(RetrievalChunk.chapter_no, 0)),
            ).where(RetrievalChunk.project_id == project_id, RetrievalChunk.source == source)
        )
    ).one()
    return tuple(int(v or 0) for v in row)


async def _load_index(db: AsyncSession, project_id: int, source: str) -> _Index:
    signature = await _signature(db, project_id, source)
    cached = _CACHE.get((project_id, source))
    if cached is not None and cached.signature == signature:
        return cached
    rows = (
        await db.execute(
            select(
                RetrievalChunk.id,
                RetrievalChunk.ref_id,
                RetrievalChunk.tokens,
                RetrievalChunk.length,
                RetrievalChunk.chapter_no,
                RetrievalChunk.enabled,
                RetrievalChunk.pinned,
                RetrievalChunk.embedding,
                RetrievalChunk.embedding_key,
            )
            .where(RetrievalChunk.project_id == project_id, RetrievalChunk.source == source)
            .order_by(RetrievalChunk.id)
        )
    ).all()
    n = len(rows)
    term_rows: dict[str, list[tuple[int, int]]] = {}
    vec_rows: dict[str, list[tuple[int, np.ndarray]]] = {}
    keys: list[str | None] = []
    for i, (_id, _ref, tokens, _len, _no, _en, _pin, emb, key) in enumerate(rows):
        for term, tf in (tokens or {}).items():
            term_rows.setdefault(term, []).append((i, int(tf)))
        usable = None
        if emb and key:
            vec = decode_vector(emb)
            if vec.size and key.endswith(f":{vec.size}"):
                usable = key
                vec_rows.setdefault(key, []).append((i, vec))
        keys.append(usable)
    index = _Index(
        signature=signature,
        ids=np.array([r[0] for r in rows], dtype=np.int64),
        ref_ids=np.array([r[1] for r in rows], dtype=np.int64),
        lengths=np.array([float(r[3] or 0) for r in rows], dtype=np.float64),
        chapter_nos=np.array([r[4] if r[4] is not None else -1 for r in rows], dtype=np.int64),
        enabled=np.array([bool(r[5]) for r in rows], dtype=bool),
        pinned=np.array([bool(r[6]) for r in rows], dtype=bool),
        keys=np.array(keys, dtype=object) if n else np.array([], dtype=object),
        postings={
            t: (np.array([p[0] for p in ps], dtype=np.int64), np.array([p[1] for p in ps], dtype=np.float64))
            for t, ps in term_rows.items()
        },
        vectors={
            k: (np.array([p[0] for p in ps], dtype=np.int64), np.vstack([p[1] for p in ps]).astype(np.float32))
            for k, ps in vec_rows.items()
        },
    )
    if len(_CACHE) >= MAX_CACHED and (project_id, source) not in _CACHE:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[(project_id, source)] = index
    return index


# ---------------- 检索 ----------------


@dataclass
class SearchFilter:
    enabled_only: bool = True
    # 只取章节号 < chapter_lt 的（记忆）
    chapter_lt: int | None = None
    exclude_chapter_nos: set[int] = field(default_factory=set)
    exclude_ref_ids: set[int] = field(default_factory=set)
    # 限定在这些 ref_id 之内（None = 不限）
    ref_ids: set[int] | None = None


@dataclass
class Hit:
    ref_id: int
    chunk_id: int
    text: str
    score: float
    vector_score: float | None
    keyword_score: float
    chapter_no: int | None
    pinned: bool


@dataclass
class SearchResult:
    hits: list[Hit] = field(default_factory=list)
    mode: SearchMode = "keyword"
    warnings: list[str] = field(default_factory=list)
    candidates: int = 0


def _mask(index: _Index, flt: SearchFilter) -> np.ndarray:
    mask = np.ones(index.ids.shape[0], dtype=bool)
    if flt.enabled_only:
        mask &= index.enabled
    if flt.chapter_lt is not None:
        mask &= (index.chapter_nos >= 0) & (index.chapter_nos < flt.chapter_lt)
    if flt.exclude_chapter_nos:
        mask &= ~np.isin(index.chapter_nos, np.array(sorted(flt.exclude_chapter_nos), dtype=np.int64))
    if flt.exclude_ref_ids:
        mask &= ~np.isin(index.ref_ids, np.array(sorted(flt.exclude_ref_ids), dtype=np.int64))
    if flt.ref_ids is not None:
        mask &= np.isin(index.ref_ids, np.array(sorted(flt.ref_ids), dtype=np.int64))
    return mask


def bm25_scores(index: _Index, mask: np.ndarray, query_terms: Iterable[str]) -> np.ndarray:
    """候选集合（mask）上的 BM25，除以最高分归一化到 [0, 1]；非候选为 0。"""
    scores = np.zeros(index.ids.shape[0], dtype=np.float64)
    n = int(mask.sum())
    if n == 0:
        return scores
    avgdl = float(index.lengths[mask].mean()) or 1.0
    for term in dict.fromkeys(query_terms):
        posting = index.postings.get(term)
        if posting is None:
            continue
        idx, tf = posting
        keep = mask[idx]
        if not keep.any():
            continue
        idx, tf = idx[keep], tf[keep]
        df = idx.size
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        dl = index.lengths[idx]
        scores[idx] += idf * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl))
    top = scores.max() if scores.size else 0.0
    return scores / top if top > 0 else scores


async def search(
    db: AsyncSession,
    project_id: int,
    source: Source,
    query: str,
    *,
    top_k: int,
    min_score: float,
    embedder: Embedder | None = None,
    filters: SearchFilter | None = None,
) -> SearchResult:
    """混合检索（见模块说明）。不抛提供方错误：查询向量失败时退回关键词并给出 warning。"""
    await _preflight_guarded_embedder(embedder, project_id)
    flt = filters or SearchFilter()
    result = SearchResult()
    index = await _load_index(db, project_id, source)
    mask = _mask(index, flt)
    result.candidates = int(mask.sum())
    if result.candidates == 0 or top_k <= 0:
        await _preflight_guarded_embedder(embedder, project_id)
        return result
    keyword = bm25_scores(index, mask, tokenize(query))

    vector: np.ndarray | None = None
    if embedder is not None and query.strip():
        cand_keys = index.keys[mask]
        if all(isinstance(k, str) and key_matches_model(k, embedder.model_key) for k in cand_keys):
            qvec, warning = await embed_query(embedder, query)
            if warning:
                result.warnings.append(warning)
            key = embedder.key
            group = index.vectors.get(key) if key else None
            if qvec is not None and group is not None and all(k == key for k in cand_keys):
                idx, matrix = group
                cos = np.clip(matrix @ qvec, -1.0, 1.0)
                vector = np.zeros(index.ids.shape[0], dtype=np.float64)
                vector[idx] = (cos.astype(np.float64) + 1.0) / 2.0
            elif qvec is not None:
                result.warnings.append(KEYWORD_ONLY_STALE)
        else:
            result.warnings.append(KEYWORD_ONLY_STALE)

    if vector is not None:
        result.mode = "hybrid"
        final = VECTOR_WEIGHT * vector + KEYWORD_WEIGHT * keyword
        eligible = mask.copy()
    else:
        final = keyword
        eligible = mask & (keyword > 0)
    eligible &= final >= min_score
    order = [i for i in np.argsort(-final, kind="stable") if eligible[i]][:top_k]
    if not order:
        await _preflight_guarded_embedder(embedder, project_id)
        return result
    ids = [int(index.ids[i]) for i in order]
    texts = dict(
        (await db.execute(select(RetrievalChunk.id, RetrievalChunk.text).where(RetrievalChunk.id.in_(ids)))).all()
    )
    for i in order:
        cid = int(index.ids[i])
        if cid not in texts:
            continue
        no = int(index.chapter_nos[i])
        result.hits.append(
            Hit(
                ref_id=int(index.ref_ids[i]),
                chunk_id=cid,
                text=texts[cid],
                score=round(float(final[i]), 4),
                vector_score=round(float(vector[i]), 4) if vector is not None else None,
                keyword_score=round(float(keyword[i]), 4),
                chapter_no=no if no >= 0 else None,
                pinned=bool(index.pinned[i]),
            )
        )
    await _preflight_guarded_embedder(embedder, project_id)
    return result
