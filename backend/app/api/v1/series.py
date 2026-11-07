"""内部作者系列与共享档案 API。"""

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.core.deps import CurrentUser, DbDep
from app.schemas import series as s
from app.services import series_service as svc

router = APIRouter(prefix="/series", tags=["series"])
Id = Annotated[int, Path(ge=1, le=2_147_483_647)]
Revision = Annotated[int, Query(ge=0, le=2_147_483_647)]


@router.get("")
async def list_series(user: CurrentUser, db: DbDep) -> list[s.SeriesOut]:
    return await svc.list_series(db, user.id)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_series(body: s.SeriesCreate, user: CurrentUser, db: DbDep) -> s.SeriesOut:
    return await svc.create(db, user.id, body)


@router.get("/{sid}")
async def read_series(sid: Id, user: CurrentUser, db: DbDep) -> s.SeriesDetail:
    return await svc.detail(db, user.id, sid)


@router.patch("/{sid}")
async def rename_series(sid: Id, body: s.SeriesRename, user: CurrentUser, db: DbDep) -> s.SeriesOut:
    return await svc.rename(db, user.id, sid, body)


@router.delete("/{sid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_series(sid: Id, expected_revision: Revision, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_series(db, user.id, sid, expected_revision)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{sid}/books")
async def join_book(sid: Id, body: s.SeriesBookMutation, user: CurrentUser, db: DbDep) -> s.SeriesOut:
    return await svc.join(db, user.id, sid, body)


@router.delete("/{sid}/books/{pid}")
async def leave_book(sid: Id, pid: Id, expected_revision: Revision, user: CurrentUser, db: DbDep) -> s.SeriesOut:
    return await svc.leave(db, user.id, sid, pid, expected_revision)


@router.get("/{sid}/entries")
async def list_entries(
    sid: Id,
    user: CurrentUser,
    db: DbDep,
    kind: s.SeriesKind | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 30,
) -> s.SeriesEntryPage:
    return await svc.list_entries(db, user.id, sid, kind=kind, q=q, page=page, page_size=page_size)


@router.post("/{sid}/entries", status_code=status.HTTP_201_CREATED)
async def create_entry(sid: Id, body: s.SeriesEntryCreate, user: CurrentUser, db: DbDep) -> s.SeriesEntryMutation:
    return await svc.create_entry(db, user.id, sid, body)


@router.post("/{sid}/entries/promote", status_code=status.HTTP_201_CREATED)
async def promote_entry(sid: Id, body: s.SeriesPromote, user: CurrentUser, db: DbDep) -> s.SeriesEntryMutation:
    return await svc.promote(db, user.id, sid, body)


@router.get("/{sid}/entries/{eid}")
async def read_entry(sid: Id, eid: Id, user: CurrentUser, db: DbDep) -> s.SeriesEntryOut:
    return await svc.read_entry(db, user.id, sid, eid)


@router.patch("/{sid}/entries/{eid}")
async def patch_entry(
    sid: Id, eid: Id, body: s.SeriesEntryPatch, user: CurrentUser, db: DbDep,
) -> s.SeriesEntryMutation:
    return await svc.patch_entry(db, user.id, sid, eid, body)


@router.delete("/{sid}/entries/{eid}")
async def delete_entry(
    sid: Id, eid: Id, expected_revision: Revision, user: CurrentUser, db: DbDep,
) -> s.SeriesEntryMutation:
    return await svc.delete_entry(db, user.id, sid, eid, expected_revision)


@router.post("/{sid}/entries/{eid}/uses")
async def use_entry(
    sid: Id, eid: Id, body: s.SeriesBookMutation, user: CurrentUser, db: DbDep,
) -> s.SeriesEntryMutation:
    return await svc.use_entry(db, user.id, sid, eid, body)


@router.delete("/{sid}/entries/{eid}/uses/{pid}")
async def unuse_entry(
    sid: Id, eid: Id, pid: Id, expected_revision: Revision, user: CurrentUser, db: DbDep,
) -> s.SeriesEntryMutation:
    return await svc.unuse(db, user.id, sid, eid, pid, expected_revision)


@router.post("/{sid}/entries/{eid}/move-to-book")
async def move_entry(
    sid: Id, eid: Id, body: s.SeriesBookMutation, user: CurrentUser, db: DbDep,
) -> s.SeriesEntryMutation:
    return await svc.move_entry(db, user.id, sid, eid, body)


@router.get("/{sid}/impact")
async def impact(
    sid: Id,
    action: s.SeriesImpactAction,
    user: CurrentUser,
    db: DbDep,
    project_id: Annotated[int | None, Query(ge=1, le=2_147_483_647)] = None,
    kind: s.SeriesKind | None = None,
    entry_id: Annotated[int | None, Query(ge=1, le=2_147_483_647)] = None,
) -> s.SeriesImpactOut:
    return await svc.impact(db, user.id, sid, action=action, pid=project_id, kind=kind, eid=entry_id)
