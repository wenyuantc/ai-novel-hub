"""知识库：检索设置、文档上传 / 注入开关 / 删除 / 重建、分块查看、检索调试、索引状态与提示词注入块。

协作（E6-c）：无作者模型授权的编辑者只维护关键词；已授权编辑者上传 / 重新索引 / 重建使用作者向量
（任务归编辑者，经 kb_index_service 协作受理冻结原邀请轮次与作者向量选择）；作者未配置向量或配置本地校验
不可用时上传仍同步关键词（与作者本人上传降级一致），重新索引 / 重建作为显式向量操作仍返回 400。

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
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.models import ChapterAnalysis, KbChunk, KbDocument, KbSettings, Project, RetrievalChunk, StoryMemory, Task
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
from app.services import authorization_service, kb_parser, project_access, retrieval_service
from app.services.ai_dispatch import VisibilityPolicyError, VisibilityStamp, visibility_sessionmaker
from app.services.project_access import require_project
from app.services.retrieval_service import Embedder

logger = logging.getLogger(__name__)

MAX_DOCUMENTS = 200
KB_TASK_KIND = "kb.index"
DUPLICATE = "该文件已上传过：《{name}》"
KEYWORD_ONLY_COLLABORATOR = "作者未授权使用共享向量模型，仅使用关键词检索"
KEYWORD_ONLY_OWNER_UNCONFIGURED = "作者未配置向量模型，仅使用关键词检索"
KEYWORD_ONLY_OWNER_UNAVAILABLE = "作者共享向量模型配置不可用，仅使用关键词检索"


async def _owner_embedding_usable(db: AsyncSession, engine, user_id: int, project_id: int) -> bool:
    """持锁事务内按受理同一规则做本地校验（不联网）；仅 400 类配置不可用视为可降级。"""
    from app.services import project_ai_config

    ctx = getattr(engine, "ctx", None)
    if ctx is None:
        raise NotFoundError("项目不存在")
    try:
        await project_ai_config.resolve_shared_embedding(
            db, user_id, project_id, cipher=ctx.cipher, require_vector=True,
            allow_private=ctx.settings.allow_private_ai_endpoints, for_update=True,
        )
    except BadRequestError:
        return False
    return True


def _require_original_actor(db: AsyncSession, user_id: int) -> authorization_service.AuthenticatedActor:
    actor = db.info.get("authenticated_actor")
    if type(actor) is not authorization_service.AuthenticatedActor or actor.id != user_id:
        raise UnauthorizedError("未登录或登录已过期")
    return actor


@dataclass(frozen=True)
class ContentWriteActor:
    """仅从已认证的 HTTP 会话生成；不能根据新邀请补签旧写入。"""

    actor: authorization_service.AuthenticatedActor
    project_id: int
    owner_id: int
    grant: tuple[str, int] | None
    source: str


async def capture_content_write_actor(db: AsyncSession, user_id: int, project_id: int) -> ContentWriteActor:
    actor = _require_original_actor(db, user_id)
    project, access, member = await project_access.resolve_access(db, user_id, project_id)
    if not access.can_edit_content:
        await require_project(db, user_id, project_id, "content.edit")
        raise ForbiddenError("当前作品没有执行该操作的权限", extra={"code": "project_readonly"})
    grant = (member.invitation_id, member.revision) if access.is_collaborator and member is not None else None
    source = "owner" if project.user_id == user_id else (
        "editor" if member is not None and member.status == "accepted" and member.role == "editor" else "admin"
    )
    return ContentWriteActor(actor, project_id, project.user_id, grant, source)


async def lock_content_write_actor(
    db: AsyncSession, identity: ContentWriteActor, *, visibility_write: bool = False,
) -> Project:
    """新事务取得写屏障，再核对原 token、权限来源和邀请轮次。

    visibility_write：后续会在同一事务中受理协作向量任务（需发送写门禁）时，须在项目父行锁之前取得。
    """
    if (type(identity) is not ContentWriteActor or _require_original_actor(db, identity.actor.id) != identity.actor):
        raise UnauthorizedError("未登录或登录已过期")
    if db.new or db.dirty or db.deleted:
        raise ConflictError("当前事务存在未提交内容，无法安全处理")
    await db.rollback()
    project, access, member = await project_access.resolve_access(
        db, identity.actor.id, identity.project_id, for_update=True, visibility_write=visibility_write,
    )
    if project.user_id != identity.owner_id:
        raise NotFoundError("项目不存在")
    if identity.grant is not None and (
        member is None or member.status != "accepted"
        or (member.invitation_id, member.revision) != identity.grant
    ):
        raise ForbiddenError("协作授权已变化", extra={"code": "permission_denied"})
    if not access.can_edit_content:
        await require_project(db, identity.actor.id, identity.project_id, "content.edit")
        raise ForbiddenError("当前作品没有执行该操作的权限", extra={"code": "project_readonly"})
    if identity.source == "owner":
        if project.user_id != identity.actor.id or identity.grant is not None:
            raise ForbiddenError("作品授权已变化", extra={"code": "permission_denied"})
    elif identity.source == "editor":
        if identity.grant is None or member is None or member.status != "accepted" or member.role != "editor":
            raise ForbiddenError("协作授权已变化", extra={"code": "permission_denied"})
    elif identity.source == "admin":
        snapshot = await authorization_service.require_authenticated(db, identity.actor)
        if not snapshot.allows("projects.read") or not snapshot.allows("projects.edit"):
            raise ForbiddenError("管理权限已变化", extra={"code": "permission_denied"})
    else:
        raise ForbiddenError("作品授权已变化", extra={"code": "permission_denied"})
    return project


async def _locked_embedding_actor(db: AsyncSession, user_id: int, project_id: int):
    """确定人工索引请求的真实来源；锁内的协作者授权不能被作者身份代替。

    非作者随后可能在同一事务受理共享向量任务：发送写门禁须先于项目父行锁（锁序见 design §5）。
    """
    with db.no_autoflush:
        owner_id = await db.scalar(select(Project.user_id).where(Project.id == project_id))
    project, access, member = await project_access.resolve_access(
        db, user_id, project_id, for_update=True, visibility_write=owner_id is not None and owner_id != user_id,
    )
    if project.user_id == user_id:
        return project, None
    _require_original_actor(db, user_id)
    if not access.can_use_ai or not access.can_edit_content or member is None or member.role != "editor":
        await require_project(db, user_id, project_id, "ai.use")
        await require_project(db, user_id, project_id, "content.edit")
        raise NotFoundError("项目不存在")
    return project, member


async def _require_vector_task_owner(db: AsyncSession, user_id: int, project_id: int) -> Project:
    project, member = await _locked_embedding_actor(db, user_id, project_id)
    if member is not None and not member.allow_owner_ai:
        # 裁决 10：无授权编辑者的重新索引（含仅关键词重分块）与重建一律拒绝。
        raise ForbiddenError("作者未授权共享向量模型", extra={"code": "owner_embedding_not_allowed"})
    # 已授权编辑者（E6-c）：受理在同一事务按锁内原邀请轮次冻结作者向量选择，任务归编辑者。
    return project


async def search_access(
    db: AsyncSession, user_id: int, project_id: int, env: retrieval_service.EmbedEnv, *,
    chapter_id: int | None = None,
):
    """同步检索（检索调试 / 注入预览 / HTTP 上下文）的向量选择，返回 ``(embedder, grant)``。

    - 作者：原有本人默认向量（行为不变），grant 为 None。
    - accepted editor：锁内固定原邀请轮次。无作者模型授权或作者无可用向量配置 → 关键词（embedder 为 None，
      请求事务保持持锁）；已授权且作者有配置 → 冻结作者共享向量选择，结束持锁事务后经 ``for_http`` 受控发送
      构造作者向量的查询 embedder（每次真实发送在租约内重验原令牌、原轮次、授权与配置；读写作者旧键空间，
      裁决 1）。协作者本人的向量配置永不参与。
    - viewer 403；待接受 / 非成员 / 已撤权 / 仅管理员 404（``require_project(ai.use)``）。
    """
    project, access, member = await project_access.resolve_access(db, user_id, project_id)
    if project.user_id == user_id:
        return await env.embedder(db, user_id, project_id=project_id, chapter_id=chapter_id), None
    _require_original_actor(db, user_id)
    if not access.can_use_ai or member is None or member.role != "editor":
        await require_project(db, user_id, project_id, "ai.use")
        raise NotFoundError("项目不存在")
    identity = (member.invitation_id, member.revision)
    # 路由前置读取可能处于旧事务，重新取得 User → Project 写屏障并匹配原邀请。
    if db.new or db.dirty or db.deleted:
        raise ConflictError("当前事务存在未提交内容，无法安全检索")
    await db.rollback()
    project, access, member = await project_access.resolve_access(db, user_id, project_id, for_update=True)
    if (project.user_id == user_id or not access.can_use_ai or member is None or member.role != "editor"
            or member.status != "accepted" or (member.invitation_id, member.revision) != identity):
        raise ForbiddenError("协作授权已变化", extra={"code": "permission_denied"})
    if not member.allow_owner_ai:
        return None, identity
    from app.services import collaboration_ai_guard, project_ai_config
    from app.services.ai_dispatch import read_visibility_stamp

    try:
        selection = await project_ai_config.resolve_shared_embedding(
            db, user_id, project_id, cipher=env.cipher, allow_private=env.allow_private, for_update=True,
        )
    except BadRequestError:
        # 作者配置存在但本地校验不可用：与作者本人检索一致退回关键词，不回退协作者本人配置。
        return None, identity
    if selection.grant_identity != identity:
        raise ForbiddenError("协作授权已变化", extra={"code": "permission_denied"})
    if selection.mode != "vector":
        return None, identity
    stamp = await read_visibility_stamp(db, project_id)
    # 构造 provider（含 DNS）与发送租约都在独立会话进行，不能持本请求的写屏障等待。
    await db.rollback()
    maker = visibility_sessionmaker(db)
    guard = collaboration_ai_guard.for_http(
        db, user_id, selection, stamp, cipher=env.cipher, allow_private=env.allow_private,
    )
    try:
        bound = await guard.prepare_provider(
            maker, env.factory, request_timeout=env.ai_timeout,
            usage_kind="retrieval.query", usage_chapter_id=chapter_id,
        )
    except VisibilityPolicyError:
        # prepare_provider 把授权变化与普通构造故障（DNS、端点暂不可达等）统一为阻断。锁内按原轮次复核：
        # 原授权、原选择仍完全成立 → 属普通故障，与作者本人检索一致退回关键词（裁决 11，持锁返回）；
        # 否则失败关闭。退回关键词不扩大能力（未授权编辑者本就只用关键词）。
        await _require_unchanged_shared_selection(db, user_id, project_id, identity, selection, env)
        return None, identity
    # 作者旧键 provider:model（裁决 1）：与作者及授权编辑者索引任务写入的空间一致。
    embedder = retrieval_service.Embedder(provider=bound.provider, provider_name=bound.provider_name,
                                          model=bound.model)
    return retrieval_service.guard_embedder(embedder, stamp, maker), identity


async def _require_unchanged_shared_selection(
    db: AsyncSession, user_id: int, project_id: int, identity: tuple[str, int],
    selection, env: retrieval_service.EmbedEnv,
) -> None:
    """新事务锁内确认原邀请轮次、accepted editor、作者模型授权与冻结的共享向量选择均未变化；否则 403。"""
    from app.services import project_ai_config

    changed = ForbiddenError("协作授权已变化", extra={"code": "permission_denied"})
    if db.new or db.dirty or db.deleted:
        raise ConflictError("当前事务存在未提交内容，无法安全检索")
    await db.rollback()
    project, access, member = await project_access.resolve_access(db, user_id, project_id, for_update=True)
    if (project.user_id == user_id or not access.can_use_ai or member is None or member.role != "editor"
            or member.status != "accepted" or (member.invitation_id, member.revision) != identity
            or not member.allow_owner_ai):
        raise changed
    try:
        await project_ai_config.revalidate_shared_embedding(
            db, selection, cipher=env.cipher, allow_private=env.allow_private, for_update=True,
        )
    except Exception:  # noqa: BLE001  任何复核失败都不能被当作普通故障降级。
        raise changed from None


_PUBLIC_QUERY_EMBED_FAILED = "查询向量生成失败，本次只用关键词检索"


def public_search_warnings(warnings: list[str], grant: tuple[str, int] | None) -> list[str]:
    """协作者看到的检索提示：提供方错误原文可能含作者的模型名 / 端点，只保留固定文案。"""
    if grant is None:
        return warnings
    allowed = {retrieval_service.KEYWORD_ONLY_STALE, _PUBLIC_QUERY_EMBED_FAILED,
               f"查询向量生成超时（{retrieval_service.QUERY_EMBED_TIMEOUT_SECONDS} 秒），本次只用关键词检索"}
    return list(dict.fromkeys(w if w in allowed else _PUBLIC_QUERY_EMBED_FAILED for w in warnings))


async def _lock_search_result(db: AsyncSession, user_id: int, project_id: int, grant: tuple[str, int] | None):
    """最终读取前重验原认证与原邀请。

    作者（grant 为 None）：新事务取得作品写屏障。协作者：按原轮次读取当前授权（关键词路径的请求事务已由
    ``search_access`` 持锁；向量路径已在每次真实发送及检索末尾预检的租约内重验，这里是返回前复核）。"""
    _require_original_actor(db, user_id)
    if grant is None:
        if db.new or db.dirty or db.deleted:
            raise ConflictError("当前事务存在未提交内容，无法安全检索")
        await db.rollback()
        project = await require_project(db, user_id, project_id, "ai.use", for_update=True)
        if project.user_id != user_id:
            raise NotFoundError("项目不存在")
    else:
        await require_project(db, user_id, project_id, "ai.use", grant_identity=grant)


async def verify_retrieval_hits(
    db: AsyncSession, project_id: int, source: retrieval_service.Source, result: retrieval_service.SearchResult,
    *, enabled_only: bool = True, chapter_lt: int | None = None,
) -> None:
    """最终命中的索引、正文和筛选字段必须仍指向同一作品。"""
    hits = result.hits
    if not hits:
        return
    with db.no_autoflush:
        snapshots = await retrieval_service.snapshot_embedding_inputs(db, project_id, [hit.chunk_id for hit in hits])
        if any(s.source != source for s in snapshots):
            raise retrieval_service._invalid_retrieval_reference()
        await retrieval_service.validate_project_source_refs(db, project_id, source, [hit.ref_id for hit in hits])
        rows = {row.id: row for row in (await db.execute(select(
            RetrievalChunk.id, RetrievalChunk.ref_id, RetrievalChunk.text, RetrievalChunk.pinned,
            RetrievalChunk.chapter_no, RetrievalChunk.enabled,
        ).where(RetrievalChunk.id.in_([hit.chunk_id for hit in hits]), RetrievalChunk.project_id == project_id,
                RetrievalChunk.source == source))).all()}
        refs = [hit.ref_id for hit in hits]
        if source == "kb":
            sources = {chunk_id: enabled for chunk_id, enabled in (await db.execute(
                select(KbChunk.id, KbDocument.inject).join(KbDocument, KbDocument.id == KbChunk.document_id)
                .where(KbChunk.id.in_(refs), KbDocument.project_id == project_id)
            )).all()}
        else:
            sources = {row.id: row for row in (await db.execute(select(
                StoryMemory.id, StoryMemory.muted, StoryMemory.pinned, StoryMemory.chapter_no,
                StoryMemory.chapter_id, StoryMemory.analysis_id,
            ).where(StoryMemory.id.in_(refs), StoryMemory.project_id == project_id))).all()}
            analysis_ids = {item.analysis_id for item in sources.values() if item.analysis_id is not None}
            analyses = {row.id: row for row in (await db.execute(select(
                ChapterAnalysis.id, ChapterAnalysis.project_id, ChapterAnalysis.chapter_id,
            ).where(ChapterAnalysis.id.in_(analysis_ids)))).all()} if analysis_ids else {}
    for hit, snapshot in zip(hits, snapshots, strict=True):
        row, item = rows.get(hit.chunk_id), sources.get(hit.ref_id)
        if row is None or item is None:
            raise retrieval_service._invalid_retrieval_reference()
        if (snapshot.ref_id != hit.ref_id or row.ref_id != hit.ref_id or snapshot.text != hit.text
                or row.text != hit.text or row.pinned != hit.pinned
                or (row.chapter_no if row.chapter_no is not None and row.chapter_no >= 0 else None) != hit.chapter_no
                or (enabled_only and not row.enabled) or (chapter_lt is not None and (
                    row.chapter_no is None or row.chapter_no >= chapter_lt))):
            raise retrieval_service._invalid_retrieval_reference()
        if source == "kb":
            valid = row.enabled == item
        else:
            valid = row.enabled == (not item.muted) and row.pinned == item.pinned and row.chapter_no == item.chapter_no
            if item.analysis_id is not None:
                analysis = analyses.get(item.analysis_id)
                valid = valid and analysis is not None and analysis.project_id == project_id \
                    and analysis.chapter_id == item.chapter_id
        if not valid:
            raise retrieval_service._invalid_retrieval_reference()


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
    await require_project(db, user_id, project_id, "content.read")
    return KbSettingsOut.model_validate(await load_settings(db, project_id))


async def update_settings(db: AsyncSession, user_id: int, project_id: int, data: KbSettingsUpdate) -> KbSettingsOut:
    await require_project(db, user_id, project_id, "content.edit", for_update=True)
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
    await require_project(db, user_id, project_id, "content.read")
    doc = await db.scalar(select(KbDocument).where(KbDocument.id == document_id, KbDocument.project_id == project_id))
    if doc is None:
        raise NotFoundError("文档不存在")
    return doc


async def list_documents(db: AsyncSession, user_id: int, project_id: int) -> list[KbDocumentOut]:
    await require_project(db, user_id, project_id, "content.read")
    rows = await db.scalars(
        select(KbDocument).where(KbDocument.project_id == project_id).order_by(KbDocument.id.desc())
    )
    return [KbDocumentOut.model_validate(d) for d in rows]


def _clean_filename(name: str) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:255] or "未命名"


async def upload_document(
    db: AsyncSession, engine, user_id: int, project_id: int, filename: str, data: bytes, *,
    write_actor: ContentWriteActor,
) -> KbUploadResult:
    from app.services import task_service

    if write_actor.actor.id != user_id or write_actor.project_id != project_id:
        raise UnauthorizedError("未登录或登录已过期")
    await lock_content_write_actor(db, write_actor, visibility_write=write_actor.source == "editor")
    project, member = await _locked_embedding_actor(db, user_id, project_id)
    grant = write_actor.grant
    keyword_warning = None
    if grant is not None:
        if member is None or not member.allow_owner_ai:
            keyword_warning = KEYWORD_ONLY_COLLABORATOR
        elif await retrieval_service.default_embedding_config(db, project.user_id) is None:
            # 裁决 2：已授权但作者没有向量配置 → 同步关键词，并提示作者未配置。
            keyword_warning = KEYWORD_ONLY_OWNER_UNCONFIGURED
        elif not await _owner_embedding_usable(db, engine, user_id, project_id):
            # 作者配置存在但本地校验不可用（密钥无法解密、端点格式 / 内网拦截、绑定无效）：与作者本人上传
            # 同样降级为关键词（裁决 11 普通故障），不阻断人工上传、零向量调用；授权类错误仍原样抛出。
            keyword_warning = KEYWORD_ONLY_OWNER_UNAVAILABLE
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
    if keyword_warning is not None:
        from app.services import kb_index_service

        await kb_index_service.chunk_document(db, doc)
        doc.status = "ready_keyword_only"
        doc.warnings = [*doc.warnings, keyword_warning]
        await db.commit()
        await db.refresh(doc)
        document_id = doc.id
        await lock_content_write_actor(db, write_actor)
        doc = await db.scalar(select(KbDocument).where(
            KbDocument.id == document_id, KbDocument.project_id == project_id,
        ).execution_options(populate_existing=True))
        if doc is None:
            raise NotFoundError("文档不存在")
        return KbUploadResult(document=KbDocumentOut.model_validate(doc), task=None)
    task = await task_service.create_task(
        db, engine, user_id, KB_TASK_KIND, {"project_id": project_id, "mode": "document", "document_id": doc.id}
    )
    await db.refresh(doc)
    return KbUploadResult(document=KbDocumentOut.model_validate(doc), task=task)


async def chunk_ids_of(db: AsyncSession, document_id: int) -> list[int]:
    with db.no_autoflush:
        return list(await db.scalars(select(KbChunk.id).where(KbChunk.document_id == document_id)))


def _require_document_identity(doc: KbDocument, project_id: int, document_id: int) -> None:
    state = inspect(doc)
    if (doc.id != document_id or doc.project_id != project_id
            or state.attrs.id.history.has_changes() or state.attrs.project_id.history.has_changes()):
        raise retrieval_service._invalid_retrieval_reference()


async def set_inject(db: AsyncSession, user_id: int, project_id: int, document_id: int, inject: bool) -> KbDocumentOut:
    await require_project(db, user_id, project_id, "content.edit", for_update=True)
    with db.no_autoflush:
        doc = await get_project_document(db, user_id, project_id, document_id)
        _require_document_identity(doc, project_id, document_id)
        chunk_ids = await chunk_ids_of(db, document_id)
        if doc.inject == inject:
            await retrieval_service.validate_project_source_refs(db, project_id, "kb", chunk_ids)
            return KbDocumentOut.model_validate(doc)
        await retrieval_service.set_project_flags(db, project_id, "kb", chunk_ids, enabled=inject)
        doc.inject = inject
        await db.commit()
        await db.refresh(doc)
    return KbDocumentOut.model_validate(doc)


async def _clear_chunks(db: AsyncSession, project_id: int, doc: KbDocument) -> None:
    await retrieval_service.delete_project_refs(db, project_id, "kb", await chunk_ids_of(db, doc.id))
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
    await require_project(db, user_id, project_id, "content.edit", for_update=True)
    with db.no_autoflush:
        doc = await get_project_document(db, user_id, project_id, document_id)
        _require_document_identity(doc, project_id, document_id)
        if await _active_doc_task(db, document_id) is not None:
            raise ConflictError("该文档正在建索引，请等待完成或取消任务后再删除")
        await _clear_chunks(db, project_id, doc)
    await db.delete(doc)
    await db.commit()


async def reindex_document(db: AsyncSession, engine, user_id: int, project_id: int, document_id: int):
    from app.services import task_service

    await _require_vector_task_owner(db, user_id, project_id)
    with db.no_autoflush:
        doc = await get_project_document(db, user_id, project_id, document_id)
        _require_document_identity(doc, project_id, document_id)
        if await _active_doc_task(db, document_id) is not None:
            raise ConflictError("该文档正在建索引，请稍候再试")
        await _clear_chunks(db, project_id, doc)
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
    chunk: "_KbSourceChunk"
    document: "_KbSourceDocument"


@dataclass(frozen=True)
class _KbSourceChunk:
    id: int
    heading_path: str
    content: str


@dataclass(frozen=True)
class _KbSourceDocument:
    id: int
    filename: str


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
    return await _materialize_search_hits(db, project_id, result), result


async def _materialize_search_hits(
    db: AsyncSession, project_id: int, result: retrieval_service.SearchResult,
) -> list[KbHit]:
    await verify_retrieval_hits(db, project_id, "kb", result)
    if not result.hits:
        return []
    with db.no_autoflush:
        rows = (
            await db.execute(
                select(
                    KbChunk.id, KbChunk.heading_path, KbChunk.content, KbDocument.id.label("document_id"),
                    KbDocument.filename, KbDocument.inject,
                )
                .join(KbDocument, KbDocument.id == KbChunk.document_id)
                .where(KbChunk.id.in_([h.ref_id for h in result.hits]), KbDocument.project_id == project_id)
            )
        ).all()
    by_id = {row.id: row for row in rows}
    if set(by_id) != {h.ref_id for h in result.hits}:
        raise retrieval_service._invalid_retrieval_reference()
    hits = []
    for hit in result.hits:
        row = by_id[hit.ref_id]
        if not row.inject or kb_parser.retrieval_text(row.heading_path, row.content) != hit.text:
            raise retrieval_service._invalid_retrieval_reference()
        hits.append(KbHit(hit, _KbSourceChunk(row.id, row.heading_path, row.content),
                          _KbSourceDocument(row.document_id, row.filename)))
    return hits


async def search_debug(
    db: AsyncSession, user_id: int, project_id: int, req: KbSearchRequest, *, env: retrieval_service.EmbedEnv
) -> KbSearchResult:
    embedder, grant = await search_access(db, user_id, project_id, env)
    settings = await load_settings(db, project_id)
    hits, result = await search_hits(
        db,
        project_id,
        req.query,
        top_k=req.top_k or settings.top_k,
        min_score=req.min_score if req.min_score is not None else settings.min_score,
        embedder=embedder,
    )
    await _lock_search_result(db, user_id, project_id, grant)
    hits = await _materialize_search_hits(db, project_id, result)
    output = KbSearchResult(
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
        warnings=public_search_warnings(result.warnings, grant),
        candidates=result.candidates,
    )
    if grant is not None:
        await require_project(db, user_id, project_id, "ai.use", grant_identity=grant)
    return output


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
    except VisibilityPolicyError:
        raise
    except ConflictError as exc:
        if exc.extra and exc.extra.get("code") == "retrieval_reference_invalid":
            raise VisibilityPolicyError("检索数据关联不一致，已停止发送", extra={
                "code": "retrieval_reference_invalid",
            }) from None
        raise
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
    visibility_stamp: VisibilityStamp | None = None,
) -> str:
    """向导 / 实体生成 / 灵感模式用：按项目所有者的向量模型检索参考资料（没有向量模型时只用关键词）。"""
    settings = await load_settings(db, project.id)
    if not settings.auto_inject or not query.strip():
        return ""
    has_docs = await db.scalar(select(KbDocument.id).where(KbDocument.project_id == project.id).limit(1))
    if has_docs is None:
        return ""
    embedder = await retrieval_service.get_embedder(
        db, project.user_id, cipher=cipher, factory=factory, ai_timeout=ai_timeout, allow_private=allow_private,
        project_id=project.id,
    )
    if embedder is not None and visibility_stamp is not None:
        embedder = retrieval_service.guard_embedder(embedder, visibility_stamp, visibility_sessionmaker(db))
    return await knowledge_text(db, project, query, embedder=embedder, purpose=purpose)


async def knowledge_for_http(
    db: AsyncSession, *, user_id: int, project_id: int, query: str, env: retrieval_service.EmbedEnv,
    purpose: str = "",
) -> str:
    """同步 HTTP 上下文的参考资料正文（E6-d）：作者沿用本人向量；accepted editor 固定原邀请轮次，
    已授权且作者有向量时用作者共享向量（受控发送，读作者旧键空间），否则只用关键词。

    与 ``knowledge_text`` 一样只把普通检索故障降级为空串；授权、可见性与检索关联类错误原样抛出，
    返回前在新事务内按原轮次复核，撤权后不返回任何资料。
    """
    embedder, grant = await search_access(db, user_id, project_id, env)
    settings = await load_settings(db, project_id)
    if not settings.auto_inject or not query.strip():
        await _lock_search_result(db, user_id, project_id, grant)
        return ""
    try:
        result = await retrieval_service.search(
            db, project_id, "kb", query, top_k=settings.top_k, min_score=settings.min_score, embedder=embedder,
        )
    except (VisibilityPolicyError, ConflictError):
        raise
    except Exception:  # noqa: BLE001  与 knowledge_text 相同：普通检索故障不阻断，只记日志。
        logger.warning("知识库检索失败（项目 %s，%s），本次不注入参考资料", project_id, purpose, exc_info=True)
        result = retrieval_service.SearchResult()
    await _lock_search_result(db, user_id, project_id, grant)
    hits = await _materialize_search_hits(db, project_id, result)
    text = "\n".join(format_lines(hits, settings.max_chars))
    if grant is not None:
        await require_project(db, user_id, project_id, "ai.use", grant_identity=grant)
    return text


# ---------------- 索引状态 ----------------


async def index_status(
    db: AsyncSession, user_id: int, project_id: int, *, env: retrieval_service.EmbedEnv,
) -> IndexStatus:
    """作者完整视图；accepted editor 精简视图（裁决 8）：不返回作者配置名 / 模型名 / ID，
    只说明本人检索是否走作者共享向量及降级原因。``stale_count`` 即「N 条资料待建向量」（裁决 7，
    按作者当前默认向量计算，作者与编辑者同口径）。viewer 403；待接受 / 非成员 / 已撤权 / 仅管理员 404。

    裁决 16：作者向量配置存在但本地不可用（解密失败、端点无效等，与上传 ``_owner_embedding_usable`` 同口径，
    不联网）→ ``retrieval_mode=keyword`` + ``owner_embedding_unavailable``；不返回具体故障原因。"""
    project = await require_project(db, user_id, project_id, "ai.use")
    owner_id = project.user_id
    grant: tuple[str, int] | None = None
    allowed = True
    if owner_id != user_id:
        _, access, member = await project_access.resolve_access(db, user_id, project_id)
        if not access.can_use_ai or member is None or member.status != "accepted" or member.role != "editor":
            raise NotFoundError("项目不存在")
        grant = (member.invitation_id, member.revision)
        allowed = bool(member.allow_owner_ai)
    cfg = await retrieval_service.default_embedding_config(db, owner_id)
    model_key = retrieval_service.config_model_key(cfg) if cfg is not None else None
    counts = await retrieval_service.stale_counts(db, project_id, model_key)
    indexing = await db.scalar(
        select(Task.id)
        .where(Task.project_id == project_id, Task.kind == KB_TASK_KIND, Task.status.in_(ACTIVE_TASK_STATUSES))
        .limit(1)
    )
    total = counts["kb"] + counts["memory"]
    if not allowed:
        mode, reason = "keyword", "owner_embedding_not_allowed"
    elif cfg is None:
        mode, reason = "keyword", "embedding_config_missing"
    elif not await _shared_embedding_usable(db, user_id, project_id, env):
        mode, reason = "keyword", "owner_embedding_unavailable"
    else:
        mode, reason = "vector", None
    out = IndexStatus(
        embedding_model=f"{cfg.name} · {cfg.model}" if cfg is not None and grant is None else None,
        needs_rebuild=cfg is not None and total > 0,
        stale_count=total,
        stale=StaleCounts(**counts),
        indexing=indexing is not None,
        owner_embedding_configured=cfg is not None,
        retrieval_mode=mode,
        keyword_reason=reason,
    )
    if grant is not None:
        await require_project(db, user_id, project_id, "ai.use", grant_identity=grant)
    return out


async def _shared_embedding_usable(
    db: AsyncSession, user_id: int, project_id: int, env: retrieval_service.EmbedEnv,
) -> bool:
    """只读的本地可用性判定（不加锁、不联网）：与 ``_owner_embedding_usable`` 同一规则，
    仅 400 类配置不可用视为不可用；授权类错误原样抛出。"""
    from app.services import project_ai_config

    try:
        await project_ai_config.resolve_shared_embedding(
            db, user_id, project_id, cipher=env.cipher, require_vector=True, allow_private=env.allow_private,
        )
    except BadRequestError:
        return False
    return True


async def start_rebuild(db: AsyncSession, engine, user_id: int, project_id: int):
    await _require_vector_task_owner(db, user_id, project_id)
    from app.services import task_service

    return await task_service.create_task(
        db, engine, user_id, KB_TASK_KIND, {"project_id": project_id, "mode": "rebuild"}
    )
