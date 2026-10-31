"""知识库：检索设置、文档上传 / 注入开关 / 删除 / 重建、分块查看、检索调试、索引状态与提示词注入块。

- 上传：请求内校验（大小 ≤ 20MB、项目文档数 ≤ 200、同项目 sha256 去重 → 409）并解析为文本（线程中运行，失败 400），
  文档以 pending 落库，同一事务创建 kb.index 文档任务（分块、关键词与向量由后台任务完成，见 kb_index_service）。
- 注入开关：同步更新该文档全部检索单元的 enabled；删除文档：先删检索单元，再删文档（分块级联）。
- 重建该文档：删除分块与检索单元、状态回到 pending，再排队文档任务（任务从文本重新分块）。
- context_block：项目总开关关闭、没有命中或检索出错时返回空串（出错只记日志，不阻断生成）。
"""

import hashlib
import logging
from dataclasses import dataclass

import anyio
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import KbChunk, KbDocument, KbSettings, Project, Task
from app.models.entities import ACTIVE_TASK_STATUSES
from app.schemas.kb import (
    IndexStatus,
    KbChunkOut,
    KbChunkPage,
    KbDocumentOut,
    KbSearchHit,
    KbSearchRequest,
    KbSearchResult,
    KbSettingsOut,
    KbSettingsUpdate,
    KbUploadResult,
    StaleCounts,
)
from app.services import kb_parser, retrieval_service
from app.services.project_service import get_manageable_project, get_owned_project
from app.services.retrieval_service import Embedder

logger = logging.getLogger(__name__)

MAX_DOCUMENTS = 200
KB_TASK_KIND = "kb.index"
DUPLICATE = "该文件已上传过：《{name}》"


# ---------------- 设置 ----------------


def default_settings(project_id: int) -> KbSettings:
    return KbSettings(
        project_id=project_id,
        auto_inject=True,
        top_k=6,
        min_score=0.35,
        max_chars=3000,
        memory_top_k=8,
        memory_max_chars=1500,
        memory_min_score=0.35,
    )


async def load_settings(db: AsyncSession, project_id: int) -> KbSettings:
    """没有设置行时返回默认值（不写库）。"""
    row = await db.get(KbSettings, project_id)
    return row if row is not None else default_settings(project_id)


async def get_settings(db: AsyncSession, user_id: int, project_id: int) -> KbSettingsOut:
    await get_manageable_project(db, user_id, project_id)
    return KbSettingsOut.model_validate(await load_settings(db, project_id))


async def update_settings(db: AsyncSession, user_id: int, project_id: int, data: KbSettingsUpdate) -> KbSettingsOut:
    await get_manageable_project(db, user_id, project_id, lock=True)
    row = await db.get(KbSettings, project_id)
    if row is None:
        row = default_settings(project_id)
        db.add(row)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return KbSettingsOut.model_validate(row)


# ---------------- 文档 ----------------


async def get_project_document(db: AsyncSession, user_id: int, project_id: int, document_id: int) -> KbDocument:
    await get_manageable_project(db, user_id, project_id)
    doc = await db.scalar(select(KbDocument).where(KbDocument.id == document_id, KbDocument.project_id == project_id))
    if doc is None:
        raise NotFoundError("文档不存在")
    return doc


async def list_documents(db: AsyncSession, user_id: int, project_id: int) -> list[KbDocumentOut]:
    await get_manageable_project(db, user_id, project_id)
    rows = await db.scalars(
        select(KbDocument).where(KbDocument.project_id == project_id).order_by(KbDocument.id.desc())
    )
    return [KbDocumentOut.model_validate(d) for d in rows]


def _clean_filename(name: str) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:255] or "未命名"


async def upload_document(
    db: AsyncSession, engine, user_id: int, project_id: int, filename: str, data: bytes
) -> KbUploadResult:
    from app.services import task_service

    await get_owned_project(db, user_id, project_id, lock=True)
    if len(data) > kb_parser.MAX_FILE_BYTES:
        raise BadRequestError(kb_parser.TOO_LARGE)
    filename = _clean_filename(filename)
    kb_parser.detect_kind(filename, data)
    count = await db.scalar(select(func.count(KbDocument.id)).where(KbDocument.project_id == project_id)) or 0
    if count >= MAX_DOCUMENTS:
        raise BadRequestError(f"每个项目最多 {MAX_DOCUMENTS} 个文档，请先删除不需要的文档")
    sha = hashlib.sha256(data).hexdigest()
    dup = await db.scalar(select(KbDocument).where(KbDocument.project_id == project_id, KbDocument.sha256 == sha))
    if dup is not None:
        raise ConflictError(DUPLICATE.format(name=dup.filename), extra={"document_id": dup.id})
    parsed = await anyio.to_thread.run_sync(kb_parser.parse, filename, data)
    doc = KbDocument(
        project_id=project_id,
        filename=filename,
        mime=parsed.mime,
        size=len(data),
        sha256=sha,
        status="pending",
        text=parsed.text,
        char_count=len(parsed.text),
        warnings=list(parsed.warnings),
    )
    db.add(doc)
    try:
        await db.flush()
    except IntegrityError as exc:
        # 并发上传同一文件：唯一约束兜底
        await db.rollback()
        raise ConflictError(DUPLICATE.format(name=filename)) from exc
    task = await task_service.create_task(
        db, engine, user_id, KB_TASK_KIND, {"project_id": project_id, "mode": "document", "document_id": doc.id}
    )
    await db.refresh(doc)
    return KbUploadResult(document=KbDocumentOut.model_validate(doc), task=task)


