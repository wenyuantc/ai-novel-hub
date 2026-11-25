from fastapi import APIRouter, Response, status

from app.core.deps import CipherDep, CurrentUser, DbDep, FactoryDep, SettingsDep
from app.schemas.reader import ReaderCastingRequest, ReaderCastingResult, ReaderChapter, ReadingProgressIn, TocItem
from app.services import reader_casting_service
from app.services import reader_service as svc

router = APIRouter(prefix="/projects/{project_id}", tags=["reader"])


@router.put("/reading-progress", status_code=status.HTTP_204_NO_CONTENT)
async def save_reading_progress(project_id: int, body: ReadingProgressIn, user: CurrentUser, db: DbDep) -> Response:
    await svc.save_progress(db, user.id, project_id, body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/reader/toc", response_model=list[TocItem])
async def reader_toc(project_id: int, user: CurrentUser, db: DbDep) -> list[TocItem]:
    return await svc.toc(db, user.id, project_id)


@router.get("/reader/chapters/{chapter_id}", response_model=ReaderChapter)
async def reader_chapter(project_id: int, chapter_id: int, user: CurrentUser, db: DbDep) -> ReaderChapter:
    return await svc.chapter(db, user.id, project_id, chapter_id)


@router.post("/reader/chapters/{chapter_id}/ai-casting", response_model=ReaderCastingResult)
async def reader_ai_casting(
    project_id: int, chapter_id: int, body: ReaderCastingRequest, user: CurrentUser, db: DbDep,
    settings: SettingsDep, cipher: CipherDep, factory: FactoryDep,
) -> ReaderCastingResult:
    return await reader_casting_service.generate(
        db, user.id, project_id, chapter_id, body, cipher, factory, ai_timeout=settings.ai_request_timeout,
        allow_private=settings.allow_private_ai_endpoints, context_budget=settings.context_char_budget,
    )
