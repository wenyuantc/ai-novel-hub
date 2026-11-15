"""character.generate / organization.generate / career.generate / relations.parse / relations.generate。

包装 entity_ai_service：只产出草稿，不写库。

E4-g3b：作者与 accepted editor 共用（同 E4-g3a 大纲）。协作者任务按原 grant 在受理、领取（引擎复核）、构建前、
检索前、每次发送（for_task lease，含实体来源快照）、每个输出事件发布与终态写入前重验；唯一持久化是 Task 自身的
终态与草稿 result（人工确认后经阶段 D 的 content.edit 接口另行保存），在同一终态事务内锁内重验后写入，
因而撤权后没有“已提交、待证明”的业务副作用，无需收据收尾；终态核验失败按引擎 `_DRAFT_ONLY_KINDS` 改记无结果失败。
"""

from collections.abc import AsyncIterator
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models import Task
from app.schemas.career import CareerGenerateRequest
from app.schemas.entity_ai import (
    CharacterGenerateRequest,
    OrganizationGenerateRequest,
    RelationsGenerateRequest,
    RelationsParseRequest,
)
from app.services import entity_ai_service, project_ai_config
from app.services.collaboration_task_access import GRANT_KEY
from app.services.project_service import get_execution_project
from app.services.task_engine.execution_identity import TaskExecutionIdentity, matches_running, require_write_execution
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register

_REQUESTS = {
    "character.generate": CharacterGenerateRequest,
    "organization.generate": OrganizationGenerateRequest,
    "career.generate": CareerGenerateRequest,
    "relations.parse": RelationsParseRequest,
    "relations.generate": RelationsGenerateRequest,
}


