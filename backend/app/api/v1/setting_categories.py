"""分类配置、只读成员/草稿预览与独立整理标签；权限由明确的服务边界校验。"""

from typing import Annotated

from fastapi import APIRouter, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.core.deps import CurrentUser, DbDep, SessionmakerDep
from app.schemas import setting_categories as s
from app.schemas.progression import Boundary
from app.schemas.setting_fields import EntryKind, EntryType
from app.services import authorization_service as authz
from app.services import setting_category_query_service as query
from app.services import setting_category_service as service

router = APIRouter(prefix="/projects/{project_id}", tags=["setting-categories"])
Id = Annotated[int, Path(ge=1, le=2_147_483_647)]
Revision = Annotated[int, Query(ge=0, le=2_147_483_647)]


def _actor(db, user) -> authz.AuthenticatedActor:
    actor = authz.actor_snapshot(db, user)
    if not isinstance(actor, authz.AuthenticatedActor):
        raise TypeError("分类读取必须携带已认证的真实身份")
    return actor


@router.get("/setting-categories", response_model=s.CategoryPage)
async def categories(
    project_id: Id,
    user: CurrentUser,
    db: DbDep,
    sessionmaker: SessionmakerDep,
    types: Annotated[list[EntryType] | None, Query(max_length=9)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1, le=2_147_483_647)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return await service.list_categories(
        sessionmaker, _actor(db, user), project_id, types=types, q=q, page=page, page_size=page_size
    )


@router.post("/setting-categories", response_model=s.CategoryMutation, status_code=201)
async def create_category(project_id: Id, body: s.CategoryCreate, user: CurrentUser, db: DbDep):
    return await service.save_category(db, user.id, project_id, body)


# 固定preview路径先于动态分类ID；只读快照仅需projects.read，不进入任何写/审计上下文。
@router.post("/setting-categories/preview", response_model=s.CategoryEntryPage)
async def preview(project_id: Id, body: s.CategoryPreview, user: CurrentUser, db: DbDep, sessionmaker: SessionmakerDep):
    return await query.query_category_entries(
        sessionmaker,
        _actor(db, user),
        project_id,
        draft=body.draft,
        editing_category_id=body.editing_category_id,
        filters=body.filters,
    )


@router.get("/setting-categories/{category_id}", response_model=s.CategoryMutation)
async def category(project_id: Id, category_id: Id, user: CurrentUser, db: DbDep, sessionmaker: SessionmakerDep):
    return await service.read_category(sessionmaker, _actor(db, user), project_id, category_id)


@router.patch("/setting-categories/{category_id}", response_model=s.CategoryMutation)
async def update_category(project_id: Id, category_id: Id, body: s.CategoryUpdate, user: CurrentUser, db: DbDep):
    return await service.save_category(db, user.id, project_id, body, category_id)


@router.delete("/setting-categories/{category_id}", response_model=s.CategoryVersions)
async def delete_category(
    project_id: Id,
    category_id: Id,
    expected_categories_revision: Revision,
    user: CurrentUser,
    db: DbDep,
):
    return await service.delete_category(db, user.id, project_id, category_id, expected_categories_revision)


@router.get("/setting-categories/{category_id}/entries", response_model=s.CategoryEntryPage)
async def entries(
    project_id: Id,
    category_id: Id,
    user: CurrentUser,
    db: DbDep,
    sessionmaker: SessionmakerDep,
    types: Annotated[list[EntryType] | None, Query(max_length=9)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1, le=2_147_483_647)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    chapter_id: Annotated[int | None, Query(ge=1, le=2_147_483_647)] = None,
    chapter_no: Annotated[int | None, Query(ge=1, le=2_147_483_647)] = None,
    boundary: Boundary = "start",
):
    try:
        filters = s.CategoryEntryQuery(
            types=types,
            q=q,
            page=page,
            page_size=page_size,
            chapter_id=chapter_id,
            chapter_no=chapter_no,
            boundary=boundary,
        )
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    return await query.query_category_entries(
        sessionmaker, _actor(db, user), project_id, category_id=category_id, filters=filters
    )


@router.get("/setting-entries/{kind}/{entry_id}/tags", response_model=s.SettingTagsOut)
async def tags(
    project_id: Id, kind: EntryKind, entry_id: Id, user: CurrentUser, db: DbDep, sessionmaker: SessionmakerDep
):
    return await service.read_tags(sessionmaker, _actor(db, user), project_id, kind, entry_id)


@router.patch("/setting-entries/{kind}/{entry_id}/tags", response_model=s.SettingTagsOut)
async def update_tags(
    project_id: Id, kind: EntryKind, entry_id: Id, body: s.SettingTagsUpdate, user: CurrentUser, db: DbDep
):
    return await service.save_tags(db, user.id, project_id, kind, entry_id, body)


@router.get("/setting-entry-tags", response_model=s.SettingTagSuggestions)
async def suggestions(
    project_id: Id,
    user: CurrentUser,
    db: DbDep,
    sessionmaker: SessionmakerDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
):
    return await service.suggest_tags(sessionmaker, _actor(db, user), project_id, q=q, limit=limit)
