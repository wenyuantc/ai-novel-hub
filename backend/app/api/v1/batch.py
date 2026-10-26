"""批量顺序生成：预览与发起（chapter.batch 任务，返回 202）。继续 / 结束 / 取消 / 重试走 /tasks/{id}/...。"""

from fastapi import APIRouter, status

from app.core.deps import CurrentUser, DbDep, SettingsDep, TaskEngineDep
from app.schemas.batch import BatchGenerateRequest, BatchPreview
from app.schemas.task import TaskOut
from app.services import batch_service, task_service

router = APIRouter(prefix="/projects/{project_id}", tags=["batch-generate"])


@router.post("/batch-generate/preview", response_model=BatchPreview)
async def preview_batch(
    project_id: int, body: BatchGenerateRequest, user: CurrentUser, db: DbDep, settings: SettingsDep
) -> BatchPreview:
    return await batch_service.preview(db, settings, user.id, project_id, body)


@router.post("/batch-generate", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def start_batch(
    project_id: int, body: BatchGenerateRequest, user: CurrentUser, db: DbDep, engine: TaskEngineDep
) -> TaskOut:
    params = {"project_id": project_id, **body.model_dump(mode="json")}
    return await task_service.create_task(db, engine, user.id, batch_service.KIND, params)
