"""章内场景：固定后缀先注册，路由只负责 schema 与 service 调用。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, status

from app.core.deps import CurrentUser, DbDep
from app.schemas.progression import SettingContextOut
from app.schemas.rich_document import (
    MarkerDeleteOut,
    MarkerRelink,
    MarkerRelinkOut,
    MarkerTimelinePage,
    SceneDocumentOut,
    SceneDocumentSave,
    SceneSectionConvert,
)
from app.schemas.scene import (
    SceneCreate,
    SceneMerge,
    SceneMergePreview,
    SceneMergePreviewOut,
    SceneMutation,
    SceneOut,
    ScenePage,
    ScenePositionOut,
    SceneReorder,
    SceneSplit,
    SceneUpdate,
)
from app.services import (
    marker_lifecycle_service,
    marker_timeline_service,
    rich_document_conversion,
    rich_document_service,
)
from app.services import scene_service as svc

router = APIRouter(tags=["scenes"])
_BASE = "/projects/{project_id}/chapters/{chapter_id}/scenes"
PathId = Annotated[int, Path(ge=1, le=2_147_483_647)]


@router.get("/projects/{project_id}/markers", response_model=MarkerTimelinePage)
async def list_markers(
    project_id: PathId, user: CurrentUser, db: DbDep,
    offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
    snapshot: str | None = Query(default=None, min_length=64, max_length=64, pattern="^[a-f0-9]+$"),
) -> MarkerTimelinePage:
    return await marker_timeline_service.list_markers(
        db, user.id, project_id, offset=offset, limit=limit, expected_snapshot=snapshot,
    )


@router.post("/projects/{project_id}/markers/{scene_id}/{marker_key}/relink", response_model=MarkerRelinkOut)
async def relink_marker(
    project_id: PathId, scene_id: PathId, marker_key: UUID, body: MarkerRelink, user: CurrentUser, db: DbDep,
) -> MarkerRelinkOut:
    return await marker_lifecycle_service.relink(db, user.id, project_id, scene_id, marker_key, body)


@router.delete("/projects/{project_id}/markers/{scene_id}/{marker_key}", response_model=MarkerDeleteOut)
async def delete_stale_marker(
    project_id: PathId, scene_id: PathId, marker_key: UUID, user: CurrentUser, db: DbDep,
    expected_content_revision: int = Query(ge=0),
) -> MarkerDeleteOut:
    return await marker_lifecycle_service.delete_stale(
        db, user.id, project_id, scene_id, marker_key, expected_content_revision,
    )


@router.get("/projects/{project_id}/scenes", response_model=ScenePage)
async def project_scenes(
    project_id: PathId, user: CurrentUser, db: DbDep,
    offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
) -> ScenePage:
    return await svc.list_scenes(db, user.id, project_id, offset=offset, limit=limit)


@router.get(_BASE, response_model=ScenePage)
async def list_scenes(
    project_id: PathId, chapter_id: PathId, user: CurrentUser, db: DbDep,
    offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
) -> ScenePage:
    return await svc.list_scenes(db, user.id, project_id, chapter_id, offset=offset, limit=limit)


@router.post(_BASE, response_model=SceneOut, status_code=status.HTTP_201_CREATED)
async def create_scene(
    project_id: PathId, chapter_id: PathId, body: SceneCreate, user: CurrentUser, db: DbDep,
) -> SceneOut:
    return await svc.create_scene(db, user.id, project_id, chapter_id, body)


@router.get(f"{_BASE}/resolve-position", response_model=ScenePositionOut)
async def resolve_position(
    project_id: PathId, chapter_id: PathId, user: CurrentUser, db: DbDep,
    fingerprint: str = Query(min_length=64, max_length=64, pattern="^[a-f0-9]+$"),
    start: int = Query(ge=0), end: int = Query(ge=0),
) -> ScenePositionOut:
    return await svc.resolve_position(db, user.id, project_id, chapter_id, fingerprint, start, end)


@router.post(f"{_BASE}/reorder", response_model=SceneMutation)
async def reorder_scenes(
    project_id: PathId, chapter_id: PathId, body: SceneReorder, user: CurrentUser, db: DbDep,
) -> SceneMutation:
    return await svc.reorder_scenes(db, user.id, project_id, chapter_id, body)


@router.post(f"{_BASE}/merge-preview", response_model=SceneMergePreviewOut)
async def merge_preview(
    project_id: PathId, chapter_id: PathId, body: SceneMergePreview, user: CurrentUser, db: DbDep,
) -> SceneMergePreviewOut:
    return await svc.merge_preview(db, user.id, project_id, chapter_id, body)


@router.post(f"{_BASE}/merge", response_model=SceneMutation)
async def merge_scenes(
    project_id: PathId, chapter_id: PathId, body: SceneMerge, user: CurrentUser, db: DbDep,
) -> SceneMutation:
    return await svc.merge_scenes(db, user.id, project_id, chapter_id, body)


@router.get(f"{_BASE}/{{scene_id}}", response_model=SceneOut)
async def get_scene(project_id: PathId, chapter_id: PathId, scene_id: PathId, user: CurrentUser, db: DbDep) -> SceneOut:
    return await svc.get_scene(db, user.id, project_id, chapter_id, scene_id)


@router.patch(f"{_BASE}/{{scene_id}}", response_model=SceneOut)
async def update_scene(
    project_id: PathId, chapter_id: PathId, scene_id: PathId, body: SceneUpdate, user: CurrentUser, db: DbDep,
) -> SceneOut:
    return await svc.update_scene(db, user.id, project_id, chapter_id, scene_id, body)


@router.post(f"{_BASE}/{{scene_id}}/split", response_model=SceneMutation)
async def split_scene(
    project_id: PathId, chapter_id: PathId, scene_id: PathId, body: SceneSplit, user: CurrentUser, db: DbDep,
) -> SceneMutation:
    return await svc.split_scene(db, user.id, project_id, chapter_id, scene_id, body)


@router.delete(f"{_BASE}/{{scene_id}}", response_model=SceneMutation)
async def delete_scene(
    project_id: PathId, chapter_id: PathId, scene_id: PathId, user: CurrentUser, db: DbDep,
    expected_content_revision: int = Query(ge=0),
) -> SceneMutation:
    return await svc.delete_scene(db, user.id, project_id, chapter_id, scene_id, expected_content_revision)


@router.get(f"{_BASE}/{{scene_id}}/setting-context", response_model=SettingContextOut)
async def setting_context(
    project_id: PathId, chapter_id: PathId, scene_id: PathId, user: CurrentUser, db: DbDep,
) -> SettingContextOut:
    from app.services.planning_context_service import scene_setting_context

    return await scene_setting_context(db, user.id, project_id, chapter_id, scene_id)


@router.get(f"{_BASE}/{{scene_id}}/document", response_model=SceneDocumentOut)
async def get_document(
    project_id: PathId, chapter_id: PathId, scene_id: PathId, user: CurrentUser, db: DbDep,
) -> SceneDocumentOut:
    return await rich_document_service.get_document(db, user.id, project_id, chapter_id, scene_id)


@router.put(f"{_BASE}/{{scene_id}}/document", response_model=SceneDocumentOut)
async def save_document(
    project_id: PathId, chapter_id: PathId, scene_id: PathId, body: SceneDocumentSave, user: CurrentUser, db: DbDep,
) -> SceneDocumentOut:
    return await rich_document_service.save_document(db, user.id, project_id, chapter_id, scene_id, body)


@router.post(f"{_BASE}/{{scene_id}}/document/sections/{{section_key}}/convert", response_model=SceneDocumentOut)
async def convert_section(
    project_id: PathId, chapter_id: PathId, scene_id: PathId, section_key: UUID, body: SceneSectionConvert,
    user: CurrentUser, db: DbDep,
) -> SceneDocumentOut:
    return await rich_document_conversion.convert_section(
        db, user.id, project_id, chapter_id, scene_id, section_key, body.expected_content_revision,
    )
