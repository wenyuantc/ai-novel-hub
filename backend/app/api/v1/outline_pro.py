"""大纲进阶路由：概览、追加 / 排序、细纲与建章、续写 / 展开任务、快照、导入导出。

固定路径（/outlines/tree、/outlines/export 等）须先于通用 CRUD 的 /outlines/{item_id} 注册。
"""

from typing import Any

from fastapi import APIRouter, Response, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.models import Outline
from app.schemas.outline import (
    OutlineAppendRequest,
    OutlineContinueRequest,
    OutlineDetail,
    OutlineExpandRequest,
    OutlineImportPreview,
    OutlineImportPreviewRequest,
    OutlineImportRequest,
    OutlineImportResult,
    OutlineMoveRequest,
    PlanBuildResult,
    PlanOut,
    PlansSync,
    PlanUpdate,
    SnapshotCreate,
    SnapshotDetail,
    SnapshotOut,
    SnapshotRestoreResult,
)
from app.schemas.story import OutlineOut
from app.schemas.task import TaskOut
from app.services import (
    outline_service,
    outline_snapshot_service,
    outline_transfer_service,
    plan_service,
    task_service,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["outline-pro"])


@router.get("/outlines/tree", response_model=list[OutlineDetail])
async def outline_tree(project_id: int, user: CurrentUser, db: DbDep) -> list[OutlineDetail]:
    return await outline_service.outline_tree(db, user.id, project_id)


@router.get("/outlines/export")
async def export_outline(project_id: int, user: CurrentUser, db: DbDep) -> dict[str, Any]:
    return await outline_transfer_service.export_outline(db, user.id, project_id)


@router.post("/outlines/import/preview", response_model=OutlineImportPreview)
async def preview_import(
    project_id: int, body: OutlineImportPreviewRequest, user: CurrentUser, db: DbDep
) -> OutlineImportPreview:
    return await outline_transfer_service.preview_import(db, user.id, project_id, body.data)


@router.post("/outlines/import", response_model=OutlineImportResult)
async def import_outline(
    project_id: int, body: OutlineImportRequest, user: CurrentUser, db: DbDep
) -> OutlineImportResult:
    return await outline_transfer_service.import_outline(db, user.id, project_id, body)


@router.post("/outlines/append", response_model=list[OutlineOut], status_code=status.HTTP_201_CREATED)
async def append_outlines(project_id: int, body: OutlineAppendRequest, user: CurrentUser, db: DbDep) -> list[Outline]:
    return await outline_service.append_outlines(db, user.id, project_id, body)


@router.post("/outlines/continue", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def continue_outline(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: OutlineContinueRequest | None = None,
) -> TaskOut:
    params = {"project_id": project_id, **(body or OutlineContinueRequest()).model_dump(mode="json")}
    return await task_service.create_task(db, engine, user.id, "outline.continue", params)


@router.post("/outlines/{outline_id}/move", response_model=list[OutlineOut])
async def move_outline(
    project_id: int, outline_id: int, body: OutlineMoveRequest, user: CurrentUser, db: DbDep
) -> list[Outline]:
    return await outline_service.move_outline(db, user.id, project_id, outline_id, body)


@router.post("/outlines/{outline_id}/expand", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def expand_outline(
    project_id: int,
    outline_id: int,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: OutlineExpandRequest | None = None,
) -> TaskOut:
    params = {
        "project_id": project_id,
        "outline_id": outline_id,
        **(body or OutlineExpandRequest()).model_dump(mode="json"),
    }
    return await task_service.create_task(db, engine, user.id, "outline.expand", params)


@router.get("/outlines/{outline_id}/plans", response_model=list[PlanOut])
async def list_plans(project_id: int, outline_id: int, user: CurrentUser, db: DbDep) -> list[PlanOut]:
    return await plan_service.list_plans(db, user.id, project_id, outline_id)


@router.put("/outlines/{outline_id}/plans", response_model=list[PlanOut])
async def sync_plans(project_id: int, outline_id: int, body: PlansSync, user: CurrentUser, db: DbDep) -> list[PlanOut]:
    return await plan_service.sync_plans(db, user.id, project_id, outline_id, body)


@router.patch("/outlines/{outline_id}/plans/{plan_id}", response_model=PlanOut)
async def update_plan(
    project_id: int, outline_id: int, plan_id: int, body: PlanUpdate, user: CurrentUser, db: DbDep
) -> PlanOut:
    return await plan_service.update_plan(db, user.id, project_id, outline_id, plan_id, body)


@router.post("/outlines/{outline_id}/plans/build", response_model=PlanBuildResult)
async def build_chapters(project_id: int, outline_id: int, user: CurrentUser, db: DbDep) -> PlanBuildResult:
    return await plan_service.build_chapters(db, user.id, project_id, outline_id)


@router.get("/chapters/{chapter_id}/plan", response_model=PlanOut | None)
async def chapter_plan(project_id: int, chapter_id: int, user: CurrentUser, db: DbDep) -> PlanOut | None:
    return await plan_service.chapter_plan_out(db, user.id, project_id, chapter_id)


@router.get("/outline-snapshots", response_model=list[SnapshotOut])
async def list_snapshots(project_id: int, user: CurrentUser, db: DbDep) -> list[SnapshotOut]:
    return await outline_snapshot_service.list_snapshots(db, user.id, project_id)


@router.post("/outline-snapshots", response_model=SnapshotOut, status_code=status.HTTP_201_CREATED)
async def create_snapshot(
    project_id: int, user: CurrentUser, db: DbDep, body: SnapshotCreate | None = None
) -> SnapshotOut:
    return await outline_snapshot_service.create_manual(db, user.id, project_id, (body or SnapshotCreate()).note)


@router.get("/outline-snapshots/{snapshot_id}", response_model=SnapshotDetail)
async def get_snapshot(project_id: int, snapshot_id: int, user: CurrentUser, db: DbDep) -> SnapshotDetail:
    return await outline_snapshot_service.get_detail(db, user.id, project_id, snapshot_id)


@router.post("/outline-snapshots/{snapshot_id}/restore", response_model=SnapshotRestoreResult)
async def restore_snapshot(project_id: int, snapshot_id: int, user: CurrentUser, db: DbDep) -> SnapshotRestoreResult:
    return await outline_snapshot_service.restore_snapshot(db, user.id, project_id, snapshot_id)


@router.delete("/outline-snapshots/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_snapshot(project_id: int, snapshot_id: int, user: CurrentUser, db: DbDep) -> Response:
    await outline_snapshot_service.delete_snapshot(db, user.id, project_id, snapshot_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
