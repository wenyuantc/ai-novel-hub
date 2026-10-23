from fastapi import APIRouter, Response, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
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
from app.schemas.task import TaskOut
from app.services import chapter_service as svc
from app.services import task_service

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


@router.post("/chapters/{chapter_id}/generate", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def generate_chapter(
    chapter_id: int,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: GenerateChapterRequest | None = None,
) -> TaskOut:
    params = {"chapter_id": chapter_id, **(body or GenerateChapterRequest()).model_dump(mode="json")}
    return await task_service.create_task(db, engine, user.id, "chapter.generate", params)


@router.post("/chapters/{chapter_id}/polish", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def polish_chapter(
    chapter_id: int,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: PolishChapterRequest | None = None,
) -> TaskOut:
    params = {"chapter_id": chapter_id, **(body or PolishChapterRequest()).model_dump(mode="json")}
    return await task_service.create_task(db, engine, user.id, "chapter.polish", params)
