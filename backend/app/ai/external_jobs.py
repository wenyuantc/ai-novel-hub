"""持久化视频任务。每次真实 HTTP 发送复验执行身份、模型与素材，厂商任务号只领取一次。"""

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.endpoint_guard import AIEndpointError, ensure_endpoint_allowed
from app.ai.providers.base import AIProviderError
from app.ai.providers.dispatch_hooks import DispatchHooks, use_dispatch_hooks
from app.ai.providers.video import VideoTransientError, build_video_provider
from app.ai.video_job_inputs import VideoInputs
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.media import service as media
from app.models import AIConfig, Project, Task
from app.models.ai_external_jobs import AiExternalJob
from app.models.media import MediaObject
from app.plugins import state as plugin_state
from app.services import ai_config_service, ai_media_billing, project_ai_config
from app.services.ai_usage_storage import UsageCallIdentity, as_utc
from app.services.task_engine.execution_identity import TaskExecutionIdentity, matches_running, require_write_execution
from app.services.task_engine.registry import TaskContext

_POLL_INTERVAL = 10
_MAX_DOWNLOAD = 1024**3
_TERMINAL = {"cancelled", "expired", "failed", "succeeded"}
_UNKNOWN = "厂商任务号尚未确认，请在厂商控制台核对后再发起生成"


def _identity(job, execution=None):
    return UsageCallIdentity(actor_id=job.actor_id, payer_id=job.payer_id, project_id=job.project_id,
                             task_id=execution.task_id if execution else None,
                             task_key=execution.task_key if execution else None,
                             chapter_id=execution.chapter_id if execution else None,
                             provider=job.provider, model=job.model, model_type="video",
                             kind=job.request["source_kind"], config_instance_key=job.config_instance_key,
                             author_paid=job.payer_id == job.request["owner_id"] and job.project_id is not None)


async def create_job(db: AsyncSession, ctx: TaskContext, *, actor_id: int, config: AIConfig, request: dict,
                     project_id: int | None = None, owner_ref: str = "ai.video_probe", project_binding=None,
                     plugin_id: str | None = None, source_kind: str = "ai.video_probe") -> str:
    """与任务受理同事务；私有配置只能归发起人，作品配置必须携带服务端冻结绑定。"""
    args = VideoInputs.parse(request)
    if (config is None or config.model_type != "video" or not config.instance_key
            or (project_id is None) != (project_binding is None)):
        raise BadRequestError("需要有效的视频模型配置")
    owner = actor_id
    binding = None
    if project_binding is not None:
        if (project_binding.project_id != project_id or project_binding.actor_id != actor_id
                or project_binding.model_type != "video" or project_binding.config_id != config.id
                or project_binding.config_instance_key != config.instance_key
                or project_binding.config_owner_id != config.user_id):
            raise BadRequestError("作品视频模型授权已变化")
        binding = project_ai_config.serialize_project_model(project_binding)
        await project_ai_config.revalidate_project_model(db, project_binding, cipher=ctx.cipher,
                                                         allow_private=ctx.settings.allow_private_ai_endpoints)
        owner = project_binding.project_owner_id
    elif config.user_id != actor_id:
        raise NotFoundError("模型配置不存在")
    if not ctx.settings.allow_private_ai_endpoints:
        try:
            await ensure_endpoint_allowed(config.base_url)
        except AIEndpointError as exc:
            detail = "作者共享视频模型接口不可用，请联系作者" if config.user_id != actor_id else str(exc)
            raise BadRequestError(detail) from None
    async for _ in args.references(db, actor_id, project_id, config.provider):
        pass
    snapshot = dict(inputs=args.model_dump(), source_kind=source_kind, plugin_id=plugin_id, owner_id=owner,
                    fingerprint=project_ai_config._fingerprint(config), project_binding=binding)  # noqa: SLF001
    job = AiExternalJob(actor_id=actor_id, project_id=project_id, payer_id=config.user_id, config_id=config.id,
                        config_instance_key=config.instance_key, provider=config.provider, model=config.model,
                        kind="video", request=snapshot, owner_ref=owner_ref, state="created")
    snapshot["price"] = await ai_media_billing.check_video_budget(
        db, _identity(job), seconds=args.duration_seconds, now=datetime.now(UTC),
    )
    db.add(job)
    await db.flush()
    return job.id


