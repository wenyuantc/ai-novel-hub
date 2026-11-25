"""当前登录账号的收件、偏好及白名单跳转。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.core.deps import DbDep, EditorUser
from app.schemas.notifications import Kind, NotificationPage, NotificationTarget, PreferenceUpdate, UnreadCount
from app.services import notification_service as service


def _private(response: Response):
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(prefix="/notifications", tags=["notifications"], dependencies=[Depends(_private)])


@router.get("", response_model=NotificationPage)
async def list_items(user: EditorUser, db: DbDep, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                     before: Annotated[str | None, Query(max_length=64)] = None):
    return await service.list_items(db, user.id, limit, before)


@router.get("/unread-count", response_model=UnreadCount)
async def count(user: EditorUser, db: DbDep):
    return UnreadCount(count=await service.unread_count(db, user.id))


@router.get("/preferences", response_model=dict[Kind, bool])
async def preferences(user: EditorUser, db: DbDep):
    return await service.preferences(db, user.id)


@router.put("/preferences/{kind}", response_model=dict[Kind, bool])
async def set_preference(kind: Kind, body: PreferenceUpdate, user: EditorUser, db: DbDep):
    return await service.set_preference(db, user.id, kind, body.enabled)


@router.post("/{notification_id}/read", status_code=204)
async def mark_read(notification_id: UUID, user: EditorUser, db: DbDep):
    await service.mark_read(db, user.id, str(notification_id), open_target=False)


@router.post("/{notification_id}/open", response_model=NotificationTarget)
async def open_target(notification_id: UUID, user: EditorUser, db: DbDep):
    return await service.mark_read(db, user.id, str(notification_id), open_target=True)