class EntityHandler:
    kinds = tuple(_REQUESTS)
    execution_identity_aware = True
    admission_preflight_aware = True
    # E4-g3b：开放 accepted editor；检索只用关键词（共享向量检索属 E6），作者私人风格/模板不可见。
    collaboration_execution_ready = True

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any],
        admission: project_ai_config.ProjectModelPreflight, accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        if type(admission) is not project_ai_config.ProjectModelPreflight or admission.binding != accepted_model:
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        return await self._claim(db, ctx, user_id=user_id, kind=kind, params=params, accepted_model=accepted_model)

    async def validate_retry(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task,
        admission: project_ai_config.ProjectModelPreflight, accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        """保留原受理绑定，旧任务没有可信绑定时不能把重试伪装成新的模型选择。"""
        if (task.user_id != user_id or task.kind not in _REQUESTS or type(task.project_id) is not int
                or task.chapter_id is not None or not isinstance(task.input, dict)
                or not isinstance(task.input.get("request"), dict)
                or type(task.input.get("ai_config_id")) is not int
                or type(task.input["request"].get("project_id")) is not int
                or task.input["request"]["project_id"] != task.project_id
                or type(accepted_model) is not project_ai_config.ResolvedProjectModel
                or accepted_model.project_id != task.project_id):
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        # 作品作者取自中央按原来源接受的绑定；validate_entity_task 再在锁内核对当前作品作者与原 grant 一致。
        owner_id = accepted_model.project_owner_id
        binding = project_ai_config.deserialize_project_model(
            task.input.get("_project_model"), actor_id=user_id, project_id=task.project_id,
            project_owner_id=owner_id,
        )
        embedding = project_ai_config.deserialize_shared_embedding(
            task.input.get("_shared_embedding"), actor_id=user_id, project_id=task.project_id,
            project_owner_id=owner_id, task_grant=task.input.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
        )
        if (type(admission) is not project_ai_config.ProjectModelPreflight or admission.binding != accepted_model
                or binding is None or binding != accepted_model or embedding is None
                or embedding.grant_identity != binding.grant_identity
                or binding.config_id != task.input["ai_config_id"]):
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        requested_id = task.input["request"].get("ai_config_id")
        if requested_id is not None and (type(requested_id) is not int or requested_id != binding.config_id):
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        return await self._claim(db, ctx, user_id=user_id, kind=task.kind,
                                 params=dict(task.input["request"]), original_model_binding=binding,
                                 original_embedding=embedding, accepted_model=accepted_model)

    async def _claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any],
        accepted_model: project_ai_config.ResolvedProjectModel,
        original_model_binding: project_ai_config.ResolvedProjectModel | None = None,
        original_embedding: project_ai_config.ResolvedSharedEmbedding | None = None,
    ) -> TaskSpec:
        ekind = cast(entity_ai_service.EntityKind, kind)
        project_id = int(params["project_id"])
        character_id = int(params["character_id"]) if params.get("character_id") is not None else None
        req = _REQUESTS[kind].model_validate(params)
        binding, embedding, title = await entity_ai_service.validate_entity_task(
            db,
            user_id=user_id,
            kind=ekind,
            project_id=project_id,
            character_id=character_id,
            ai_config_id=req.ai_config_id,
            cipher=ctx.cipher,
            allow_private=ctx.settings.allow_private_ai_endpoints,
            character_ids=req.character_ids if isinstance(req, RelationsGenerateRequest) else None,
            accepted_model=accepted_model,
            original_model_binding=original_model_binding,
            original_embedding=original_embedding,
        )
        request: dict[str, Any] = {"project_id": project_id, **req.model_dump(mode="json")}
        if character_id is not None:
            request["character_id"] = character_id
        return TaskSpec(
            project_id=project_id,
            chapter_id=None,
            # 生成角色 / 组织 / 职业 / 关系草稿允许并发；同一角色的旧关系解析同时只能有一个
            resource_key=f"relparse:{character_id}" if kind == "relations.parse" else None,
            title=title,
            input={"request": request, "ai_config_id": binding.config_id,
                   "_project_model": project_ai_config.serialize_project_model(binding),
                   "_shared_embedding": project_ai_config.serialize_shared_embedding(embedding)},
        )

    async def build_job(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity,
    ) -> entity_ai_service.EntityJob:
        if type(execution) is not TaskExecutionIdentity or not matches_running(execution, task):
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        original = execution.input
        if (not isinstance(original.get("request"), dict) or type(execution.project_id) is not int
                or (execution.grant_identity is None) != (GRANT_KEY not in original)
                or type(original.get("ai_config_id")) is not int
                or type(original["request"].get("project_id")) is not int
                or original["request"]["project_id"] != execution.project_id):
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        params = dict(original["request"])
        async with ctx.sessionmaker() as db:
            # 构建前按原领取身份锁内重验（令牌、协作者还含 accepted editor、原邀请轮次）；作品按原 grant 读取，
            # 两份绑定再与执行身份逐项核对。会话结束即释放写屏障，后续检索/发送各自在短事务或 lease 内重验。
            await require_write_execution(db, execution)
            project = await get_execution_project(db, execution.actor_id, execution.project_id, execution)
            binding = project_ai_config.deserialize_project_model(
                original.get("_project_model"), actor_id=execution.actor_id, project_id=execution.project_id,
                project_owner_id=project.user_id,
            )
            embedding = project_ai_config.deserialize_shared_embedding(
                original.get("_shared_embedding"), actor_id=execution.actor_id, project_id=execution.project_id,
                project_owner_id=project.user_id,
                task_grant=original.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
            )
            if (binding is None or embedding is None or binding.config_id != original["ai_config_id"]
                    or binding.model_type != "text"
                    or binding.grant_identity != execution.grant_identity
                    or embedding.grant_identity != execution.grant_identity):
                raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
            requested_id = params.get("ai_config_id")
            if requested_id is not None and (type(requested_id) is not int or requested_id != binding.config_id):
                raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        async with ctx.sessionmaker() as db:
            job = await entity_ai_service.build_entity_job(
                db,
                user_id=execution.actor_id,
                kind=cast(entity_ai_service.EntityKind, execution.kind),
                project_id=execution.project_id,
                params=params,
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
                model_binding=binding,
                shared_embedding=embedding,
                execution=execution,
            )
        return job

    def run(self, job: entity_ai_service.EntityJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return entity_ai_service.run_entity_job(job, ctx.sessionmaker)

    async def verify_event(
        self, db: AsyncSession, task: Task, *, job: entity_ai_service.EntityJob,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, event: str, data: dict,
    ) -> None:
        if job.send_guard is None:
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        await job.send_guard.require_task_write(db)
        if event == "done":
            # 关系来源和预览结果与发布同一事务重读，不能把另一作品的新身份带进结果。
            result = await entity_ai_service.build_result(db, job, job.result_object, lock_sources=True)
            data.clear()
            data.update(result)

    def stream_guard(
        self, job: entity_ai_service.EntityJob, *, execution: TaskExecutionIdentity,
        current_execution: TaskExecutionIdentity, event: str, data: dict,
    ):
        """仅把原领取时构造的发送守卫交给中央 ASGI 帧租约。"""
        checks = job.send_guard
        if (checks is None or execution != current_execution or checks._task != execution):
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        return checks

    async def verify_finalization(
        self, db: AsyncSession, task: Task, *, job: entity_ai_service.EntityJob | None,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, status: str, result: Any, text: str,
    ) -> None:
        if job is None:
            if status in ("succeeded", "paused"):
                raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
            return
        if job.send_guard is None:
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        await job.send_guard.require_task_write(db)
        if status == "succeeded":
            if not isinstance(result, dict):
                raise BadRequestError("任务结果已失效，请重新发起")
            current = await entity_ai_service.build_result(db, job, job.result_object, lock_sources=True)
            result.clear()
            result.update(current)

    def progress(self, task: Task, text: str) -> int:
        return min(90, 10 + len(text) // 20)

    async def on_abort(self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity | None = None) -> None:
        # 只产出草稿，没有需要还原的状态
        return None


register(EntityHandler())
