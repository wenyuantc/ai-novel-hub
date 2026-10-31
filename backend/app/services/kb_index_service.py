"""建索引任务 kb.index（serial_key = index:{project_id}，同项目串行）。三种模式：

1. document（document_id，resource_key = kbdoc:{id}）：文档还没有分块时从全文分块，
   写入 kb_chunks 与检索单元（同时算好关键词），
   提交后再分批（每批 32 条，每批提交一次）为缺少当前模型向量的分块生成向量。
2. memory（resource_key 为空）：为项目中缺少当前模型向量的记忆检索单元生成向量（分析成功 / 手动记忆变更后排队）。
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
from typing import Any, Literal

import anyio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.providers import AIProviderError, ProviderFactory
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import KbChunk, KbDocument, Project, RetrievalChunk, Task
from app.services import kb_parser, project_lifecycle, retrieval_service
from app.services.project_service import get_owned_project
from app.services.retrieval_service import ChunkInput, Embedder

logger = logging.getLogger(__name__)

KIND = "kb.index"
Mode = Literal["document", "memory", "rebuild"]
MODES = ("document", "memory", "rebuild")
NO_EMBEDDING = "未配置向量模型，仅使用关键词检索"
NO_EMBEDDING_REBUILD = "尚未配置向量模型，请先在「模型配置」中添加向量模型"
_PROGRESS_RE = re.compile(r"(\d+)/(\d+)\s*$")


def serial_key(project_id: int) -> str:
    return f"index:{project_id}"


@dataclass
class IndexJob:
    mode: Mode
    project_id: int
    document_id: int | None
    embedder: Embedder | None
    task_id: int | None = None
    warnings: list[str] = field(default_factory=list)
    # 配置了向量模型但不可用（端点被拦截、密钥无法解密）时的原因；没有配置时为 None
    embedder_error: str | None = None

    def no_embedding_warning(self) -> str:
        return f"向量模型不可用：{self.embedder_error}；仅使用关键词检索" if self.embedder_error else NO_EMBEDDING


# ---------------- 创建 ----------------


async def validate_index_task(db: AsyncSession, *, user_id: int, params: dict[str, Any]) -> dict[str, Any]:
    """返回 TaskSpec 字段（不 commit）。文档模式把文档状态置为 pending。"""
    from app.services import kb_service

    mode = params.get("mode")
    if mode not in MODES:
        raise BadRequestError("建索引模式不正确")
    project_id = int(params["project_id"])
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


async def enqueue_memory(sessionmaker: async_sessionmaker[AsyncSession], project_id: int | None) -> int | None:
    """有向量模型且有缺少向量的记忆时排队一个记忆模式任务（已有排队中的同类任务则不再排队）。返回任务 id。"""
    from app.services import task_service

    if project_id is None:
        return None
    async with sessionmaker() as db:
        project = await db.get(Project, project_id)
        if project is None or project.deleted_at is not None or project.deletion_started_at is not None:
            return None
        try:
            await project_lifecycle.lock_active(db, project_id)
        except (ConflictError, NotFoundError):
            return None
        cfg = await retrieval_service.default_embedding_config(db, project.user_id)
        if cfg is None:
            return None
        if not await retrieval_service.stale_chunks(
            db, project.id, retrieval_service.config_model_key(cfg), source="memory", limit=1
        ):
            return None
        queued = await db.scalar(
            select(Task.id).where(
                Task.project_id == project.id,
                Task.kind == KIND,
                Task.status == "queued",
                Task.resource_key.is_(None),
            )
        )
        if queued is not None:
            return None
        task = Task(
            user_id=project.user_id,
            project_id=project.id,
            kind=KIND,
            title="剧情记忆向量",
            serial_key=serial_key(project.id),
            status="queued",
            input={"request": {"project_id": project.id, "mode": "memory"}},
            stage=task_service.STAGE_LABELS["queued"],
        )
        db.add(task)
        await db.commit()
        return task.id


# ---------------- 组装 / 运行 ----------------


async def build_index_job(
    db: AsyncSession,
    *,
    task: Task,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> IndexJob:
    request = dict((task.input or {}).get("request") or {})
    project_id = int(request["project_id"])
    project = await db.scalar(select(Project).where(Project.id == project_id, *project_lifecycle.active_conditions()))
    if project is None or project.user_id != task.user_id:
        raise NotFoundError("项目不存在")
    mode: Mode = request.get("mode", "document")
    document_id = int(request["document_id"]) if mode == "document" else None
    if document_id is not None:
        doc = await db.scalar(
            select(KbDocument).where(KbDocument.id == document_id, KbDocument.project_id == project_id)
        )
        if doc is None:
            raise NotFoundError("文档不存在")
    embedder, embedder_error = await retrieval_service.resolve_embedder(
        db, task.user_id, cipher=cipher, factory=factory, ai_timeout=ai_timeout, allow_private=allow_private
    )
    if mode == "rebuild" and embedder is None:
        raise BadRequestError(f"向量模型不可用：{embedder_error}" if embedder_error else NO_EMBEDDING_REBUILD)
    return IndexJob(
        mode=mode,
        project_id=project_id,
        document_id=document_id,
        embedder=embedder,
        task_id=task.id,
        embedder_error=embedder_error,
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
    db.add_all(rows)
    await db.flush()
    await retrieval_service.upsert_many(
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


async def _embed_stale(
    sessionmaker: async_sessionmaker[AsyncSession],
    job: IndexJob,
    *,
    source: retrieval_service.Source | None,
    ref_ids: list[int] | None,
    total: int,
) -> AsyncIterator[tuple[str, dict]]:
    """分批为缺少当前模型向量的检索单元生成向量，每批提交一次；产出进度 delta。失败抛 AIProviderError。"""
    embedder = job.embedder
    assert embedder is not None
    done = 0
    # 上限保护：每轮至少处理一条，最多 total / 批大小 + 2 轮
    for _ in range(total // retrieval_service.EMBED_BATCH + 2):
        async with sessionmaker() as db:
            rows = await retrieval_service.stale_chunks(
                db, job.project_id, embedder.model_key, source=source, ref_ids=ref_ids
            )
            if not rows:
                return
            vectors = await embedder.embed([r.text for r in rows])
            assert embedder.key is not None
            await retrieval_service.store_embeddings(db, rows, vectors, embedder.key)
            await db.commit()
        done += len(rows)
        yield "delta", {"text": f"生成向量 {min(done, total)}/{max(total, done)}\n"}


async def _count_stale(
    sessionmaker: async_sessionmaker[AsyncSession], job: IndexJob, source: str | None, ref_ids: list[int] | None
) -> int:
    from sqlalchemy import func

    assert job.embedder is not None
    async with sessionmaker() as db:
        stmt = select(func.count(RetrievalChunk.id)).where(
            RetrievalChunk.project_id == job.project_id,
            retrieval_service._stale_condition(job.embedder.model_key),
        )
        if source is not None:
            stmt = stmt.where(RetrievalChunk.source == source)
        if ref_ids is not None:
            stmt = stmt.where(RetrievalChunk.ref_id.in_(ref_ids))
        return int(await db.scalar(stmt) or 0)


async def _set_doc(sessionmaker, document_id: int, **values) -> None:
    async with sessionmaker() as db:
        await db.execute(update(KbDocument).where(KbDocument.id == document_id).values(**values))
        await db.commit()


async def run_index_job(
    job: IndexJob, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[tuple[str, dict]]:
    try:
        if job.mode == "document":
            async for event in _run_document(job, sessionmaker):
                yield event
        else:
            async for event in _run_vectors(job, sessionmaker):
                yield event
    except Exception:  # noqa: BLE001
        logger.exception("建索引任务失败（项目 %s）", job.project_id)
        if job.document_id is not None:
            await _set_doc(sessionmaker, job.document_id, status="failed", error="建索引失败：服务器内部错误")
        yield "error", {"detail": "建索引失败：服务器内部错误"}


async def _run_document(job: IndexJob, sessionmaker) -> AsyncIterator[tuple[str, dict]]:
    assert job.document_id is not None
    async with sessionmaker() as db:
        doc = await db.get(KbDocument, job.document_id)
        if doc is None:
            yield "error", {"detail": "文档已被删除"}
            return
        doc.status, doc.error = "indexing", ""
        count = await _ensure_chunks(db, doc)
        await db.commit()
        warnings = list(doc.warnings or [])
    yield "delta", {"text": f"已分块：{count} 块\n"}
    status = "ready"
    if count == 0:
        warnings.append("文档没有可分块的正文")
    elif job.embedder is None:
        status = "ready_keyword_only"
        job.warnings.append(job.no_embedding_warning())
    else:
        async with sessionmaker() as db:
            ref_ids = list(await db.scalars(select(KbChunk.id).where(KbChunk.document_id == job.document_id)))
        try:
            total = await _count_stale(sessionmaker, job, "kb", ref_ids)
            async for event in _embed_stale(sessionmaker, job, source="kb", ref_ids=ref_ids, total=total):
                yield event
        except AIProviderError as exc:
            status = "ready_keyword_only"
            job.warnings.append(f"向量生成失败（{exc}），该文档暂时只能关键词检索，可稍后重建索引")
    async with sessionmaker() as db:
        doc = await db.get(KbDocument, job.document_id)
        if doc is None:
            yield "error", {"detail": "文档已被删除"}
            return
        doc.status, doc.error = status, ""
        doc.warnings = list(dict.fromkeys([*warnings, *job.warnings]))[:20]
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
    if job.embedder is None:
        job.warnings.append(job.no_embedding_warning())
    else:
        try:
            total = await _count_stale(sessionmaker, job, source, None)
            async for event in _embed_stale(sessionmaker, job, source=source, ref_ids=None, total=total):
                embedded += 1
                yield event
        except AIProviderError as exc:
            job.warnings.append(f"向量生成失败（{exc}），未完成的部分暂时只能关键词检索")
    if job.mode == "rebuild":
        await _refresh_doc_statuses(sessionmaker, job)
    yield "done", {"mode": job.mode, "project_id": job.project_id, "batches": embedded, "warnings": job.warnings}


async def _refresh_doc_statuses(sessionmaker, job: IndexJob) -> None:
    """重建后：文档的分块都有当前模型向量 → ready，否则 ready_keyword_only（建索引中 / 失败的不动）。"""
    if job.embedder is None:
        return
    async with sessionmaker() as db:
        docs = list(
            await db.scalars(
                select(KbDocument).where(
                    KbDocument.project_id == job.project_id, KbDocument.status.in_(("ready", "ready_keyword_only"))
                )
            )
        )
        for doc in docs:
            ids = list(await db.scalars(select(KbChunk.id).where(KbChunk.document_id == doc.id)))
            stale = ids and await retrieval_service.stale_chunks(
                db, job.project_id, job.embedder.model_key, source="kb", ref_ids=ids, limit=1
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
            await db.execute(
                update(KbDocument)
                .where(KbDocument.id == int(request["document_id"]), KbDocument.status.in_(("pending", "indexing")))
                .values(status="failed", error="建索引未完成（失败、取消或中断），请重试")
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("建索引任务 %s 回退文档状态失败", task.id)
        raise
