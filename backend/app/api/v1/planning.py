"""规划矩阵、支线与交汇；路由只做传输校验，所有读写归服务所有。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query
from pydantic import TypeAdapter, ValidationError

from app.core.deps import CurrentUser, DbDep
from app.core.errors import AppError
from app.schemas import planning as s
from app.services import planning_service as svc
from app.services.project_service import get_manageable_project

router = APIRouter(tags=["planning"])
BASE = "/projects/{project_id}/planning"
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
ExpectedRevision = Annotated[int, Query(ge=0, le=2_147_483_646)]
SourceId = Annotated[int, Path(gt=0, le=2_147_483_647)]


class InvalidPlanningQuery(AppError):
    status_code = 422


def _decode_list(raw, annotation, maximum):
    if raw is None:
        return None
    try:
        values = TypeAdapter(annotation).validate_json(raw)
    except ValidationError as exc:
        raise InvalidPlanningQuery("规划筛选参数格式不正确") from exc
    if len(values) > maximum:
        raise InvalidPlanningQuery(f"筛选条目超过上限 {maximum}")
    return values


@router.get(BASE + "/matrix", response_model=s.PlanningMatrix)
async def matrix(
    project_id: SourceId,
    user: CurrentUser,
    db: DbDep,
    page: Page = 1,
    page_size: PageSize = 50,
    column_page: Page = 1,
    column_mode: Literal["settings", "subplots", "custom"] = "settings",
    column_refs: Annotated[str | None, Query(max_length=20000)] = None,
    source_refs: Annotated[str | None, Query(max_length=20000)] = None,
    source_kind: Literal["chapter", "plan", "scene"] | None = None,
    granularity: Literal["chapter", "scene"] = "chapter",
    volume_no: Annotated[int | None, Query(ge=1)] = None,
    tag_id: Annotated[int | None, Query(gt=0, le=2_147_483_647)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    entity_kind: s.ReferenceKind | None = None,
    entry_q: Annotated[str | None, Query(max_length=200)] = None,
):
    return await svc.matrix(
        db,
        user.id,
        project_id,
        page=page,
        page_size=page_size,
        column_page=column_page,
        column_mode=column_mode,
        column_refs=_decode_list(column_refs, list[s.MatrixColumnRef], 30),
        source_refs=_decode_list(source_refs, list[s.PlanningSource], 100),
        source_kind=source_kind,
        granularity=granularity,
        volume_no=volume_no,
        tag_id=tag_id,
        q=q,
        entity_kind=entity_kind,
        entry_q=entry_q,
    )


@router.get(BASE + "/entries", response_model=s.PlanningEntryPage)
async def entries(
    project_id: SourceId,
    user: CurrentUser,
    db: DbDep,
    kind: s.ReferenceKind | None = None,
    category: Annotated[str | None, Query(max_length=16)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Page = 1,
    page_size: PageSize = 50,
):
    return await svc.list_entries(
        db, user.id, project_id, kind=kind, category=category, q=q, page=page, page_size=page_size
    )


@router.patch(BASE + "/units/batch", response_model=s.PlanningMutation)
async def batch(project_id: SourceId, body: s.PlanningBatch, user: CurrentUser, db: DbDep):
    return await svc.batch_update(db, user.id, project_id, body)


@router.post(BASE + "/units/merge", response_model=s.PlanningMutation)
async def merge(project_id: SourceId, body: s.PlanningMerge, user: CurrentUser, db: DbDep):
    return await svc.merge_units(db, user.id, project_id, body)


@router.post(BASE + "/units/{unit_id}/binding", response_model=s.PlanningMutation)
async def binding(project_id: SourceId, unit_id: SourceId, body: s.PlanningBinding, user: CurrentUser, db: DbDep):
    return await svc.bind_unit(db, user.id, project_id, unit_id, body)


@router.get(BASE + "/units/context-preview", response_model=s.PlanningContextPreview)
async def context_preview(
    project_id: SourceId,
    user: CurrentUser,
    db: DbDep,
    source_kind: s.SourceKind,
    source_id: Annotated[int, Query(gt=0, le=2_147_483_647)],
    scope: Literal["chapter", "selection"] = "chapter",
    budget: Annotated[int, Query(ge=256, le=20000)] = 4000,
):
    from app.services.planning_context_service import context_preview as preview

    project = await get_manageable_project(db, user.id, project_id)
    return await preview(db, project, source_kind, source_id, scope=scope, budget=budget)


@router.get(BASE + "/detached", response_model=s.PlanningDetached)
async def detached(
    project_id: SourceId,
    user: CurrentUser,
    db: DbDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Page = 1,
    page_size: PageSize = 50,
):
    return await svc.list_detached(db, user.id, project_id, q=q, page=page, page_size=page_size)


@router.get(BASE + "/tags", response_model=s.PlanningTagPage)
async def tags(
    project_id: SourceId,
    user: CurrentUser,
    db: DbDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Page = 1,
    page_size: PageSize = 50,
):
    return await svc.list_tags(db, user.id, project_id, q=q, page=page, page_size=page_size)


@router.post(BASE + "/tags", response_model=s.PlanningTagMutation, status_code=201)
async def create_tag(project_id: SourceId, body: s.PlanningTagCreate, user: CurrentUser, db: DbDep):
    return await svc.save_tag(db, user.id, project_id, body)


@router.patch(BASE + "/tags/{tag_id}", response_model=s.PlanningTagMutation)
async def update_tag(project_id: SourceId, tag_id: SourceId, body: s.PlanningTagUpdate, user: CurrentUser, db: DbDep):
    return await svc.save_tag(db, user.id, project_id, body, tag_id)


@router.delete(BASE + "/tags/{tag_id}", response_model=s.PlanningRevision)
async def delete_tag(
    project_id: SourceId, tag_id: SourceId, user: CurrentUser, db: DbDep, expected_revision: ExpectedRevision
):
    return await svc.delete_tag(db, user.id, project_id, tag_id, expected_revision)


@router.get(BASE + "/subplots", response_model=s.SubplotPage)
async def subplots(
    project_id: SourceId,
    user: CurrentUser,
    db: DbDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Page = 1,
    page_size: PageSize = 50,
):
    return await svc.list_subplots(db, user.id, project_id, q=q, page=page, page_size=page_size)


@router.post(BASE + "/subplots", response_model=s.SubplotMutation, status_code=201)
async def create_subplot(project_id: SourceId, body: s.SubplotCreate, user: CurrentUser, db: DbDep):
    return await svc.save_subplot(db, user.id, project_id, body)


@router.patch(BASE + "/subplots/{subplot_id}", response_model=s.SubplotMutation)
async def update_subplot(
    project_id: SourceId, subplot_id: SourceId, body: s.SubplotUpdate, user: CurrentUser, db: DbDep
):
    return await svc.save_subplot(db, user.id, project_id, body, subplot_id)


@router.delete(BASE + "/subplots/{subplot_id}", response_model=s.PlanningRevision)
async def delete_subplot(
    project_id: SourceId, subplot_id: SourceId, user: CurrentUser, db: DbDep, expected_revision: ExpectedRevision
):
    return await svc.delete_subplot(db, user.id, project_id, subplot_id, expected_revision)


@router.put(BASE + "/subplots/{subplot_id}/nodes/{source_kind}/{source_id}", response_model=s.SubplotNodeMutation)
async def put_node(
    project_id: SourceId,
    subplot_id: SourceId,
    source_kind: s.SourceKind,
    source_id: SourceId,
    body: s.SubplotNodePut,
    user: CurrentUser,
    db: DbDep,
):
    return await svc.put_node(
        db, user.id, project_id, subplot_id, s.PlanningSource(kind=source_kind, id=source_id), body
    )


@router.delete(BASE + "/subplots/{subplot_id}/nodes/{source_kind}/{source_id}", response_model=s.PlanningRevision)
async def delete_node(
    project_id: SourceId,
    subplot_id: SourceId,
    source_kind: s.SourceKind,
    source_id: SourceId,
    user: CurrentUser,
    db: DbDep,
    expected_revision: ExpectedRevision,
):
    return await svc.delete_node(
        db, user.id, project_id, subplot_id, s.PlanningSource(kind=source_kind, id=source_id), expected_revision
    )


@router.get(BASE + "/crossings", response_model=s.SubplotCrossingPage)
async def crossings(
    project_id: SourceId,
    user: CurrentUser,
    db: DbDep,
    source_kind: s.SourceKind | None = None,
    source_id: Annotated[int | None, Query(gt=0, le=2_147_483_647)] = None,
    subplot_id: Annotated[int | None, Query(gt=0, le=2_147_483_647)] = None,
    page: Page = 1,
    page_size: PageSize = 50,
):
    if (source_kind is None) != (source_id is None):
        raise InvalidPlanningQuery("来源类型与来源 ID 必须一起指定")
    source = s.PlanningSource(kind=source_kind, id=source_id) if source_kind is not None else None
    return await svc.list_crossings(
        db, user.id, project_id, source=source, subplot_id=subplot_id, page=page, page_size=page_size
    )


@router.post(BASE + "/crossings", response_model=s.SubplotCrossingMutation, status_code=201)
async def create_crossing(project_id: SourceId, body: s.SubplotCrossingCreate, user: CurrentUser, db: DbDep):
    return await svc.save_crossing(db, user.id, project_id, body)


@router.patch(BASE + "/crossings/{crossing_id}", response_model=s.SubplotCrossingMutation)
async def update_crossing(
    project_id: SourceId, crossing_id: SourceId, body: s.SubplotCrossingUpdate, user: CurrentUser, db: DbDep
):
    return await svc.save_crossing(db, user.id, project_id, body, crossing_id)


@router.delete(BASE + "/crossings/{crossing_id}", response_model=s.PlanningRevision)
async def delete_crossing(
    project_id: SourceId, crossing_id: SourceId, user: CurrentUser, db: DbDep, expected_revision: ExpectedRevision
):
    return await svc.delete_crossing(db, user.id, project_id, crossing_id, expected_revision)


@router.get(BASE + "/weave", response_model=s.PlanningWeave)
async def weave(
    project_id: SourceId,
    user: CurrentUser,
    db: DbDep,
    axis: Literal["chapter", "plan", "scene"] = "chapter",
    page: Page = 1,
    page_size: PageSize = 50,
    subplot_page: Page = 1,
    subplot_page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    subplot_ids: Annotated[list[int] | None, Query(max_length=30)] = None,
    gap_threshold: Annotated[int, Query(ge=1, le=100)] = 5,
):
    return await svc.weave(
        db,
        user.id,
        project_id,
        axis=axis,
        page=page,
        page_size=page_size,
        subplot_page=subplot_page,
        subplot_page_size=subplot_page_size,
        subplot_ids=subplot_ids,
        gap_threshold=gap_threshold,
    )