def _provider(ctx, cfg):
    return build_video_provider(cfg.provider, ctx.cipher.decrypt(cfg.api_key_enc), cfg.base_url,
                                ctx.settings.ai_request_timeout, allow_private=ctx.settings.allow_private_ai_endpoints)


async def _config(db, ctx, job):
    # 配置行锁必须在账户的配置选择门禁之后获取，和修改配置的锁序一致。
    await ai_config_service.lock_config_owner(db, job.payer_id, write=False)
    if job.request["project_binding"] is not None:
        owner = await db.scalar(select(Project.user_id).where(Project.id == job.project_id))
        binding = project_ai_config.deserialize_project_model(
            job.request["project_binding"], actor_id=job.actor_id, project_id=job.project_id, project_owner_id=owner,
        )
        await project_ai_config.revalidate_project_model(db, binding, cipher=ctx.cipher,
                                                         allow_private=ctx.settings.allow_private_ai_endpoints)
    elif job.project_id is not None or job.actor_id != job.payer_id:
        raise BadRequestError("视频模型归属已变化")
    cfg = await db.get(AIConfig, job.config_id, populate_existing=True)
    if (cfg is None or cfg.model_type != "video" or cfg.instance_key != job.config_instance_key
            or cfg.user_id != job.payer_id or project_ai_config._fingerprint(cfg) != job.request["fingerprint"]):
        raise BadRequestError("视频模型配置已变化，请重新选择")
    return cfg


async def _job(db, execution, job_id):
    try:
        await require_write_execution(db, execution)
    except ConflictError:
        # 管理员取消意图也属于执行门禁的拒绝条件。仅原执行者可把命中的本轮意图翻译成取消。
        task = await db.get(Task, execution.task_id)
        if (task is not None and task.admin_cancel_operation_id is not None
                and matches_running(execution, task, cancellation_operation=task.admin_cancel_operation_id)):
            raise asyncio.CancelledError from None
        raise
    task = await db.get(Task, execution.task_id)
    if task.cancel_requested_at is not None:
        raise asyncio.CancelledError
    job = await db.get(AiExternalJob, job_id, populate_existing=True, with_for_update=True)
    if (job is None or job.id not in execution.input.get("external_job_ids", []) or job.actor_id != execution.actor_id
            or job.project_id != execution.project_id or job.request["source_kind"] != execution.kind
            or (job.project_id is not None and execution.input.get("_project_model") != job.request["project_binding"])
            or job.task_id not in (None, execution.task_id, execution.retry_of_id)):
        raise NotFoundError("视频任务不存在")
    if plugin_id := job.request["plugin_id"]:
        await plugin_state.require_enabled(db, plugin_id)
    return job


@asynccontextmanager
async def _dispatch(ctx, execution, job_id, state):
    @asynccontextmanager
    async def lease():
        async with ctx.sessionmaker() as db:
            job = await _job(db, execution, job_id)
            if job.state != state:
                raise ConflictError("视频任务状态已变化")
            await _config(db, ctx, job)
            if state == "submitting":
                args = VideoInputs.parse(job.request["inputs"])
                async for _ in args.references(db, job.actor_id, job.project_id, job.provider):
                    pass
                await ai_media_billing.check_frozen_video_budget(
                    db, _identity(job, execution), seconds=args.duration_seconds, now=datetime.now(UTC),
                    price=job.request["price"],
                )
            yield

    hooks = DispatchHooks(lease)
    with use_dispatch_hooks(hooks):
        try:
            yield
        finally:
            await hooks.aclose()


