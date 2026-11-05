"""四来源全文提及与正文分布；预览为无副作用的人工读取。"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, DbDep
from app.schemas.mentions import (
    EntityCategory,
    EntityKind,
    MentionCatalog,
    MentionPage,
    MentionPreview,
    MentionPreviewRequest,
    MentionSummary,
    SourceType,
    SourceWindow,
)
from app.services.mentions import query, summary, window

router = APIRouter(prefix="/projects/{project_id}/mentions", tags=["mentions"])


@router.get("/catalog", response_model=MentionCatalog)
async def catalog(project_id: int, user: CurrentUser, db: DbDep) -> MentionCatalog:
    return await query.catalog(db, user.id, project_id)


@router.post("/preview", response_model=MentionPreview)
async def preview(project_id: int, body: MentionPreviewRequest, user: CurrentUser, db: DbDep) -> MentionPreview:
    return await query.preview(db, user.id, project_id, body.text)


@router.get("", response_model=MentionPage)
async def list_mentions(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    entity_kind: EntityKind | None = None,
    entity_id: Annotated[int | None, Query(ge=1)] = None,
    source_type: SourceType | None = None,
    chapter_from: Annotated[int | None, Query(ge=1)] = None,
    chapter_to: Annotated[int | None, Query(ge=1)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MentionPage:
    return await query.list_mentions(
        db,
        user.id,
        project_id,
        entity_kind=entity_kind,
        entity_id=entity_id,
        source_type=source_type,
        chapter_from=chapter_from,
        chapter_to=chapter_to,
        page=page,
        page_size=page_size,
    )


@router.get("/summary", response_model=MentionSummary)
async def summarize(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    entity_kind: EntityKind | None = None,
    entity_id: Annotated[int | None, Query(ge=1)] = None,
    category: EntityCategory | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    chapter_from: Annotated[int | None, Query(ge=1)] = None,
    chapter_to: Annotated[int | None, Query(ge=1)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MentionSummary:
    return await summary.summarize(
        db,
        user.id,
        project_id,
        entity_kind=entity_kind,
        entity_id=entity_id,
        category=category,
        q=q,
        chapter_from=chapter_from,
        chapter_to=chapter_to,
        page=page,
        page_size=page_size,
    )


@router.get("/sources/{source_type}/{source_id}", response_model=SourceWindow)
async def source_window(
    project_id: int,
    source_type: SourceType,
    source_id: int,
    user: CurrentUser,
    db: DbDep,
    start: Annotated[int, Query(ge=0)] = 0,
    end: Annotated[int | None, Query(ge=1)] = None,
    content_hash: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
) -> SourceWindow:
    return await window.source_window(
        db, user.id, project_id, source_type, source_id, start=start, end=end, content_hash=content_hash
    )
