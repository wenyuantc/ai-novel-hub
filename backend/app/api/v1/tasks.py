import json
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from fastapi.responses import StreamingResponse

from app.api.sse import sse_response
from app.core.deps import CurrentUser, DbDep, SessionmakerDep, TaskEngineDep
from app.core.errors import ConflictError, NotFoundError
from app.schemas.rich_document_review import ReviewAccept, ReviewIdentity
from app.schemas.task import TaskClearResult, TaskKind, TaskOut, TaskOutput
from app.services import authorization_service, project_access, project_ai_config, rich_document_review
from app.services import task_service as svc
from app.services.task_engine import peer_stream
from app.services.task_engine.admission import capture_retry_source, prepare_task_admission
from app.services.task_engine.execution_identity import _TASK_TOKEN_KEY, _TOKEN_KEYS
from app.services.task_engine.registry import get_handler
from app.services.task_engine.stream_evidence import stream_kind_open
from app.services.task_engine.stream_lease import _capture_stream_binding, task_sse_response

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
async def get_task(task_id: int, task_key: str, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await svc.get_task(db, engine, user.id, task_id, expected_task_key=task_key)


@router.get("/{task_id}/output", response_model=TaskOutput)
async def get_output(task_id: int, task_key: str, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOutput:
    return TaskOutput(text=await svc.get_output(db, engine, user.id, task_id, expected_task_key=task_key))


@router.get("/{task_id}/stream", response_class=StreamingResponse)
async def stream_task(
    task_id: int, task_key: str, user: CurrentUser, db: DbDep, engine: TaskEngineDep, sessionmaker: SessionmakerDep,
) -> StreamingResponse:
    # 由这个 HTTP 请求冻结任务轮次和账号实例，连接换进程时仍使用同一绑定。
    authenticated = authorization_service.actor_snapshot(db, user.id)
    if not isinstance(authenticated, authorization_service.AuthenticatedActor):
        raise NotFoundError("任务不存在")
    actor = authorization_service.AuthenticatedActor(authenticated.id, authenticated.token_version, user.created_at)
    task = await svc.get_owned_task(db, user.id, task_id, expected_task_key=task_key)
    binding = await _capture_stream_binding(db, task, actor)
    if binding.member_grant is not None and not stream_kind_open(task.kind):
        raise NotFoundError("任务不存在")
    await db.close()
    events = peer_stream.subscribe(engine, binding)
    if binding.member_grant is not None:
        return task_sse_response(events, sessionmaker=sessionmaker, binding=binding)
    return sse_response(events)


@router.post("/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(
    task_id: int, task_key: str, user: CurrentUser, db: DbDep, engine: TaskEngineDep,
    body: ReviewIdentity | None = None,
) -> TaskOut:
    if body is not None and body.task_key != task_key:
        raise NotFoundError("任务不存在")
    return await svc.cancel_task(db, engine, user.id, task_id, body, expected_task_key=task_key)


@router.post("/{task_id}/retry", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def retry_task(task_id: int, task_key: str, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    actor_id = user.id
    original = await svc.get_owned_task(db, actor_id, task_id, expected_task_key=task_key)
    handler = get_handler(original.kind)
    if (getattr(handler, "admission_preflight_aware", False) is not True
            or original.status not in svc.RETRYABLE_STATUSES):
        return await svc.retry_task(db, engine, actor_id, task_id, expected_task_key=task_key)

    # 只冻结首次本人读取的原任务，绝不由当前整数 ID 或默认模型补造新身份。
    actor = authorization_service.actor_snapshot(db, actor_id)
    if type(actor) is not authorization_service.AuthenticatedActor or actor.id != actor_id:
        raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    source = capture_retry_source(original, expected_task_key=task_key)
    payload = json.loads(source.input_json)
    token = payload.get(_TASK_TOKEN_KEY)
    alias = _TOKEN_KEYS.get(source.kind)
    if (type(token) is not int or token != actor.token_version
            or (alias is not None and (type(payload.get(alias)) is not int or payload[alias] != token))
            or any(key in payload and key != alias for key in set(_TOKEN_KEYS.values()))):
        raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    request = payload.get("request")
    if type(source.project_id) is not int or type(request) is not dict:
        raise ConflictError("原任务绑定无法验证，请重新创建任务", extra={"code": "task_admission_changed"})
    project = await project_access.require_project(db, actor_id, source.project_id, "content.read")
    owner_id = project.user_id
    if owner_id != actor_id and getattr(handler, "collaboration_execution_ready", False) is not True:
        raise ConflictError("协作任务重试暂不可用", extra={"code": "task_admission_changed"})
    model = project_ai_config.deserialize_project_model(
        payload.get("_project_model"), actor_id=actor_id, project_id=source.project_id, project_owner_id=owner_id,
    )
    if model is None or model.model_type != "text":
        raise ConflictError("原任务模型绑定无法验证，请重新创建任务", extra={"code": "task_admission_changed"})
    # 原请求只读事务也在外部 DNS/provider 预检前结束；锁内由 service 再验证原来源。
    await db.rollback()
    ctx = engine.ctx
    # 原任务专用预检绑定已注册的 RetrySource；锁内再消费同事务来源证明。
    preflight = await project_ai_config.preflight_original_project_model(
        ctx.sessionmaker, actor, source, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
        for_task_admission=True,
    )
    if preflight.binding != model:
        raise ConflictError("原任务模型绑定已变化，请重新创建任务", extra={"code": "task_admission_changed"})
    admission = await prepare_task_admission(ctx, actor, source.kind, request, model_preflight=preflight,
                                             retry_source=source)
    return await svc.retry_task(db, engine, actor_id, task_id, expected_task_key=task_key, admission=admission)


@router.post("/{task_id}/resume", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def resume_task(task_id: int, task_key: str, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await svc.resume_task(db, engine, user.id, task_id, expected_task_key=task_key)


@router.post("/{task_id}/finish", response_model=TaskOut)
async def finish_task(
    task_id: int, task_key: str, user: CurrentUser, db: DbDep, engine: TaskEngineDep,
    body: ReviewIdentity | None = None,
) -> TaskOut:
    if body is not None and body.task_key != task_key:
        raise NotFoundError("任务不存在")
    return await svc.finish_paused_task(db, engine, user.id, task_id, body, expected_task_key=task_key)


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
    task_id: int, task_key: str, user: CurrentUser, db: DbDep, run_key: str | None = None, token: str | None = None,
) -> Response:
    if run_key is not None or token is not None:
        from pydantic import ValidationError

        # 身份无效与不存在统一隐藏；提案附加字段无效仍返回普通参数错误。
        try:
            if str(UUID(task_key)) != task_key:
                raise ValueError
        except ValueError:
            raise NotFoundError("任务不存在") from None
        try:
            expected = ReviewIdentity.model_validate({"task_key": task_key, "run_key": run_key, "token": token})
        except ValidationError:
            from app.core.errors import BadRequestError

            raise BadRequestError("提案身份无效") from None
    else:
        expected = None
    await svc.delete_task(db, user.id, task_id, expected, expected_task_key=task_key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