async def _fail(ctx, execution, job_id, code, message, *, state="failed"):
    async with ctx.sessionmaker() as db:
        job = await _job(db, execution, job_id)
        if job.state not in _TERMINAL:
            job.state, job.error_code, job.error_message = state, code, message
            job.finished_at = datetime.now(UTC)
            await media.mark_deleted(db, [_storage_id(job)])
            await db.commit()
    raise BadRequestError(message, extra={"code": code})


async def _submit(ctx, execution, job_id, provider, request):
    provider.validate(request)
    async with ctx.sessionmaker() as db:
        job = await _job(db, execution, job_id)
        if job.state != "created":
            raise ConflictError("视频任务提交状态已变化")
        now = datetime.now(UTC)
        price = await ai_media_billing.check_video_budget(
            db, _identity(job, execution), seconds=request.duration_seconds, now=now,
        )
        job.request = {**job.request, "price": price}
        job.state, job.submitted_at = "submitting", now
        job.deadline_at = now + timedelta(seconds=ctx.settings.video_job_timeout_seconds)
        await db.commit()
    try:
        async with asyncio.timeout(ctx.settings.video_job_timeout_seconds), _dispatch(
            ctx, execution, job_id, "submitting",
        ):
            external_id = await provider.submit(request)
    except TimeoutError:
        await _fail(ctx, execution, job_id, "video_timeout", f"提交视频任务超时。{_UNKNOWN}", state="expired")
    except AIEndpointError:
        raise
    except AIProviderError as exc:
        await _fail(ctx, execution, job_id, "video_submit_failed", f"{exc}。{_UNKNOWN}")
    # 收据必须在后续业务动作之前保存。执行者可能刚失去租约；只允许补记一次厂商返回的事实。
    async with ctx.sessionmaker() as db:
        result = await db.execute(update(AiExternalJob).where(
            AiExternalJob.id == job_id, AiExternalJob.state == "submitting", AiExternalJob.external_id.is_(None),
        ).values(external_id=external_id, state="submitted"))
        late_cancel = False
        if result.rowcount != 1:
            # 取消恰好发生在收到回执前。仍保存这次原始回执，按原配置通知厂商。
            recorded = await db.execute(update(AiExternalJob).where(
                AiExternalJob.id == job_id, AiExternalJob.error_code == "video_cancel_unconfirmed",
                AiExternalJob.external_id.is_(None),
            ).values(external_id=external_id, state="cancelled", error_code=None, error_message="视频任务已取消"))
            late_cancel = recorded.rowcount == 1
        await db.commit()
    if late_cancel:
        await _cancel_external(ctx, job_id, external_id)
        raise ConflictError("视频任务已取消")
    if result.rowcount != 1:
        raise ConflictError(_UNKNOWN)
    return external_id


def _result_target(job):
    return dict(owner_id=job.request["owner_id"], project_id=job.project_id, plugin_id=job.request["plugin_id"],
                scope="project" if job.project_id is not None else "user",
                origin="video_probe" if job.request["source_kind"] == "ai.video_probe" else "generation")


def _storage_id(job):
    return job.request.get("result_storage_id", job.id)


