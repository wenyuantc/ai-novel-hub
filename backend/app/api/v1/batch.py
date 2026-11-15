"""批量顺序生成：预览与发起（chapter.batch 任务，返回 202）。继续 / 结束 / 取消 / 重试走 /tasks/{id}/...。"""

from fastapi import APIRouter, status

from app.core.deps import CurrentUser, DbDep, SettingsDep, TaskEngineDep
from app.schemas.batch import BatchGenerateRequest, BatchPreview
from app.schemas.task import TaskOut
from app.services import batch_service

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
    # 锁外预检 + 中央受控受理：作者与 accepted editor 共用（E4-g2）。
    return await batch_service.create_batch_task(db, engine, user.id, project_id, body)
