"""共享字段与跨类型条目目录；路由仅校验传输参数。"""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.core.deps import CurrentUser, DbDep
from app.schemas import setting_fields as s
from app.schemas.progression import Boundary, ProgressionMutationOut
from app.schemas.setting_ai_visibility import EntryAiSettingsOut, EntryAiSettingsUpdate, LegacyFieldAiVisibilityUpdate
from app.services import setting_ai_visibility as visibility
from app.services import setting_catalog_service as catalog
from app.services import setting_field_service as svc

router = APIRouter(prefix="/projects/{project_id}", tags=["setting-fields"])
Id = Annotated[int, Path(ge=1, le=2_147_483_647)]
Revision = Annotated[int, Query(ge=0, le=2_147_483_647)]


@router.get("/setting-field-definitions", response_model=s.DefinitionPage)
async def definitions(
    project_id: Id,
    user: CurrentUser,
    db: DbDep,
    types: Annotated[list[s.EntryType] | None, Query(max_length=9)] = None,
    include_archived: bool = True,
):
    return await svc.list_definitions(db, user.id, project_id, types=types, include_archived=include_archived)


@router.post("/setting-field-definitions", response_model=s.DefinitionMutation, status_code=201)
async def create_definition(project_id: Id, body: s.DefinitionCreate, user: CurrentUser, db: DbDep):
    return await svc.save_definition(db, user.id, project_id, body)


@router.get("/setting-field-definitions/{definition_id}", response_model=s.DefinitionMutation)
async def definition(project_id: Id, definition_id: Id, user: CurrentUser, db: DbDep):
    return await svc.read_definition(db, user.id, project_id, definition_id)


@router.patch("/setting-field-definitions/{definition_id}", response_model=s.DefinitionMutation)
async def update_definition(project_id: Id, definition_id: Id, body: s.DefinitionUpdate, user: CurrentUser, db: DbDep):
    return await svc.save_definition(db, user.id, project_id, body, definition_id)


@router.delete("/setting-field-definitions/{definition_id}", response_model=s.FieldRevision)
async def delete_definition(
    project_id: Id,
    definition_id: Id,
    expected_fields_revision: Revision,
    user: CurrentUser,
    db: DbDep,
):
    return await svc.delete_definition(db, user.id, project_id, definition_id, expected_fields_revision)


@router.post("/setting-field-definitions/{definition_id}/options", response_model=s.OptionMutation, status_code=201)
async def create_option(project_id: Id, definition_id: Id, body: s.OptionCreate, user: CurrentUser, db: DbDep):
    return await svc.save_option(db, user.id, project_id, definition_id, body)


@router.patch("/setting-field-definitions/{definition_id}/options/{option_id}", response_model=s.OptionMutation)
async def update_option(
    project_id: Id,
    definition_id: Id,
    option_id: Id,
    body: s.OptionUpdate,
    user: CurrentUser,
    db: DbDep,
):
    return await svc.save_option(db, user.id, project_id, definition_id, body, option_id)


@router.delete("/setting-field-definitions/{definition_id}/options/{option_id}", response_model=s.FieldRevision)
async def delete_option(
    project_id: Id,
    definition_id: Id,
    option_id: Id,
    expected_fields_revision: Revision,
    user: CurrentUser,
    db: DbDep,
):
    return await svc.delete_option(db, user.id, project_id, definition_id, option_id, expected_fields_revision)


@router.get("/setting-entries", response_model=s.SettingEntryPage)
async def entries(
    project_id: Id,
    user: CurrentUser,
    db: DbDep,
    types: Annotated[list[s.EntryType] | None, Query(max_length=9)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return await catalog.list_entries(db, user.id, project_id, types=types, q=q, page=page, page_size=page_size)


@router.get("/setting-entries/{kind}/{entry_id}/ai-settings", response_model=EntryAiSettingsOut)
async def ai_settings(project_id: Id, kind: s.EntryKind, entry_id: Id, user: CurrentUser, db: DbDep):
    return await visibility.read_settings(db, user.id, project_id, kind, entry_id)


@router.patch("/setting-entries/{kind}/{entry_id}/ai-settings", response_model=EntryAiSettingsOut)
async def update_ai_settings(
    project_id: Id, kind: s.EntryKind, entry_id: Id, body: EntryAiSettingsUpdate, user: CurrentUser, db: DbDep,
):
    return await visibility.write_settings(db, user.id, project_id, kind, entry_id, body)


@router.get("/setting-entries/{kind}/{entry_id}/fields", response_model=s.EntryFieldsOut)
async def fields(
    project_id: Id,
    kind: s.EntryKind,
    entry_id: Id,
    user: CurrentUser,
    db: DbDep,
    chapter_id: Annotated[int | None, Query(ge=1, le=2_147_483_647)] = None,
    chapter_no: Annotated[int | None, Query(ge=1, le=2_147_483_647)] = None,
    scene_id: Annotated[int | None, Query(ge=1, le=2_147_483_647)] = None,
    boundary: Boundary = "start",
):
    return await svc.read_entry_fields(
        db, user.id, project_id, kind, entry_id, chapter_id=chapter_id, chapter_no=chapter_no,
        scene_id=scene_id, boundary=boundary
    )


@router.patch("/setting-entries/{kind}/{entry_id}/fields/{definition_id}/baseline", response_model=s.EntryFieldsOut)
async def baseline(
    project_id: Id,
    kind: s.EntryKind,
    entry_id: Id,
    definition_id: Id,
    body: s.FieldValueWrite,
    user: CurrentUser,
    db: DbDep,
):
    return await svc.write_baseline(db, user.id, project_id, kind, entry_id, definition_id, body)


@router.post(
    "/setting-entries/{kind}/{entry_id}/fields/{definition_id}/progressions",
    response_model=ProgressionMutationOut,
    status_code=201,
)
async def progression(
    project_id: Id,
    kind: s.EntryKind,
    entry_id: Id,
    definition_id: Id,
    body: s.FieldProgressionCreate,
    user: CurrentUser,
    db: DbDep,
):
    return await svc.create_field_progression(db, user.id, project_id, kind, entry_id, definition_id, body)


@router.post("/setting-entries/{kind}/{entry_id}/legacy-fields/{field_id}/binding", response_model=s.EntryFieldsOut)
async def binding(
    project_id: Id,
    kind: s.EntryKind,
    entry_id: Id,
    field_id: Id,
    body: s.LegacyBinding,
    user: CurrentUser,
    db: DbDep,
):
    return await svc.bind_legacy(db, user.id, project_id, kind, entry_id, field_id, body)


@router.patch(
    "/setting-entries/{kind}/{entry_id}/legacy-fields/{field_id}/ai-visibility", response_model=s.EntryFieldsOut
)
async def legacy_ai_visibility(
    project_id: Id,
    kind: s.EntryKind,
    entry_id: Id,
    field_id: Id,
    body: LegacyFieldAiVisibilityUpdate,
    user: CurrentUser,
    db: DbDep,
):
    return await svc.write_legacy_visibility(db, user.id, project_id, kind, entry_id, field_id, body)
