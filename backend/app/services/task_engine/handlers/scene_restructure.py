"""只读场景建议通过标准受理、执行、发送和发布门禁。"""

from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers.base import AIProviderError, ChatMessage, ThinkingLevel
from app.core.errors import BadRequestError
from app.models import Task
from app.schemas.scene_restructure import RestructureTaskRequest
from app.services import ai_config_service, collaboration_ai_guard, project_ai_config, scene_restructure
from app.services.authorization_service import AuthenticatedActor
from app.services.chapter_service import get_manual_chapter
from app.services.project_service import get_execution_project
from app.services.task_engine.execution_identity import TaskExecutionIdentity, matches_running, require_write_execution
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register


@dataclass(frozen=True, slots=True, repr=False)
class RestructureJob:
    guard: collaboration_ai_guard.SendGuard
    bound: collaboration_ai_guard.BoundProvider
    messages: list[ChatMessage]
    plan: scene_restructure.SavedPlan


class SceneRestructureHandler:
    kinds = (scene_restructure.KIND,)
    execution_identity_aware = True
    admission_preflight_aware = True
    collaboration_execution_ready = True

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any],
        admission: project_ai_config.ProjectModelPreflight, accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        try:
            request = RestructureTaskRequest.model_validate(params)
        except ValidationError:
            raise BadRequestError("场景建议请求无效，请重新预览") from None
        if (kind != scene_restructure.KIND or type(admission) is not project_ai_config.ProjectModelPreflight
                or admission.binding != accepted_model or accepted_model.model_type != "text"
                or (accepted_model.actor_id, accepted_model.project_id) != (user_id, request.project_id)):
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        project, grant = await scene_restructure._access(db, user_id, request.project_id, write=True)
        if (project.user_id, grant) != (accepted_model.project_owner_id, accepted_model.grant_identity):
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        chapter = await get_manual_chapter(db, user_id, project.id, request.chapter_id, "content.read")
        budget = ctx.settings.context_char_budget
        plan = await scene_restructure.confirmed_plan(
            db, chapter, request, admission.actor, grant, settings=ctx.settings,
        )
        scene_restructure.check_model_budget(
            scene_restructure.model_messages(plan.preview),
            ai_config_service.context_char_budget(admission.config, budget),
        )
        title = "场景拆分建议" if request.operation == "split" else "场景合并建议"
        return TaskSpec(
            project_id=project.id, chapter_id=chapter.id, resource_key=None, title=f"{title} · {chapter.title}",
            input={"request": request.model_dump(mode="json"), "ai_config_id": accepted_model.config_id,
                   "chapter_id": chapter.id},
        )

    async def validate_retry(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task,
        admission: project_ai_config.ProjectModelPreflight, accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        if task.user_id != user_id or task.project_id is None or task.chapter_id is None:
            raise BadRequestError("场景建议任务已经失效，请重新预览")
        request = scene_restructure.task_request(task.input, task.project_id, task.chapter_id)
        return await self.validate_and_claim(
            db, ctx, user_id=user_id, kind=task.kind, params=request.model_dump(mode="json"),
            admission=admission, accepted_model=accepted_model,
        )

    async def build_job(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity,
    ) -> RestructureJob:
        if (not matches_running(execution, task) or execution.kind != scene_restructure.KIND
                or execution.project_id is None or execution.chapter_id is None
                or execution.actor_token_version is None):
            raise BadRequestError("场景建议任务已经失效，请重新预览")
        async with ctx.sessionmaker() as db:
            await require_write_execution(db, execution)
            project = await get_execution_project(db, execution.actor_id, execution.project_id, execution)
            request = scene_restructure.task_request(execution.input, execution.project_id, execution.chapter_id)
            binding = scene_restructure.model_binding(
                execution.input, execution.actor_id, project, execution.grant_identity,
            )
            chapter = await get_manual_chapter(db, execution.actor_id, project.id, request.chapter_id, "content.read")
            actor = AuthenticatedActor(execution.actor_id, execution.actor_token_version)
            plan = await scene_restructure.confirmed_plan(
                db, chapter, request, actor, execution.grant_identity, settings=ctx.settings,
            )
            guard = collaboration_ai_guard.for_task(
                execution, binding, plan.stamp.visibility, cipher=ctx.cipher,
                allow_private=ctx.settings.allow_private_ai_endpoints, scene=plan.stamp,
            )
        bound = await guard.prepare_provider(
            ctx.sessionmaker, ctx.factory, request_timeout=ctx.settings.ai_request_timeout,
        )
        messages = scene_restructure.model_messages(plan.preview)
        scene_restructure.check_model_budget(
            messages, ai_config_service.context_char_budget(bound, ctx.settings.context_char_budget),
        )
        return RestructureJob(guard, bound, messages, plan)

    async def run(self, job: RestructureJob, ctx: TaskContext) -> AsyncIterator[Event]:
        chunks = []
        total = 0
        bound = job.bound
        try:
            limit = min(1_200, (bound.max_output_k or 2) * ai_config_service.TOKENS_PER_K)
            stream = bound.provider.generate_stream(
                job.messages, bound.model, max_tokens=limit, thinking=cast(ThinkingLevel, bound.thinking_level),
            )
            async with aclosing(stream) as pieces:
                async for piece in pieces:
                    total += len(piece)
                    if total > scene_restructure.MAX_RESPONSE_CHARS:
                        raise ValueError("模型结果过长")
                    chunks.append(piece)
            choices = scene_restructure.parse_choices("".join(chunks), job.plan.preview)
        except AIProviderError:
            yield "error", {"detail": "场景建议生成失败，请重试"}
        except (ValidationError, ValueError):
            yield "error", {"detail": "模型返回的场景建议格式无效，请重新生成"}
        else:
            # 只存已验证的编号与理由，原稿及左右预览由仍匹配的章版本重建。
            yield "done", choices.model_dump(mode="json")

    async def verify_event(
        self, db: AsyncSession, task: Task, *, job: RestructureJob,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, event: str, data: dict,
    ) -> None:
        await job.guard.require_task_write(db)

    def stream_guard(
        self, job: RestructureJob, *, execution: TaskExecutionIdentity,
        current_execution: TaskExecutionIdentity, event: str, data: dict,
    ) -> collaboration_ai_guard.SendGuard:
        if execution != current_execution:
            raise BadRequestError("场景建议任务执行身份已变化")
        return job.guard

    async def verify_finalization(
        self, db: AsyncSession, task: Task, *, job: RestructureJob | None,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, status: str, result: Any, text: str,
    ) -> None:
        if status == "succeeded":
            if job is None:
                raise BadRequestError("场景建议任务执行身份已变化")
            await job.guard.require_task_write(db)

    def progress(self, task: Task, text: str) -> int:
        return 0

    async def on_abort(self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity | None = None) -> None:
        return None


register(SceneRestructureHandler())
