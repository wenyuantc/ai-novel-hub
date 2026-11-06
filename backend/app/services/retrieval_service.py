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
import logging
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

import numpy as np
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.providers import AIProviderError, LLMProvider, ProviderFactory
from app.core.errors import BadRequestError
from app.core.security import SecretCipher
from app.models import AIConfig, RetrievalChunk
from app.services.ai_dispatch import VisibilityPolicyError, VisibilityStamp, guard_provider

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
    return embedding_key.startswith(prefix) and embedding_key[len(prefix) :].isdigit()


def config_model_key(cfg: AIConfig) -> str:
    return f"{cfg.provider}:{cfg.model}"


# ---------------- 向量模型 ----------------


@dataclass
class Embedder:
    """当前用户的默认向量模型。embed 分批、截断、归一化；失败抛 AIProviderError。"""

    provider: LLMProvider
    provider_name: str
    model: str
    config_name: str = ""
    dim: int | None = None
    _cache: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def model_key(self) -> str:
        return f"{self.provider_name}:{self.model}"

    @property
    def key(self) -> str | None:
        return make_embedding_key(self.model_key, self.dim) if self.dim else None

    async def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        for start in range(0, len(missing), EMBED_BATCH):
            batch = missing[start : start + EMBED_BATCH]
            raw = await self.provider.embed([t[:EMBED_TEXT_CHARS] for t in batch], self.model)
            if len(raw) != len(batch):
                raise AIProviderError("模型调用失败：返回的向量数量与请求不符")
            for text, values in zip(batch, raw, strict=True):
                vec = normalize_vector(values)
                if vec is None:
                    raise AIProviderError("模型调用失败：返回了空向量")
                if self.dim is None:
                    self.dim = int(vec.size)
                elif vec.size != self.dim:
                    raise AIProviderError(f"模型调用失败：向量维度不一致（{vec.size} ≠ {self.dim}）")
                self._cache[text] = vec
        return [self._cache[t] for t in texts]


def guard_embedder(
    embedder: Embedder | None, stamp: VisibilityStamp, sessionmaker: async_sessionmaker[AsyncSession],
) -> Embedder | None:
    """仅给由设定派生的本次检索绑定发送戳；不改变普通知识库索引。"""
    if embedder is None:
        return None
    return replace(embedder, provider=guard_provider(embedder.provider, stamp, sessionmaker), _cache={})


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
    return Embedder(provider=provider, provider_name=cfg.provider, model=cfg.model, config_name=cfg.name), None


async def get_embedder(
    db: AsyncSession,
    user_id: int,
    *,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> Embedder | None:
    """构建默认向量模型；没有配置或配置不可用时返回 None（调用方退回关键词检索）。"""
    embedder, _ = await resolve_embedder(
        db, user_id, cipher=cipher, factory=factory, ai_timeout=ai_timeout, allow_private=allow_private
    )
    return embedder


@dataclass(frozen=True)
class EmbedEnv:
    """构建向量模型所需的依赖（路由经 deps.EmbedEnvDep 注入）。"""

    cipher: SecretCipher
    factory: ProviderFactory
    ai_timeout: float
    allow_private: bool

    async def embedder(self, db: AsyncSession, user_id: int) -> Embedder | None:
        return await get_embedder(
            db,
            user_id,
            cipher=self.cipher,
            factory=self.factory,
            ai_timeout=self.ai_timeout,
            allow_private=self.allow_private,
        )


async def embed_query(embedder: Embedder | None, text: str) -> tuple[np.ndarray | None, str]:
    """为查询求向量；失败返回 (None, warning)。"""
    if embedder is None or not text.strip():
        return None, ""
    try:
        async with asyncio.timeout(QUERY_EMBED_TIMEOUT_SECONDS):
            vec = (await embedder.embed([text]))[0]
    except TimeoutError:
        return None, f"查询向量生成超时（{QUERY_EMBED_TIMEOUT_SECONDS} 秒），本次只用关键词检索"
    except VisibilityPolicyError:
        raise
    except AIProviderError as exc:
        return None, f"查询向量生成失败（{exc}），本次只用关键词检索"
    return vec, ""


# ---------------- 写入 ----------------


@dataclass
class ChunkInput:
    ref_id: int
    text: str
    chapter_no: int | None = None
    enabled: bool = True
    pinned: bool = False


async def upsert_many(db: AsyncSession, project_id: int, source: Source, items: Iterable[ChunkInput]) -> None:
    """按 (source, ref_id) 新建或更新检索单元；正文变化时重算关键词并清空向量。不 commit。"""
    items = list(items)
    if not items:
        return
    existing = {
        r.ref_id: r
        for r in await db.scalars(
            select(RetrievalChunk).where(
                RetrievalChunk.source == source, RetrievalChunk.ref_id.in_([i.ref_id for i in items])
            )
        )
    }
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
    flt = filters or SearchFilter()
    result = SearchResult()
    index = await _load_index(db, project_id, source)
    mask = _mask(index, flt)
    result.candidates = int(mask.sum())
    if result.candidates == 0 or top_k <= 0:
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
    return result
