"""作品正文搜索和无副作用的逐项预览。锁内重新验证后才可交给批次服务写入。"""

import hashlib
import hmac
import json
from collections import defaultdict
from dataclasses import dataclass
from itertools import batched

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, UnauthorizedError
from app.models import Chapter, Scene, User
from app.schemas.book_search import (
    ApplyRequest,
    PreviewRequest,
    SearchHit,
    SearchPage,
    SearchRequest,
    SearchScope,
    SelectedHit,
)
from app.schemas.rich_document import SceneDocument, is_text_boundary, validate_chapter_document_boundaries
from app.services import project_access, scene_service
from app.services.book_search_mapping import (
    VerifiedPreviews,
    lost_marker_keys,
    replacement_warning,
    replay_replacements,
)
from app.services.rich_document_mapping import UnsafeMapping
from app.services.rich_document_store import capture_documents
from app.services.scene_move_history import chapter_identity_expression

CONTEXT_SIZE = 40
CHAPTERS_PER_READ = 16


@dataclass
class SceneChange:
    scene: Scene
    before: SceneDocument
    after: SceneDocument
    start_offsets: list[int]


@dataclass
class ChapterDocuments:
    scenes: list[Scene]
    ranges: list[scene_service.SceneRange]
    docs: dict[int, SceneDocument]
    has_metadata: bool

    def check_seams(self, updated: dict[int, SceneDocument]) -> None:
        parts = [(s.separator_before, updated.get(s.id, self.docs[s.id])) for s in self.scenes]
        # 本地元数据已验证，只在跨场景或分隔接缝形成 CRLF 时复核章内坐标。
        previous = ""
        for separator, document in parts:
            for segment in (separator, document.content):
                if segment:
                    if previous == "\r" and segment.startswith("\n"):
                        try:
                            validate_chapter_document_boundaries(parts)
                        except ValueError as exc:
                            raise UnsafeMapping(str(exc)) from exc
                        return
                    previous = segment[-1]


