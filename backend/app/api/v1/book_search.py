"""全书检索、修订预览及批次历史。"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, DbDep, EditorUser
from app.schemas.book_search import ApplyRequest, BatchPage, BatchSummary, PreviewRequest, SearchPage, SearchRequest
from app.services import batch_revision, book_search

router = APIRouter(prefix="/projects/{project_id}/book-search", tags=["book-search"])


@router.post("/query", response_model=SearchPage)
async def query(project_id: int, body: SearchRequest, user: CurrentUser, db: DbDep):
    return await book_search.search(db, user.id, project_id, body)


@router.post("/preview", response_model=SearchPage)
async def preview(project_id: int, body: PreviewRequest, user: CurrentUser, db: DbDep):
    return await book_search.search(db, user.id, project_id, body)


@router.post("/apply", response_model=BatchSummary)
async def apply(project_id: int, body: ApplyRequest, user: EditorUser, db: DbDep):
    return await batch_revision.apply(db, user.id, project_id, body)


@router.get("/batches", response_model=BatchPage)
async def batches(
    project_id: int, user: CurrentUser, db: DbDep,
    page: Annotated[int, Query(ge=1, le=2_147_483_647)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    result = await batch_revision.history(db, user.id, project_id, (page - 1) * page_size, page_size)
    return {**result, "page": page, "page_size": page_size}


@router.post("/batches/{batch_id}/undo", response_model=BatchSummary)
async def undo(project_id: int, batch_id: int, user: EditorUser, db: DbDep):
    return await batch_revision.undo(db, user.id, project_id, batch_id)
