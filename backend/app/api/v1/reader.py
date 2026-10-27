from fastapi import APIRouter, Response, status

from app.core.deps import CurrentUser, DbDep
from app.schemas.reader import ReadingProgressIn, TocItem
from app.services import reader_service as svc

router = APIRouter(prefix="/projects/{project_id}", tags=["reader"])


@router.put("/reading-progress", status_code=status.HTTP_204_NO_CONTENT)
async def save_reading_progress(project_id: int, body: ReadingProgressIn, user: CurrentUser, db: DbDep) -> Response:
    await svc.save_progress(db, user.id, project_id, body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/reader/toc", response_model=list[TocItem])
async def reader_toc(project_id: int, user: CurrentUser, db: DbDep) -> list[TocItem]:
    return await svc.toc(db, user.id, project_id)
