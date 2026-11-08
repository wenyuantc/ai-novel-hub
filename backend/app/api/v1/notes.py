"""独立作品笔记，不触发模型调用或自动上下文注入。"""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentUser, DbDep
from app.schemas.notes import NoteCreate, NoteFlags, NoteOut, NotePage, NoteUpdate
from app.services import note_service as svc

router = APIRouter(tags=["notes"])
BASE = "/projects/{project_id}/notes"


@router.get(BASE, response_model=NotePage)
async def list_notes(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    favorite: bool | None = None,
    pinned: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return await svc.list_notes(
        db, user.id, project_id, q=q, favorite=favorite, pinned=pinned, page=page, page_size=page_size
    )


@router.post(BASE, response_model=NoteOut, status_code=status.HTTP_201_CREATED)
async def create_note(project_id: int, body: NoteCreate, user: CurrentUser, db: DbDep):
    return await svc.create_note(db, user.id, project_id, body)


@router.get(BASE + "/{note_id}", response_model=NoteOut)
async def get_note(project_id: int, note_id: int, user: CurrentUser, db: DbDep):
    return await svc.get_note(db, user.id, project_id, note_id)


@router.patch(BASE + "/{note_id}", response_model=NoteOut)
async def update_note(project_id: int, note_id: int, body: NoteUpdate, user: CurrentUser, db: DbDep):
    return await svc.update_note(db, user.id, project_id, note_id, body)


@router.put(BASE + "/{note_id}/flags", response_model=NoteOut)
async def set_note_flags(project_id: int, note_id: int, body: NoteFlags, user: CurrentUser, db: DbDep):
    return await svc.set_note_flags(db, user.id, project_id, note_id, body)


@router.delete(BASE + "/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    project_id: int,
    note_id: int,
    user: CurrentUser,
    db: DbDep,
    expected_revision: Annotated[int, Query(ge=1)],
) -> Response:
    await svc.delete_note(db, user.id, project_id, note_id, expected_revision)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