async def _publish(ctx, execution, job_id, provider, poll):
    async with ctx.sessionmaker() as db:
        job = await _job(db, execution, job_id)
        target = _result_target(job)
        object_id = _storage_id(job)
        stored = await db.get(MediaObject, object_id)
        if stored is not None:
            if (stored.owner_user_id != target["owner_id"] or stored.project_id != job.project_id
                    or stored.plugin_id != target["plugin_id"] or stored.scope != target["scope"]
                    or stored.kind != "video" or stored.source != target["origin"] or stored.parent_id is not None):
                raise ConflictError("视频存储对象归属已变化")
            if stored.status != "ready" or stored.deleted_at is not None:
                # 前一执行可能仍在传输。保留其物理键供清理，让新执行在新键上完成同一厂商任务。
                await media.mark_deleted(db, [object_id])
                object_id = str(uuid4())
                job.request = {**job.request, "result_storage_id": object_id}
                await db.commit()
                stored = None

    async def require_upload(db):
        current = await _job(db, execution, job_id)
        if current.state != "submitted" or _storage_id(current) != object_id:
            raise ConflictError("视频任务已结束或存储预留已变更")
        await _config(db, ctx, current)

    if stored is None:
        async with _dispatch(ctx, execution, job_id, "submitted"), ctx.sessionmaker() as db:
            async with aclosing(provider.download(poll)) as chunks:
                stored = await media.store(db, **target, object_id=object_id, kind="video", mime=poll.mime_type,
                                           source=chunks, max_bytes=_MAX_DOWNLOAD, write_guard=require_upload)
    async with ctx.sessionmaker() as db:
        job = await _job(db, execution, job_id)
        if job.state == "succeeded":
            return job.result_object_id
        if job.state != "submitted" or _storage_id(job) != object_id:
            raise ConflictError("视频任务已结束或存储预留已变更")
        await _config(db, ctx, job)
        now = datetime.now(UTC)
        seconds = poll.duration_seconds
        if seconds is None and stored.duration_ms:
            seconds = (stored.duration_ms + 999) // 1000
        job.usage_call_id = await ai_media_billing.settle_video(
            db, _identity(job, execution), job_id=job.id, seconds=seconds,
            started_at=as_utc(job.submitted_at), finished_at=now, price=job.request["price"],
        )
        job.state, job.progress, job.result_object_id, job.finished_at = "succeeded", 100, object_id, now
        await db.commit()
    return object_id


async def _drive_one(ctx, job_id, execution, emit):
    async with ctx.sessionmaker() as db:
        job = await _job(db, execution, job_id)
        if job.state == "succeeded" and job.result_object_id is not None:
            return job.result_object_id
        if job.state in _TERMINAL:
            raise BadRequestError(job.error_message or "视频任务已结束")
        if job.state == "submitting":
            raise ConflictError(_UNKNOWN)
        provider = _provider(ctx, await _config(db, ctx, job))
        job.task_id = execution.task_id
        await db.commit()
    if job.state == "created":
        async with ctx.sessionmaker() as db:
            request = await VideoInputs.parse(job.request["inputs"]).to_request(
                db, model=job.model, actor=job.actor_id, project=job.project_id, provider=job.provider,
            )
        await emit("正在向厂商提交视频", 5)
        await _submit(ctx, execution, job_id, provider, request)

    failures = 0
    while True:
        async with ctx.sessionmaker() as db:
            job = await _job(db, execution, job_id)
            if job.state != "submitted":
                raise BadRequestError(job.error_message or "视频任务已结束")
            remaining = (as_utc(job.deadline_at) - datetime.now(UTC)).total_seconds()
            external_id = job.external_id
        if remaining <= 0:
            await _fail(ctx, execution, job_id, "video_timeout", "等待视频生成超时", state="expired")
        try:
            async with asyncio.timeout(remaining), _dispatch(ctx, execution, job_id, "submitted"):
                poll = await provider.poll(external_id)
        except (VideoTransientError, TimeoutError):
            failures += 1
        else:
            failures = 0
            if poll.state == "failed":
                await _fail(ctx, execution, job_id, "video_provider_failed", poll.error_message or "厂商生成失败")
            if poll.state == "succeeded":
                await emit("正在保存视频", 95)
                remaining = (as_utc(job.deadline_at) - datetime.now(UTC)).total_seconds()
                try:
                    async with asyncio.timeout(max(0, remaining)):
                        return await _publish(ctx, execution, job_id, provider, poll)
                except TimeoutError:
                    await _fail(ctx, execution, job_id, "video_timeout", "下载或保存视频超时", state="expired")
            progress = min(94, max(5, poll.progress or 5))
            async with ctx.sessionmaker() as db:
                job = await _job(db, execution, job_id)
                if job.state == "submitted" and job.progress != progress:
                    job.progress = progress
                    await db.commit()
            await emit("等待厂商生成视频", progress)
        delay = min(30, _POLL_INTERVAL * 2 ** min(failures, 3)) * random.uniform(0.9, 1.1)
        await asyncio.sleep(min(delay, max(0, (as_utc(job.deadline_at) - datetime.now(UTC)).total_seconds())))


