"""outline.continue / outline.expand：包装 outline_ai_service。只产出草稿，不写库。

E4-g3a：作者与 accepted editor 共用。协作者任务按原 grant 在受理、领取（引擎复核）、构建前、检索前、
每次发送（for_task lease）、每个输出事件发布与终态写入前重验；唯一持久化是 Task 自身的终态与草稿 result，
在同一终态事务内锁内重验后写入，因而撤权后没有“已提交、待证明”的业务副作用，也无需收据收尾。
"""

from collections.abc import AsyncIterator
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Task
from app.schemas.outline import OutlineContinueRequest, OutlineExpandRequest
from app.services import outline_ai_service, project_access, project_ai_config
from app.services.collaboration_ai_guard import CollaborationSendBlocked, SendGuard
from app.services.collaboration_task_access import GRANT_KEY
from app.services.project_service import get_owned_project
from app.services.task_engine.execution_identity import (
    TaskExecutionIdentity,
    matches_running,
    require_write_execution,
)
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register


class OutlineHandler:
    kinds = ("outline.continue", "outline.expand")
    execution_identity_aware = True
    admission_preflight_aware = True
    # E4-g3a：开放 accepted editor；检索只用关键词（共享向量检索属 E6），作者私人风格/模板不可见。
    collaboration_execution_ready = True

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any],
        admission: project_ai_config.ProjectModelPreflight,
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        okind = cast(outline_ai_service.OutlineKind, kind)
        project_id = int(params["project_id"])
        binding, title, resource_key = await outline_ai_service.validate_outline_task(
            db,
            user_id=user_id,
            kind=okind,
            project_id=project_id,
            params=params,
            allow_private=ctx.settings.allow_private_ai_endpoints,
            admission=admission,
            accepted_model=accepted_model,
        )
        # 文本模型选择已持有配置选择门禁；同一受理事务固定共享检索模式。
        embedding = await project_ai_config.resolve_shared_embedding(
            db, user_id, project_id, cipher=ctx.cipher,
            allow_private=ctx.settings.allow_private_ai_endpoints, for_update=True,
        )
        if (embedding.grant_identity != binding.grant_identity
                or embedding.project_owner_id != binding.project_owner_id):
            raise ConflictError("任务模型预检身份已变化")
        if kind == "outline.expand":
            req: Any = OutlineExpandRequest.model_validate(params)
            request = {"project_id": project_id, "outline_id": int(params["outline_id"]), **req.model_dump(mode="json")}
        else:
            req = OutlineContinueRequest.model_validate(params)
            request = {"project_id": project_id, **req.model_dump(mode="json")}
        return TaskSpec(
            project_id=project_id,
            chapter_id=None,
            # 同项目同时只能有一个续写；同一条大纲同时只能有一个展开
            resource_key=resource_key,
            title=title,
            input={
                "request": request,
                "ai_config_id": binding.config_id,
                "_project_model": project_ai_config.serialize_project_model(binding),
                "_shared_embedding": project_ai_config.serialize_shared_embedding(embedding),
            },
        )

    async def validate_retry(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task,
        admission: project_ai_config.ProjectModelPreflight,
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        # 重试只能使用原任务服务端固定的模型，不能因默认配置变动而更换发送对象。
        data = task.input
        if (task.user_id != user_id or task.kind not in self.kinds or type(task.project_id) is not int
                or task.chapter_id is not None or type(data) is not dict or type(data.get("request")) is not dict):
            raise ConflictError("原任务模型绑定无法验证，请重新创建任务")
        if (type(accepted_model) is not project_ai_config.ResolvedProjectModel
                or accepted_model.project_id != task.project_id):
            raise ConflictError("原任务模型绑定无法验证，请重新创建任务")
        # 作品作者取自中央按原来源接受的绑定；validate_outline_task 再在锁内核对当前作品作者一致。
        owner_id = accepted_model.project_owner_id
        binding = project_ai_config.deserialize_project_model(
            data.get("_project_model"), actor_id=user_id, project_id=task.project_id,
            project_owner_id=owner_id,
        )
        embedding = project_ai_config.deserialize_shared_embedding(
            data.get("_shared_embedding"), actor_id=user_id, project_id=task.project_id,
            project_owner_id=owner_id, task_grant=data.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
        )
        request = data["request"]
        if (binding is None or binding != accepted_model or binding.model_type != "text"
                or embedding is None or embedding.grant_identity != binding.grant_identity
                or type(data.get("ai_config_id")) is not int or data["ai_config_id"] != binding.config_id
                or type(request.get("project_id")) is not int or request["project_id"] != task.project_id
                or (request.get("ai_config_id") is not None
                    and (type(request["ai_config_id"]) is not int or request["ai_config_id"] != binding.config_id))):
            raise ConflictError("原任务模型绑定无法验证，请重新创建任务")
        params = {**request, "ai_config_id": binding.config_id}
        current, title, resource_key = await outline_ai_service.validate_outline_task(
            db, user_id=user_id, kind=cast(outline_ai_service.OutlineKind, task.kind), project_id=task.project_id,
            params=params, allow_private=ctx.settings.allow_private_ai_endpoints,
            admission=admission, accepted_model=accepted_model,
        )
        if current != binding:
            raise ConflictError("原任务模型配置已变化，请重新创建任务")
        await project_ai_config.revalidate_shared_embedding(
            db, embedding, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
            for_update=True, lock_config=True,
        )
        return TaskSpec(
            project_id=task.project_id, chapter_id=None, resource_key=resource_key, title=title,
            input={"request": request, "ai_config_id": binding.config_id,
                   "_project_model": project_ai_config.serialize_project_model(binding),
                   "_shared_embedding": project_ai_config.serialize_shared_embedding(embedding)},
        )

    async def build_job(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity,
    ) -> outline_ai_service.OutlineJob:
        if (type(execution) is not TaskExecutionIdentity or not matches_running(execution, task)
                or execution.kind not in self.kinds or type(execution.project_id) is not int
                or execution.chapter_id is not None):
            raise ConflictError("任务执行身份已变化")
        data = execution.input
        params = data.get("request")
        if type(params) is not dict or type(params.get("project_id")) is not int \
                or params["project_id"] != execution.project_id:
            raise ConflictError("任务执行身份已变化")
        if execution.grant_identity is not None:
            # 构建前按原领取身份锁内重验（令牌、accepted editor、原邀请轮次、作者模型授权）；随即结束该写事务，
            # 后续检索/发送各自在独立短事务或 lease 内重验，不能持写锁跨越它们（SQLite 会自锁）。
            async with ctx.sessionmaker() as barrier:
                await require_write_execution(barrier, execution)
                await barrier.rollback()
        async with ctx.sessionmaker() as db:
            if execution.grant_identity is None:
                project = await get_owned_project(db, execution.actor_id, execution.project_id)
            else:
                with db.no_autoflush:
                    project = await project_access.require_project(
                        db, execution.actor_id, execution.project_id, "ai.use",
                        grant_identity=execution.grant_identity,
                    )
            text_binding = project_ai_config.deserialize_project_model(
                data.get("_project_model"), actor_id=execution.actor_id, project_id=execution.project_id,
                project_owner_id=project.user_id,
            )
            embedding = project_ai_config.deserialize_shared_embedding(
                data.get("_shared_embedding"), actor_id=execution.actor_id, project_id=execution.project_id,
                project_owner_id=project.user_id,
                task_grant=data.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
            )
            if (text_binding is None or text_binding.model_type != "text" or embedding is None
                    or text_binding.grant_identity != execution.grant_identity
                    or embedding.grant_identity != execution.grant_identity
                    or type(data.get("ai_config_id")) is not int or data["ai_config_id"] != text_binding.config_id
                    or (params.get("ai_config_id") is not None
                        and (type(params["ai_config_id"]) is not int
                             or params["ai_config_id"] != text_binding.config_id))):
                raise ConflictError("任务模型身份无法验证")
            return await outline_ai_service.build_outline_job(
                db,
                user_id=execution.actor_id,
                kind=cast(outline_ai_service.OutlineKind, execution.kind),
                project_id=execution.project_id,
                params=params,
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
                execution=execution,
                text_binding=text_binding,
                shared_embedding=embedding,
            )

    def run(self, job: outline_ai_service.OutlineJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return outline_ai_service.run_outline_job(job, ctx.sessionmaker)

    async def verify_event(
        self, db: AsyncSession, task: Task, *, job: outline_ai_service.OutlineJob,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
    ) -> None:
        if (job.guard is None or job.guard._task is not execution or job.execution is not execution
                or current_execution != execution
                or not matches_running(execution, task)):
            raise CollaborationSendBlocked()
        await job.guard.require_task_write(db)

    def stream_guard(
        self, job: outline_ai_service.OutlineJob, *, execution: TaskExecutionIdentity,
        current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
    ) -> SendGuard:
        if (job.guard is None or job.guard._task is not execution or job.execution is not execution
                or current_execution != execution):
            raise CollaborationSendBlocked()
        return job.guard

    async def verify_finalization(
        self, db: AsyncSession, task: Task, *, execution: TaskExecutionIdentity,
        current_execution: TaskExecutionIdentity, status: str, result: dict[str, Any] | None,
        job: outline_ai_service.OutlineJob | None, text: str,
    ) -> None:
        if (status not in ("succeeded", "paused") or result is None or current_execution != execution
                or not matches_running(execution, task) or job is None
                or job.execution is not execution or job.guard is None or job.guard._task is not execution):
            raise CollaborationSendBlocked()
        await job.guard.require_task_write(db)

    def progress(self, task: Task, text: str) -> int:
        return min(90, 10 + len(text) // 40)

    async def on_abort(self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity | None = None) -> None:
        # 只产出草稿，没有需要还原的状态
        return None


register(OutlineHandler())
