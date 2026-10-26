from fastapi import APIRouter, Query, Response, status
from fastapi.responses import StreamingResponse

from app.api.sse import sse_response
from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.schemas.task import TaskClearResult, TaskKind, TaskOut, TaskOutput
from app.services import task_service as svc

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    project_id: int | None = None,
    chapter_id: int | None = None,
    kind: TaskKind | None = None,
    active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[TaskOut]:
    return await svc.list_tasks(
        db, engine, user.id, project_id=project_id, chapter_id=chapter_id, kind=kind, active=active, limit=limit
    )


@router.delete("", response_model=TaskClearResult)
async def clear_finished(
    user: CurrentUser, db: DbDep, finished: bool = True, project_id: int | None = None
) -> TaskClearResult:
    # 目前只支持清理已结束的任务；finished=false 不做任何事
    deleted = await svc.clear_finished(db, user.id, project_id=project_id) if finished else 0
    return TaskClearResult(deleted=deleted)


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await svc.get_task(db, engine, user.id, task_id)


@router.get("/{task_id}/output", response_model=TaskOutput)
async def get_output(task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOutput:
    return TaskOutput(text=await svc.get_output(db, engine, user.id, task_id))


@router.get("/{task_id}/stream", response_class=StreamingResponse)
async def stream_task(task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> StreamingResponse:
    await svc.get_owned_task(db, user.id, task_id)
    # 订阅可能持续很久：先释放请求会话
    await db.close()
    return sse_response(engine.subscribe(task_id))


@router.post("/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await svc.cancel_task(db, engine, user.id, task_id)


@router.post("/{task_id}/retry", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def retry_task(task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await svc.retry_task(db, engine, user.id, task_id)


@router.post("/{task_id}/resume", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def resume_task(task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await svc.resume_task(db, engine, user.id, task_id)


@router.post("/{task_id}/finish", response_model=TaskOut)
async def finish_task(task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await svc.finish_paused_task(db, engine, user.id, task_id)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_task(db, user.id, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
