from fastapi import APIRouter, Response, status
from fastapi.responses import StreamingResponse

from app.api.sse import sse_response
from app.core.deps import CipherDep, CurrentUser, DbDep, FactoryDep, SessionmakerDep, SettingsDep
from app.models import Chapter
from app.schemas.chapter import (
    ChapterBrief,
    ChapterCreate,
    ChapterOut,
    ChaptersFromOutlines,
    ChapterUpdate,
    GenerateChapterRequest,
    PolishChapterRequest,
)
from app.services import chapter_service as svc
from app.services import generation_service

router = APIRouter(tags=["chapters"])


@router.get("/projects/{project_id}/chapters", response_model=list[ChapterBrief])
async def list_chapters(project_id: int, user: CurrentUser, db: DbDep) -> list[Chapter]:
    return await svc.list_chapters(db, user.id, project_id)


@router.post("/projects/{project_id}/chapters", response_model=ChapterOut, status_code=status.HTTP_201_CREATED)
async def create_chapter(project_id: int, body: ChapterCreate, user: CurrentUser, db: DbDep) -> Chapter:
    return await svc.create_chapter(db, user.id, project_id, body)


@router.post(
    "/projects/{project_id}/chapters/from-outlines",
    response_model=list[ChapterBrief],
    status_code=status.HTTP_201_CREATED,
)
async def create_from_outlines(
    project_id: int, body: ChaptersFromOutlines, user: CurrentUser, db: DbDep
) -> list[Chapter]:
    return await svc.create_from_outlines(db, user.id, project_id, body)


@router.get("/projects/{project_id}/chapters/{chapter_id}", response_model=ChapterOut)
async def get_chapter(project_id: int, chapter_id: int, user: CurrentUser, db: DbDep) -> Chapter:
    return await svc.get_project_chapter(db, user.id, project_id, chapter_id)


@router.patch("/projects/{project_id}/chapters/{chapter_id}", response_model=ChapterOut)
async def update_chapter(
    project_id: int, chapter_id: int, body: ChapterUpdate, user: CurrentUser, db: DbDep
) -> Chapter:
    return await svc.update_chapter(db, user.id, project_id, chapter_id, body)


@router.delete("/projects/{project_id}/chapters/{chapter_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chapter(project_id: int, chapter_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_chapter(db, user.id, project_id, chapter_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/chapters/{chapter_id}/generate", response_class=StreamingResponse)
async def generate_chapter(
    chapter_id: int,
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    cipher: CipherDep,
    factory: FactoryDep,
    sessionmaker: SessionmakerDep,
    body: GenerateChapterRequest | None = None,
) -> StreamingResponse:
    job = await generation_service.prepare_chapter_job(
        db,
        user_id=user.id,
        chapter_id=chapter_id,
        mode="generate",
        req=body or GenerateChapterRequest(),
        cipher=cipher,
        factory=factory,
        ai_timeout=settings.ai_request_timeout,
        allow_private=settings.allow_private_ai_endpoints,
        context_budget=settings.context_char_budget,
    )
    await db.close()
    return sse_response(generation_service.run_chapter_job(job, sessionmaker))


@router.post("/chapters/{chapter_id}/polish", response_class=StreamingResponse)
async def polish_chapter(
    chapter_id: int,
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    cipher: CipherDep,
    factory: FactoryDep,
    sessionmaker: SessionmakerDep,
    body: PolishChapterRequest | None = None,
) -> StreamingResponse:
    job = await generation_service.prepare_chapter_job(
        db,
        user_id=user.id,
        chapter_id=chapter_id,
        mode="polish",
        req=body or PolishChapterRequest(),
        cipher=cipher,
        factory=factory,
        ai_timeout=settings.ai_request_timeout,
        allow_private=settings.allow_private_ai_endpoints,
        context_budget=settings.context_char_budget,
    )
    await db.close()
    return sse_response(generation_service.run_chapter_job(job, sessionmaker))