async def drive(ctx: TaskContext, job_ids: Sequence[str], *, execution: TaskExecutionIdentity,
                emit: Callable[[str, int], Awaitable[None]], parallel: int = 3) -> list[str]:
    if parallel < 1 or len(set(job_ids)) != len(job_ids):
        raise BadRequestError("视频任务列表或并发数无效")
    slots = asyncio.Semaphore(parallel)

    async def run(job_id):
        async with slots:
            try:
                return await _drive_one(ctx, job_id, execution, emit)
            except AIEndpointError as exc:
                async with ctx.sessionmaker() as db:
                    row = await _job(db, execution, job_id)
                    detail = "作者共享视频模型接口不可用，请联系作者" if row.payer_id != row.actor_id else str(exc)
                await _fail(ctx, execution, job_id, "video_endpoint_unavailable", detail)
            except AIProviderError as exc:
                await _fail(ctx, execution, job_id, "video_provider_failed", str(exc))

    workers = [asyncio.create_task(run(job_id)) for job_id in job_ids]
    try:
        return await asyncio.gather(*workers)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


async def _cancel_external(ctx, job_id, external_id):
    # 取消只带厂商任务号，仍要固定原模型配置到实际发出请求。
    @asynccontextmanager
    async def lease():
        async with ctx.sessionmaker() as db:
            job = await db.get(AiExternalJob, job_id)
            if job is None or job.state != "cancelled" or job.external_id != external_id:
                raise ConflictError("视频任务已变化")
            await _config(db, ctx, job)
            yield

    hooks = DispatchHooks(lease)
    try:
        async with ctx.sessionmaker() as db:
            job = await db.get(AiExternalJob, job_id)
            if job is None:
                return
            provider = _provider(ctx, await _config(db, ctx, job))
        with use_dispatch_hooks(hooks):
            async with asyncio.timeout(5):
                await provider.cancel(external_id)
    except (AIProviderError, AppError, TimeoutError):
        pass
    finally:
        await hooks.aclose()


async def cancel_jobs(ctx: TaskContext, job_ids: Sequence[str], *, actor_id: int, project_id: int | None = None,
                      task: Task | None = None, execution: TaskExecutionIdentity | None = None) -> None:
    for job_id in job_ids:
        async with ctx.sessionmaker() as db:
            if task is not None:
                current = await db.get(Task, task.id, with_for_update=True)
                if (current is None or current.statistics_key != task.statistics_key
                        or current.queued_at != task.queued_at or current.worker_key != task.worker_key
                        or current.execution_key != task.execution_key
                        or job_id not in current.input.get("external_job_ids", [])
                        or (execution is not None and current.execution_key not in (None, execution.execution_key))):
                    continue
            job = await db.get(AiExternalJob, job_id, with_for_update=True)
            if (job is None or job.actor_id != actor_id or job.project_id != project_id or job.state in _TERMINAL
                    or (task is not None and job.task_id not in (None, task.id))):
                continue
            external_id = job.external_id
            if job.state == "submitting" and external_id is None:
                job.state, job.error_code, job.error_message = "failed", "video_cancel_unconfirmed", _UNKNOWN
            else:
                job.state, job.error_message = "cancelled", "视频任务已取消"
            job.finished_at = datetime.now(UTC)
            await media.mark_deleted(db, [_storage_id(job)])
            await db.commit()
        if external_id is not None:
            await _cancel_external(ctx, job_id, external_id)
