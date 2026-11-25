"""章节工作台协作批注。"""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.core.deps import CurrentUser, DbDep
from app.schemas.annotations import MessageCreate, MessageOut, MessagePage, ThreadCreate, ThreadOut, ThreadPage
from app.services import annotations as svc

router = APIRouter(prefix="/projects/{project_id}/chapters/{chapter_id}/annotations", tags=["annotations"])
PathId = Annotated[int, Path(ge=1, le=2_147_483_647)]
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=200)]


@router.get("", response_model=ThreadPage)
async def list_threads(project_id: PathId, chapter_id: PathId, user: CurrentUser, db: DbDep,
                       offset: Offset = 0, limit: Limit = 50) -> ThreadPage:
    return await svc.list_threads(db, user, project_id, chapter_id, offset, limit)


@router.post("", response_model=ThreadOut, status_code=201)
async def create_thread(project_id: PathId, chapter_id: PathId, body: ThreadCreate,
                        user: CurrentUser, db: DbDep) -> ThreadOut:
    return await svc.create_thread(db, user, project_id, chapter_id, body)


@router.get("/{thread_id}/messages", response_model=MessagePage)
async def list_messages(project_id: PathId, chapter_id: PathId, thread_id: PathId, user: CurrentUser, db: DbDep,
                        offset: Offset = 0, limit: Limit = 50) -> MessagePage:
    return await svc.list_messages(db, user, project_id, chapter_id, thread_id, offset, limit)


@router.post("/{thread_id}/messages", response_model=MessageOut, status_code=201)
async def reply(project_id: PathId, chapter_id: PathId, thread_id: PathId, body: MessageCreate,
                user: CurrentUser, db: DbDep) -> MessageOut:
    return await svc.reply(db, user, project_id, chapter_id, thread_id, body)


@router.post("/{thread_id}/resolve", response_model=ThreadOut)
async def resolve(project_id: PathId, chapter_id: PathId, thread_id: PathId,
                  user: CurrentUser, db: DbDep) -> ThreadOut:
    return await svc.resolve(db, user, project_id, chapter_id, thread_id)
