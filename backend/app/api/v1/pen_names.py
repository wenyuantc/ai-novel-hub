from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentUser, DbDep
from app.schemas.pen_names import PenNameCreate, PenNameOut, PenNameUpdate
from app.services import pen_name_service as svc

router = APIRouter(prefix="/pen-names", tags=["pen-names"])


@router.get("", response_model=list[PenNameOut])
async def list_pen_names(user: CurrentUser, db: DbDep):
    return await svc.list_pen_names(db, user.id)


@router.post("", response_model=PenNameOut, status_code=status.HTTP_201_CREATED)
async def create_pen_name(body: PenNameCreate, user: CurrentUser, db: DbDep):
    return await svc.create_pen_name(db, user.id, body.name)


@router.patch("/{pen_name_id}", response_model=PenNameOut)
async def update_pen_name(pen_name_id: int, body: PenNameUpdate, user: CurrentUser, db: DbDep):
    return await svc.update_pen_name(db, user.id, pen_name_id, body.name, body.expected_revision)


@router.delete("/{pen_name_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pen_name(
    pen_name_id: int, user: CurrentUser, db: DbDep, expected_revision: int = Query(ge=1),
) -> Response:
    await svc.delete_pen_name(db, user.id, pen_name_id, expected_revision)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