def _bytes(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


def _scope_changed() -> ConflictError:
    return ConflictError("搜索范围或章节内容已变化，请重新搜索", extra={"code": "book_search_stale"})


@dataclass(frozen=True)
class RangeEnd:
    order: tuple[int, int]
    scene_identity: int | None


type Bounds = tuple[RangeEnd, RangeEnd] | None


async def _bounds(db: AsyncSession, project_id: int, body: SearchScope) -> Bounds:
    if body.start_chapter_id is None:
        return None
    ends = {cid: RangeEnd((number, cid), identity) for cid, number, identity in await db.execute(
        select(Chapter.id, Chapter.chapter_no, chapter_identity_expression()).where(
            Chapter.project_id == project_id, Chapter.id.in_((body.start_chapter_id, body.end_chapter_id)),
        )
    )}
    if body.start_chapter_id not in ends or body.end_chapter_id not in ends:
        raise ConflictError("起止章节已不存在，请重新选择范围", extra={"code": "book_search_scope_missing"})
    left, right = ends[body.start_chapter_id], ends[body.end_chapter_id]
    if left.order > right.order:
        raise BadRequestError("起始章节不能晚于结束章节")
    return left, right


async def _catalog(db: AsyncSession, project_id: int, body: SearchRequest):
    bounds = await _bounds(db, project_id, body)
    statement = select(
        Chapter.id, Chapter.chapter_no, Chapter.title, Chapter.content_revision, chapter_identity_expression(),
    ).where(Chapter.project_id == project_id)
    if bounds:
        order = tuple_(Chapter.chapter_no, Chapter.id)
        statement = statement.where(order >= bounds[0].order, order <= bounds[1].order)
    rows = (await db.execute(statement.order_by(Chapter.chapter_no, Chapter.id))).all()
    snapshot = hashlib.sha256(_bytes([
        project_id, body.query, body.start_chapter_id, body.end_chapter_id, [tuple(row) for row in rows],
    ])).hexdigest()
    return rows, bounds, snapshot


async def _documents(db: AsyncSession, chapter_id: int, content: str) -> ChapterDocuments:
    scenes = await scene_service.list_active_scenes(db, chapter_id)
    projected, ranges = scene_service.project_scenes(scenes)
    if projected != content:
        raise _scope_changed()
    docs, has_metadata = await capture_documents(db, scenes)
    return ChapterDocuments(scenes, ranges, docs, has_metadata)


async def _preview_actor(db: AsyncSession) -> tuple[int, int, str]:
    actor = db.info["authenticated_actor"]
    created_at = await db.scalar(select(User.created_at).where(User.id == actor.id))
    if created_at is None:
        raise UnauthorizedError("账号已变化，请重新登录")
    return actor.id, actor.token_version, created_at.isoformat()


def _signature(
    db: AsyncSession, project_id: int, body: PreviewRequest | ApplyRequest,
    bounds: Bounds, hit: SearchHit, scene_id: int, warning: str | None, actor: tuple[int, int, str],
) -> str:
    # 起止主键与稳定身份绑定。范围内未选章节的普通修订不会使已勾选的预览失效。
    identities = [b.scene_identity for b in bounds] if bounds else None
    data = [project_id, actor, body.query, body.replacement,
            body.start_chapter_id, body.end_chapter_id, identities, hit.chapter_id, hit.chapter_no,
            hit.chapter_title, hit.revision, hit.content_hash, scene_id, hit.start, hit.end, warning]
    return hmac.digest(db.info["book_search_hmac_key"], _bytes(data), "sha256").hex()


def _preview_hit(
    db: AsyncSession, project_id: int, body: PreviewRequest | ApplyRequest, bounds: Bounds,
    hit: SearchHit, content: str, documents: ChapterDocuments, actor: tuple[int, int, str],
    verified: VerifiedPreviews | None = None,
) -> None:
    target, _ = scene_service.range_target(documents.ranges, hit.start, hit.end)
    if target is None:
        hit.unavailable_reason = "命中跨越场景或位于场景分隔符，请在编辑器中核对"
        return
    hit.scene_id = target.scene_id
    if not is_text_boundary(content, hit.start) or not is_text_boundary(content, hit.end):
        hit.unavailable_reason = "命中拆分了 CRLF 换行符"
        return
    if body.query == body.replacement:
        hit.unavailable_reason = "替换前后相同"
        return
    # 普通正文没有需要重映射的结构。预览只取窗口，批量应用时一次拼接整景。
    if documents.has_metadata:
        original = documents.docs[target.scene_id]
        local_start, local_end = hit.start - target.start, hit.end - target.start
        try:
            mapped = replay_replacements(original, body.query, body.replacement, [local_start])
            documents.check_seams({target.scene_id: mapped})
        except UnsafeMapping as exc:
            hit.unavailable_reason = str(exc)
            return
        lost = lost_marker_keys(original, mapped)
        hit.warning = replacement_warning(original, local_start, local_end, len(lost))
        if verified is not None:
            verified.record(local_start, mapped, lost)
    hit.token = _signature(db, project_id, body, bounds, hit, target.scene_id, hit.warning, actor)


async def search(
    db: AsyncSession, user_id: int, project_id: int, body: SearchRequest | PreviewRequest,
) -> SearchPage:
    await project_access.require_project(db, user_id, project_id, "content.read")
    catalog, bounds, snapshot = await _catalog(db, project_id, body)
    if body.snapshot is not None and body.snapshot != snapshot:
        raise _scope_changed()
    actor = await _preview_actor(db) if isinstance(body, PreviewRequest) else None
    total = 0
    first = (body.page - 1) * body.page_size
    items: list[SearchHit] = []
    for batch in batched(catalog, CHAPTERS_PER_READ):
        contents = dict((await db.execute(select(Chapter.id, Chapter.content).where(
            Chapter.project_id == project_id, Chapter.id.in_([row[0] for row in batch]),
        ))).all())
        for cid, number, title, revision, _identity in batch:
            if cid not in contents:
                raise _scope_changed()
            content = contents[cid]
            count = content.count(body.query)
            offset = max(0, first - total)
            total += count
            take = min(count - offset, body.page_size - len(items))
            if take <= 0:
                continue
            fingerprint = scene_service.content_fingerprint(content)
            documents = await _documents(db, cid, content) if isinstance(body, PreviewRequest) else None
            cursor = 0
            for index in range(offset + take):
                start = content.index(body.query, cursor)
                cursor = start + len(body.query)
                if index < offset:
                    continue
                hit = SearchHit(chapter_id=cid, chapter_no=number, chapter_title=title, revision=revision,
                                content_hash=fingerprint, start=start, end=cursor,
                                before=content[max(0, start - CONTEXT_SIZE):start],
                                after=content[cursor:cursor + CONTEXT_SIZE])
                if documents is not None and actor is not None:
                    _preview_hit(db, project_id, body, bounds, hit, content, documents, actor)
                items.append(hit)
    if (await _catalog(db, project_id, body))[2] != snapshot:
        raise _scope_changed()
    await project_access.require_project(db, user_id, project_id, "content.read")
    return SearchPage(items=items, total=total, page=body.page, page_size=body.page_size, snapshot=snapshot)


async def plan_chapter(
    db: AsyncSession, project_id: int, chapter: Chapter, body: ApplyRequest, hits: list[SelectedHit],
) -> list[SceneChange]:
    """外层已取得人工写锁；无写入，可逐章收集冲突。"""
    if any(hit.chapter_id != chapter.id or hit.revision != chapter.content_revision for hit in hits):
        raise _scope_changed()
    try:
        bounds = await _bounds(db, project_id, body)
    except BadRequestError as exc:
        raise _scope_changed() from exc
    if bounds and not bounds[0].order <= (chapter.chapter_no, chapter.id) <= bounds[1].order:
        raise _scope_changed()
    content = chapter.content
    documents = await _documents(db, chapter.id, content)
    starts = {r.scene_id: r.start for r in documents.ranges}
    offsets: dict[int, list[int]] = defaultdict(list)
    verified: dict[int, VerifiedPreviews] = defaultdict(VerifiedPreviews)
    fingerprint = scene_service.content_fingerprint(content)
    actor = await _preview_actor(db)
    for selected in hits:
        if (selected.chapter_no != chapter.chapter_no or selected.chapter_title != chapter.title
                or content[selected.start:selected.end] != body.query):
            raise _scope_changed()
        hit = SearchHit(chapter_id=chapter.id, chapter_no=chapter.chapter_no, chapter_title=chapter.title,
                        revision=chapter.content_revision, content_hash=fingerprint, start=selected.start,
                        end=selected.end, before="", after="")
        _preview_hit(db, project_id, body, bounds, hit, content, documents, actor, verified[selected.scene_id])
        if (hit.scene_id != selected.scene_id or hit.token is None
                or not hmac.compare_digest(hit.token, selected.token)):
            raise ConflictError(hit.unavailable_reason or "命中与替换预览不一致，请重新预览")
        offsets[selected.scene_id].append(hit.start - starts[selected.scene_id])
    changes = []
    try:
        for scene in documents.scenes:
            if scene.id in offsets:
                before = documents.docs[scene.id]
                selected_offsets = sorted(offsets[scene.id])
                after = replay_replacements(before, body.query, body.replacement, selected_offsets,
                                            verified=verified.pop(scene.id))
                changes.append(SceneChange(scene, before, after, selected_offsets))
        documents.check_seams({c.scene.id: c.after for c in changes})
    except UnsafeMapping as exc:
        raise ConflictError(str(exc)) from exc
    return changes
