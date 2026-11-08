from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from fastapi.responses import StreamingResponse

from app.api.sse import sse_response
from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.schemas.rich_document_review import ReviewAccept, ReviewIdentity
from app.schemas.task import TaskClearResult, TaskKind, TaskOut, TaskOutput
from app.services import rich_document_review
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
    task = await svc.get_owned_task(db, user.id, task_id)
    statistics_key = task.statistics_key
    # 订阅可能持续很久：先释放请求会话
    await db.close()
    return sse_response(engine.subscribe(task_id, statistics_key=statistics_key))


@router.post("/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(
    task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep, body: ReviewIdentity | None = None,
) -> TaskOut:
    return await svc.cancel_task(db, engine, user.id, task_id, body)


@router.post("/{task_id}/retry", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def retry_task(task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await svc.retry_task(db, engine, user.id, task_id)


@router.post("/{task_id}/resume", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def resume_task(task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await svc.resume_task(db, engine, user.id, task_id)


@router.post("/{task_id}/finish", response_model=TaskOut)
async def finish_task(
    task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep, body: ReviewIdentity | None = None,
) -> TaskOut:
    return await svc.finish_paused_task(db, engine, user.id, task_id, body)


@router.get("/{task_id}/rich-proposals/{token}")
async def get_rich_proposal(
    task_id: int, token: UUID, task_key: UUID, run_key: UUID, user: CurrentUser, db: DbDep,
) -> dict:
    return await rich_document_review.get_review(db, user.id, task_id, str(task_key), str(run_key), str(token))


@router.post("/{task_id}/rich-proposals/{token}/accept")
async def accept_rich_proposal(
    task_id: int, token: UUID, body: ReviewAccept, user: CurrentUser, db: DbDep, engine: TaskEngineDep,
) -> dict:
    return await rich_document_review.accept_review(db, engine, user.id, task_id, str(token), body)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: int, user: CurrentUser, db: DbDep, task_key: str | None = None, run_key: str | None = None,
    token: str | None = None,
) -> Response:
    if any(value is not None for value in (task_key, run_key, token)):
        from pydantic import ValidationError

        try:
            expected = ReviewIdentity.model_validate({"task_key": task_key, "run_key": run_key, "token": token})
        except ValidationError:
            from app.core.errors import BadRequestError

            raise BadRequestError("提案身份无效") from None
    else:
        expected = None
    await svc.delete_task(db, user.id, task_id, expected)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
