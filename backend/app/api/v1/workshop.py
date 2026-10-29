"""工坊公开浏览、本人分享和管理员审核。路由只负责依赖与 schema。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response, status

from app.core.deps import AdminUser, CurrentUser, DbDep
from app.schemas.workshop import (
    SubmissionStatus,
    WorkshopApprove,
    WorkshopCategory,
    WorkshopImportOut,
    WorkshopItemList,
    WorkshopItemOut,
    WorkshopKind,
    WorkshopReject,
    WorkshopSubmissionList,
    WorkshopSubmissionOut,
    WorkshopSubmit,
)
from app.services import workshop_service as svc

router = APIRouter(prefix="/workshop", tags=["workshop"])
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=50)]


@router.get("/items", response_model=WorkshopItemList)
async def list_items(
    user: CurrentUser,
    db: DbDep,
    kind: WorkshopKind | None = None,
    category: WorkshopCategory | None = None,
    tag: Annotated[str | None, Query(max_length=12)] = None,
    prompt_key: Annotated[str | None, Query(max_length=100)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    sort: Literal["newest", "likes", "downloads"] = "newest",
    page: Page = 1,
    page_size: PageSize = 20,
) -> WorkshopItemList:
    return await svc.list_items(
        db,
        user,
        kind=kind,
        category=category,
        tag=tag,
        prompt_key=prompt_key,
        q=q,
        sort=sort,
        page=page,
        page_size=page_size,
    )


@router.get("/mine/items", response_model=WorkshopItemList)
async def my_items(user: CurrentUser, db: DbDep, page: Page = 1, page_size: PageSize = 20) -> WorkshopItemList:
    return await svc.list_items(db, user, mine=True, page=page, page_size=page_size)


@router.get("/submissions", response_model=WorkshopSubmissionList)
async def my_submissions(
    user: CurrentUser, db: DbDep, status: SubmissionStatus | None = None, page: Page = 1, page_size: PageSize = 20
) -> WorkshopSubmissionList:
    return await svc.list_submissions(db, user, status=status, page=page, page_size=page_size)


@router.post("/submissions", response_model=WorkshopSubmissionOut, status_code=status.HTTP_201_CREATED)
async def submit(body: WorkshopSubmit, user: CurrentUser, db: DbDep) -> WorkshopSubmissionOut:
    return await svc.submit(db, user, body)


@router.get("/submissions/{submission_id}", response_model=WorkshopSubmissionOut)
async def get_submission(submission_id: int, user: CurrentUser, db: DbDep) -> WorkshopSubmissionOut:
    return await svc.get_submission(db, user, submission_id)


@router.post("/submissions/{submission_id}/withdraw", response_model=WorkshopSubmissionOut)
async def withdraw(submission_id: int, user: CurrentUser, db: DbDep) -> WorkshopSubmissionOut:
    return await svc.withdraw(db, user, submission_id)


@router.get("/admin/submissions", response_model=WorkshopSubmissionList)
async def review_list(
    admin: AdminUser, db: DbDep, status: SubmissionStatus | None = "pending", page: Page = 1, page_size: PageSize = 20
) -> WorkshopSubmissionList:
    return await svc.list_submissions(db, admin, admin=True, status=status, page=page, page_size=page_size)


@router.post("/admin/submissions/{submission_id}/approve", response_model=WorkshopSubmissionOut)
async def approve(submission_id: int, body: WorkshopApprove, admin: AdminUser, db: DbDep) -> WorkshopSubmissionOut:
    return await svc.approve(db, admin, submission_id, body)


@router.post("/admin/submissions/{submission_id}/reject", response_model=WorkshopSubmissionOut)
async def reject(submission_id: int, body: WorkshopReject, admin: AdminUser, db: DbDep) -> WorkshopSubmissionOut:
    return await svc.reject(db, admin, submission_id, body.note)


@router.post("/admin/items", response_model=WorkshopItemOut, status_code=status.HTTP_201_CREATED)
async def publish(body: WorkshopSubmit, admin: AdminUser, db: DbDep) -> WorkshopItemOut:
    submission = await svc.submit(db, admin, body)
    return await svc.get_item(db, admin, submission.item_id)


@router.patch("/admin/items/{item_id}", response_model=WorkshopItemOut)
async def patch_metadata(item_id: int, body: WorkshopApprove, admin: AdminUser, db: DbDep) -> WorkshopItemOut:
    return await svc.patch_metadata(db, admin, item_id, body)


@router.delete("/admin/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(item_id: int, admin: AdminUser, db: DbDep) -> Response:
    await svc.delete_item(db, admin, item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/items/{item_id}", response_model=WorkshopItemOut)
async def detail(item_id: int, user: CurrentUser, db: DbDep) -> WorkshopItemOut:
    return await svc.get_item(db, user, item_id)


@router.post("/items/{item_id}/hide", response_model=WorkshopItemOut)
async def hide(item_id: int, user: CurrentUser, db: DbDep) -> WorkshopItemOut:
    return await svc.set_visibility(db, user, item_id, hidden=True)


@router.post("/items/{item_id}/unhide", response_model=WorkshopItemOut)
async def unhide(item_id: int, user: CurrentUser, db: DbDep) -> WorkshopItemOut:
    return await svc.set_visibility(db, user, item_id, hidden=False)


@router.post("/items/{item_id}/like", response_model=WorkshopItemOut)
async def like(item_id: int, user: CurrentUser, db: DbDep) -> WorkshopItemOut:
    return await svc.like(db, user, item_id, liked=True)


@router.delete("/items/{item_id}/like", response_model=WorkshopItemOut)
async def unlike(item_id: int, user: CurrentUser, db: DbDep) -> WorkshopItemOut:
    return await svc.like(db, user, item_id, liked=False)


@router.post("/items/{item_id}/import", response_model=WorkshopImportOut)
async def import_item(item_id: int, user: CurrentUser, db: DbDep) -> WorkshopImportOut:
    return await svc.import_item(db, user, item_id)