async def chunk_ids_of(db: AsyncSession, document_id: int) -> list[int]:
    return list(await db.scalars(select(KbChunk.id).where(KbChunk.document_id == document_id)))


async def set_inject(db: AsyncSession, user_id: int, project_id: int, document_id: int, inject: bool) -> KbDocumentOut:
    await get_manageable_project(db, user_id, project_id, lock=True)
    doc = await get_project_document(db, user_id, project_id, document_id)
    if doc.inject != inject:
        doc.inject = inject
        await retrieval_service.set_flags(db, "kb", await chunk_ids_of(db, doc.id), enabled=inject)
        await db.commit()
        await db.refresh(doc)
    return KbDocumentOut.model_validate(doc)


async def _clear_chunks(db: AsyncSession, doc: KbDocument) -> None:
    await retrieval_service.delete_refs(db, "kb", await chunk_ids_of(db, doc.id))
    await db.execute(delete(KbChunk).where(KbChunk.document_id == doc.id))
    doc.chunk_count = 0


async def _active_doc_task(db: AsyncSession, document_id: int) -> Task | None:
    return await db.scalar(
        select(Task).where(
            Task.kind == KB_TASK_KIND,
            Task.resource_key == doc_resource_key(document_id),
            Task.status.in_(ACTIVE_TASK_STATUSES),
        )
    )


def doc_resource_key(document_id: int) -> str:
    return f"kbdoc:{document_id}"


async def delete_document(db: AsyncSession, user_id: int, project_id: int, document_id: int) -> None:
    await get_manageable_project(db, user_id, project_id, lock=True)
    doc = await get_project_document(db, user_id, project_id, document_id)
    if await _active_doc_task(db, doc.id) is not None:
        raise ConflictError("该文档正在建索引，请等待完成或取消任务后再删除")
    await _clear_chunks(db, doc)
    await db.delete(doc)
    await db.commit()


async def reindex_document(db: AsyncSession, engine, user_id: int, project_id: int, document_id: int):
    from app.services import task_service

    await get_owned_project(db, user_id, project_id, lock=True)
    doc = await get_project_document(db, user_id, project_id, document_id)
    if await _active_doc_task(db, doc.id) is not None:
        raise ConflictError("该文档正在建索引，请稍候再试")
    await _clear_chunks(db, doc)
    doc.status, doc.error = "pending", ""
    await db.flush()
    return await task_service.create_task(
        db, engine, user_id, KB_TASK_KIND, {"project_id": project_id, "mode": "document", "document_id": doc.id}
    )


async def list_chunks(
    db: AsyncSession, user_id: int, project_id: int, document_id: int, *, page: int, page_size: int
) -> KbChunkPage:
    doc = await get_project_document(db, user_id, project_id, document_id)
    total = await db.scalar(select(func.count(KbChunk.id)).where(KbChunk.document_id == doc.id)) or 0
    rows = await db.scalars(
        select(KbChunk)
        .where(KbChunk.document_id == doc.id)
        .order_by(KbChunk.seq, KbChunk.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return KbChunkPage(items=[KbChunkOut.model_validate(c) for c in rows], total=total, page=page, page_size=page_size)


# ---------------- 检索 ----------------


@dataclass
class KbHit:
    hit: retrieval_service.Hit
    chunk: KbChunk
    document: KbDocument


async def search_hits(
    db: AsyncSession,
    project_id: int,
    query: str,
    *,
    top_k: int,
    min_score: float,
    embedder: Embedder | None,
) -> tuple[list[KbHit], retrieval_service.SearchResult]:
    result = await retrieval_service.search(
        db, project_id, "kb", query, top_k=top_k, min_score=min_score, embedder=embedder
    )
    if not result.hits:
        return [], result
    rows = (
        await db.execute(
            select(KbChunk, KbDocument)
            .join(KbDocument, KbDocument.id == KbChunk.document_id)
            .where(KbChunk.id.in_([h.ref_id for h in result.hits]), KbDocument.project_id == project_id)
        )
    ).all()
    by_id = {c.id: (c, d) for c, d in rows}
    return [KbHit(h, *by_id[h.ref_id]) for h in result.hits if h.ref_id in by_id], result


async def search_debug(
    db: AsyncSession, user_id: int, project_id: int, req: KbSearchRequest, *, env: retrieval_service.EmbedEnv
) -> KbSearchResult:
    await get_owned_project(db, user_id, project_id)
    embedder = await env.embedder(db, user_id)
    settings = await load_settings(db, project_id)
    hits, result = await search_hits(
        db,
        project_id,
        req.query,
        top_k=req.top_k or settings.top_k,
        min_score=req.min_score if req.min_score is not None else settings.min_score,
        embedder=embedder,
    )
    return KbSearchResult(
        hits=[
            KbSearchHit(
                chunk_id=h.chunk.id,
                document_id=h.document.id,
                filename=h.document.filename,
                heading_path=h.chunk.heading_path,
                content=h.chunk.content,
                score=h.hit.score,
                vector_score=h.hit.vector_score,
                keyword_score=h.hit.keyword_score,
            )
            for h in hits
        ],
        mode=result.mode,
        warnings=result.warnings,
        candidates=result.candidates,
    )


def format_lines(hits: list[KbHit], max_chars: int) -> list[str]:
    """「- 《文件名》标题路径：内容」，总长度不超过 max_chars（放不下的一条截断后结束）。"""
    lines: list[str] = []
    used = 0
    for h in hits:
        head = f"- 《{h.document.filename}》{h.chunk.heading_path}："
        body = " ".join(h.chunk.content.split())
        line = head + body
        room = max_chars - used - (1 if lines else 0)
        if len(line) > room:
            if room - len(head) >= 50:
                lines.append(line[: room - 2] + "……")
            break
        lines.append(line)
        used += len(line) + (1 if len(lines) > 1 else 0)
    return lines


async def knowledge_text(
    db: AsyncSession, project: Project, query: str, *, embedder: Embedder | None, purpose: str = ""
) -> str:
    """注入用的参考资料正文（不含标题行，标题由模板 / 上下文组装输出）；不注入时为空串。"""
    if not query.strip():
        return ""
    try:
        settings = await load_settings(db, project.id)
        if not settings.auto_inject:
            return ""
        hits, _ = await search_hits(
            db, project.id, query, top_k=settings.top_k, min_score=settings.min_score, embedder=embedder
        )
        return "\n".join(format_lines(hits, settings.max_chars))
    except Exception:  # noqa: BLE001
        logger.warning("知识库检索失败（项目 %s，%s），本次不注入参考资料", project.id, purpose, exc_info=True)
        return ""


async def knowledge_for_project(
    db: AsyncSession,
    project: Project,
    query: str,
    *,
    cipher,
    factory,
    ai_timeout: float,
    allow_private: bool,
    purpose: str = "",
) -> str:
    """向导 / 实体生成 / 灵感模式用：按项目所有者的向量模型检索参考资料（没有向量模型时只用关键词）。"""
    settings = await load_settings(db, project.id)
    if not settings.auto_inject or not query.strip():
        return ""
    has_docs = await db.scalar(select(KbDocument.id).where(KbDocument.project_id == project.id).limit(1))
    if has_docs is None:
        return ""
    embedder = await retrieval_service.get_embedder(
        db, project.user_id, cipher=cipher, factory=factory, ai_timeout=ai_timeout, allow_private=allow_private
    )
    return await knowledge_text(db, project, query, embedder=embedder, purpose=purpose)


# ---------------- 索引状态 ----------------


async def index_status(db: AsyncSession, user_id: int, project_id: int) -> IndexStatus:
    project = await get_owned_project(db, user_id, project_id)
    cfg = await retrieval_service.default_embedding_config(db, project.user_id)
    model_key = retrieval_service.config_model_key(cfg) if cfg is not None else None
    counts = await retrieval_service.stale_counts(db, project_id, model_key)
    indexing = await db.scalar(
        select(Task.id)
        .where(Task.project_id == project_id, Task.kind == KB_TASK_KIND, Task.status.in_(ACTIVE_TASK_STATUSES))
        .limit(1)
    )
    total = counts["kb"] + counts["memory"]
    return IndexStatus(
        embedding_model=f"{cfg.name} · {cfg.model}" if cfg is not None else None,
        needs_rebuild=cfg is not None and total > 0,
        stale_count=total,
        stale=StaleCounts(**counts),
        indexing=indexing is not None,
    )


async def start_rebuild(db: AsyncSession, engine, user_id: int, project_id: int):
    await get_owned_project(db, user_id, project_id, lock=True)
    from app.services import task_service

    return await task_service.create_task(
        db, engine, user_id, KB_TASK_KIND, {"project_id": project_id, "mode": "rebuild"}
    )
