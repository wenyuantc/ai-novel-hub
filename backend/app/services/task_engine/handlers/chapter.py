"""chapter.generate / chapter.polish / chapter.revise：包装 generation_service。

成功后（on_success）按项目开关自动排队分析本章（analysis_service.enqueue_auto）。
"""

import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Chapter, Project, Task
from app.schemas.chapter import GenerateChapterRequest, PolishChapterRequest, ReviseChapterRequest
from app.schemas.scene_ai import SceneTarget, parse_target
from app.services import (
    ai_dispatch,
    analysis_service,
    chapter_claim,
    collaboration_ai_guard,
    generation_service,
    planning_context_service,
    project_access,
    project_ai_config,
    scene_service,
)
from app.services.chapter_service import count_words
from app.services.collaboration_ai_guard import EMBEDDING_KEY, MODEL_KEY
from app.services.task_engine.execution_identity import TaskExecutionIdentity, matches_running, require_write_execution
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register

logger = logging.getLogger(__name__)

MODE_LABELS = {"generate": "AI 生成", "polish": "AI 润色", "revise": "按建议重写", "analyze": "章节分析"}
_MODES: dict[str, generation_service.Mode] = {
    "chapter.generate": "generate",
    "chapter.polish": "polish",
    "chapter.revise": "revise",
}
_REQUESTS = {"generate": GenerateChapterRequest, "polish": PolishChapterRequest, "revise": ReviseChapterRequest}


def _mode(kind: str) -> generation_service.Mode:
    return _MODES[kind]


def _request(kind: str, params: dict[str, Any]) -> generation_service.ChapterRequest:
    return _REQUESTS[_mode(kind)].model_validate(params)


def chapter_title(chapter_no: int, title: str, mode: str) -> str:
    head = f"第 {chapter_no} 章"
    # 默认标题「第N章」与前缀重复，不再拼接
    if title and title.replace(" ", "") != f"第{chapter_no}章":
        head = f"{head} {title}"
    return f"{head} · {MODE_LABELS[mode]}"[:200]


def _changed() -> ConflictError:
    return ConflictError("章节生成状态已变化，请刷新后重试", extra={"code": "chapter_claim_changed"})


def _capture_claim(task: Task, *, phase: str) -> chapter_claim.ChapterClaim:
    """从同一 Task 的服务端持久快照捕获身份；不从后来读到的执行体补齐旧快照。"""
    payload = task.input
    statuses = {"queued"} if phase == "queued" else {"running", "failed", "cancelled", "interrupted"}
    if task.kind not in _MODES or task.status not in statuses or not isinstance(payload, dict):
        raise _changed()
    grant = chapter_claim._grant(payload)
    pointer = payload.get("_chapter_claim")
    if (not isinstance(pointer, dict) or set(pointer) != {"chapter_id", "claim_key", "prev_status"}
            or type(pointer["chapter_id"]) is not int or pointer["chapter_id"] != task.chapter_id
            or payload.get("prev_status") != pointer["prev_status"]):
        raise _changed()
    identity = chapter_claim.ChapterClaim(
        task_id=task.id, task_key=task.statistics_key, actor_id=task.user_id, project_id=task.project_id,
        chapter_id=task.chapter_id,
        queued_at=chapter_claim._utc(task.queued_at) if isinstance(task.queued_at, datetime) else task.queued_at,
        execution_key=task.execution_key, worker_key=task.worker_key,
        claim_key=pointer["claim_key"], prev_status=pointer["prev_status"], grant_identity=grant,
        phase=phase, kind="single",
    )
    if not chapter_claim._valid_identity(identity):
        raise _changed()
    return identity


def capture_queued_claim(task: Task) -> chapter_claim.ChapterClaim:
    """仅从领取前受锁 queued 记录捕获，用于同次领取和排队取消。"""
    return _capture_claim(task, phase="queued")


def capture_running_claim(task: Task) -> chapter_claim.ChapterClaim:
    """仅在旧进程已排空的恢复中，从原 running 持久事实捕获；不是运行时换代入口。"""
    return _capture_claim(task, phase="running")


async def bind_queued_claim(db: AsyncSession, task: Task) -> chapter_claim.ChapterClaim:
    """Task 已 flush 且调用方持项目写屏障，与章占用同事务写入。"""
    if not isinstance(task.input, dict):
        await db.rollback()
        raise _changed()
    try:
        grant = chapter_claim._grant(task.input)
    except ConflictError:
        await db.rollback()
        raise
    claim = await chapter_claim.claim_queued_chapter(
        db, task_id=task.id, task_key=task.statistics_key, actor_id=task.user_id, project_id=task.project_id,
        chapter_id=task.chapter_id,
        queued_at=chapter_claim._utc(task.queued_at) if isinstance(task.queued_at, datetime) else task.queued_at,
        grant_identity=grant,
    )
    if task.input.get("prev_status") != claim.prev_status:
        await db.rollback()
        raise _changed()
    return claim


def _guard_inputs(job: generation_service.ChapterJob, execution: TaskExecutionIdentity) -> tuple[object, ...]:
    return (execution, job.model_binding, job.context_stamp, job.model_cipher, job.model_allow_private,
            job.chapter_claim, job.batch_attempt, job.embedding_selection)


def _cached_guard(
    job: generation_service.ChapterJob, inputs: tuple[object, ...],
) -> collaboration_ai_guard.SendGuard | None:
    """缓存只在全部绑定输入（按对象身份）未变时有效；否则视为不存在。"""
    cached = job.publication_guard
    if (isinstance(cached, tuple) and len(cached) == 2 and type(cached[0]) is tuple
            and len(cached[0]) == len(inputs)
            and all(old is new for old, new in zip(cached[0], inputs, strict=True))
            and type(cached[1]) is collaboration_ai_guard.SendGuard):
        return cached[1]
    return None


def _job_guard(
    job: generation_service.ChapterJob, execution: TaskExecutionIdentity,
) -> collaboration_ai_guard.SendGuard:
    """同一执行固定同一 SendGuard：发布校验与受控任务流帧来源须是同一对象（E5-c1）。

    任一绑定输入（按对象身份）变化即重建，不跨执行或跨章节 attempt 复用。
    """
    inputs = _guard_inputs(job, execution)
    cached = _cached_guard(job, inputs)
    if cached is not None:
        return cached
    guard = collaboration_ai_guard.for_task(
        execution, job.model_binding, job.context_stamp.visibility, cipher=job.model_cipher,
        allow_private=job.model_allow_private, scene=job.context_stamp, chapter=job.chapter_claim,
        batch_attempt=job.batch_attempt,
        extra_bindings=((job.embedding_selection,) if job.embedding_selection is not None else ()),
    )
    job.publication_guard = (inputs, guard)
    return guard


class ChapterHandler:
    kinds = ("chapter.generate", "chapter.polish", "chapter.revise")
    execution_identity_aware = True
    admission_preflight_aware = True
    # E4-f：accepted editor 的单章任务走带 grant 的受理/领取/build_job/发送/保存链路；
    # 分析见 E4-g1（handlers/analysis.py），批量见 E4-g2（handlers/batch.py）；
    # 大纲、实体、向导、知识库索引与 Agent 仍各自失败关闭。
    collaboration_execution_ready = True

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any],
        admission: project_ai_config.ProjectModelPreflight,
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        if (type(accepted_model) is not project_ai_config.ResolvedProjectModel
                or accepted_model != project_ai_config.require_original_preflight(admission).binding):
            raise _changed()
        return await self._claim(db, ctx, user_id=user_id, kind=kind, params=params, admission=admission)

    async def validate_retry(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task,
        admission: project_ai_config.ProjectModelPreflight,
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        """重试：重新校验与抢占，但沿用原任务的 Skill 快照（之后编辑 Skill 不影响重试）。"""
        if not isinstance(task.input, dict):
            raise _changed()
        accepted = project_ai_config.require_original_preflight(admission).binding
        if type(accepted_model) is not project_ai_config.ResolvedProjectModel or accepted_model != accepted:
            raise _changed()
        binding = project_ai_config.deserialize_project_model(
            task.input.get(MODEL_KEY, project_ai_config.PROJECT_MODEL_ABSENT), actor_id=user_id,
            project_id=task.project_id, project_owner_id=accepted.project_owner_id,
        )
        if binding is None or binding != accepted:
            raise _changed()
        embedding = project_ai_config.deserialize_shared_embedding(
            task.input.get(EMBEDDING_KEY, project_ai_config.SHARED_EMBEDDING_ABSENT),
            actor_id=user_id, project_id=task.project_id, project_owner_id=accepted.project_owner_id,
            task_grant=task.input.get("_collaboration_grant", project_ai_config.TASK_GRANT_ABSENT),
        )
        if embedding is None:
            raise _changed()
        params = dict(task.input.get("request") or {})
        with db.no_autoflush:
            await project_access.require_project(
                db, user_id, task.project_id, "ai.use", for_update=True, visibility_write=True,
                grant_identity=binding.grant_identity,
            )
            if generation_service._cached_dirty(db, Chapter, task.input["chapter_id"]):
                raise _changed()
            chapter = await db.scalar(select(Chapter).where(
                Chapter.id == task.input["chapter_id"], Chapter.project_id == task.project_id,
            ).with_for_update().execution_options(populate_existing=True))
            if chapter is None:
                raise _changed()
        target = parse_target(params.get("target"))
        if isinstance(target, SceneTarget):
            await scene_service.require_scene(db, chapter, target.scene_id)
        params.update(
            expected_content_revision=chapter.content_revision,
            expected_planning_revision=await planning_context_service.planning_revision(db, chapter.project_id),
        )
        return await self._claim(
            db, ctx, user_id=user_id, kind=task.kind, params=params, skill=task.input.get("skill"),
            admission=admission, original_model_binding=binding,
            original_embedding_selection=embedding,
        )

    async def _claim(
        self,
        db: AsyncSession,
        ctx: TaskContext,
        *,
        user_id: int,
        kind: str,
        params: dict[str, Any],
        admission: project_ai_config.ProjectModelPreflight,
        skill: dict[str, Any] | None = None,
        original_model_binding: project_ai_config.ResolvedProjectModel | None = None,
        original_embedding_selection: project_ai_config.ResolvedSharedEmbedding | None = None,
    ) -> TaskSpec:
        mode = _mode(kind)
        req = _request(kind, params)
        target = parse_target(params.get("target"))
        chapter_id = int(params["chapter_id"])
        claim = await generation_service.validate_and_claim(
            db,
            user_id=user_id,
            chapter_id=chapter_id,
            mode=mode,
            req=req,
            cipher=ctx.cipher,
            factory=ctx.factory,
            ai_timeout=ctx.settings.ai_request_timeout,
            allow_private=ctx.settings.allow_private_ai_endpoints,
            skill_snapshot=skill,
            target=target,
            project_id=params.get("project_id"),
            defer_claim=True,
            admission=admission,
            original_model_binding=original_model_binding,
            original_embedding_selection=original_embedding_selection,
        )
        if claim.model_binding is None:
            raise _changed()
        title = chapter_title(claim.chapter_no, claim.chapter_title, mode)
        if isinstance(target, SceneTarget):
            suffix = f" · 场景 {target.scene_id}"
            title = title[:200 - len(suffix)].rstrip() + suffix
        return TaskSpec(
            project_id=claim.project_id,
            chapter_id=claim.chapter_id,
            resource_key=f"chapter:{claim.chapter_id}",
            title=title,
            input={
                "request": {"chapter_id": claim.chapter_id, **req.model_dump(mode="json"),
                            "target": target.model_dump()},
                "chapter_id": claim.chapter_id,
                "prev_status": claim.prev_status,
                "ai_config_id": claim.ai_config_id,
                "source_words": claim.source_words,
                "skill": claim.skill,
                MODEL_KEY: project_ai_config.serialize_project_model(claim.model_binding),
                **({EMBEDDING_KEY: project_ai_config.serialize_shared_embedding(claim.embedding_selection)}
                   if claim.embedding_selection is not None else {}),
            },
        )

    async def build_job(
        self, ctx: TaskContext, task: Task, *, claim: chapter_claim.ChapterClaim | None = None,
        execution: TaskExecutionIdentity,
    ) -> generation_service.ChapterJob:
        if claim is None or not matches_running(execution, task) or capture_running_claim(task) != claim:
            raise _changed()
        original = execution.input
        async with ctx.sessionmaker() as db:
            await require_write_execution(db, execution)
            await chapter_claim.require_running_chapter(db, claim)
            owner_id = await db.scalar(select(Project.user_id).where(Project.id == execution.project_id))
            if type(owner_id) is not int:
                raise _changed()
            await db.rollback()
        binding = project_ai_config.deserialize_project_model(
            original.get(MODEL_KEY, project_ai_config.PROJECT_MODEL_ABSENT), actor_id=execution.actor_id,
            project_id=execution.project_id, project_owner_id=owner_id,
        )
        if binding is None:
            raise _changed()
        embedding = project_ai_config.deserialize_shared_embedding(
            original.get(EMBEDDING_KEY, project_ai_config.SHARED_EMBEDDING_ABSENT), actor_id=execution.actor_id,
            project_id=execution.project_id, project_owner_id=owner_id,
            task_grant=original.get("_collaboration_grant", project_ai_config.TASK_GRANT_ABSENT),
        )
        if embedding is None:
            raise _changed()
        params = dict(original["request"])
        async with ctx.sessionmaker() as db:
            return await generation_service.build_chapter_job(
                db,
                user_id=execution.actor_id,
                chapter_id=int(original["chapter_id"]),
                mode=_mode(execution.kind),
                req=_request(execution.kind, params),
                prev_status=original["prev_status"],
                # 使用原绑定，即使默认配置改变也不得重新选择。
                ai_config_id=params.get("ai_config_id"),
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
                context_budget=ctx.settings.context_char_budget,
                task_id=execution.task_id,
                task_key=execution.task_key,
                claim=claim,
                model_binding=binding,
                embedding_selection=embedding,
                execution=execution,
                skill=original.get("skill"),
                target=parse_target(params.get("target")),
            )

    def run(self, job: generation_service.ChapterJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return generation_service.run_chapter_job(job, ctx.sessionmaker)

    async def verify_event(
        self, db: AsyncSession, task: Task, *, job: generation_service.ChapterJob,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
    ) -> None:
        """在引擎持有的同一事务中核原执行、模型和章节后发布事件。"""
        if (type(job) is not generation_service.ChapterJob or job.execution != execution
                or current_execution != execution or job.chapter_claim is None or job.model_binding is None
                or job.model_cipher is None or job.context_stamp is None):
            raise _changed()
        guard = _job_guard(job, execution)
        if event in ("done", "paused"):
            if event == "done":
                if task.admin_cancel_operation_id is None:
                    receipt = await generation_service.completed_running_chapter_receipt(db, execution, data)
                else:
                    receipt = await generation_service.completed_admin_race_chapter_receipt(
                        db, execution, data, operation_id=task.admin_cancel_operation_id,
                    )
                if receipt is None:
                    raise _changed()
            else:
                from app.services.rich_document_review import verify_single_pending_event

                await verify_single_pending_event(db, execution, job.chapter_claim, data)
            await ai_dispatch.require_visibility_stamp(db, job.context_stamp.visibility)
            await project_ai_config.revalidate_project_model(
                db, job.model_binding, cipher=job.model_cipher, allow_private=job.model_allow_private,
                for_update=True,
            )
            if job.embedding_selection is not None:
                await project_ai_config.revalidate_shared_embedding(
                    db, job.embedding_selection, cipher=job.model_cipher, allow_private=job.model_allow_private,
                    for_update=True,
                )
            return
        await guard.require_task_write(db)

    def stream_guard(
        self, job: generation_service.ChapterJob, *, execution: TaskExecutionIdentity,
        current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
    ) -> collaboration_ai_guard.SendGuard:
        """仅把本执行刚通过 verify_event 的同一 guard 交给中央 ASGI 帧租约；不重建、不补签。"""
        # 只交出与当前全部绑定输入一致的缓存 guard（与 verify_event 刚用的同一对象）；
        # 任一输入在核验后被替换即拒绝，不能交出按旧绑定构造的 guard。
        guard = (_cached_guard(job, _guard_inputs(job, execution))
                 if type(job) is generation_service.ChapterJob else None)
        if (type(guard) is not collaboration_ai_guard.SendGuard or job.execution != execution
                or current_execution != execution or guard._task != execution
                or guard._chapter is None or guard._chapter != job.chapter_claim or job.batch_attempt is not None):
            raise _changed()
        return guard

    async def verify_finalization(
        self, db: AsyncSession, task: Task, *, job: generation_service.ChapterJob,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity,
        status: str, result: dict[str, Any] | None, text: str,
    ) -> None:
        """只在原事务内确认与完成事件相同的提交或待审凭据。"""
        if status not in ("succeeded", "paused") or not isinstance(result, dict) or not isinstance(text, str):
            raise _changed()
        await self.verify_event(
            db, task, job=job, execution=execution, current_execution=current_execution,
            event="done" if status == "succeeded" else "paused", data=result,
        )

    def progress(self, task: Task, text: str) -> int:
        mode = _mode(task.kind)
        if mode == "generate":
            target = int(task.input.get("request", {}).get("target_words") or 3000)
        elif mode == "revise":
            request = task.input.get("request", {})
            target = int(request.get("target_words") or task.input.get("source_words") or 0) or 1000
        else:
            target = int(task.input.get("source_words") or 0) or 1000
        return max(1, min(95, count_words(text) * 100 // max(target, 1)))

    async def on_abort(
        self, ctx: TaskContext, task: Task, *, claim: chapter_claim.ChapterClaim | None = None,
        execution: TaskExecutionIdentity | None = None, terminal_status: str | None = None,
    ) -> None:
        """只清理本执行体持有的原 claim；缺证据的历史任务不得推测章节归属。"""
        if claim is None:
            raise _changed()
        if execution is not None and (
            execution.task_id != claim.task_id or execution.task_key != claim.task_key
            or execution.actor_id != claim.actor_id or execution.execution_key != claim.execution_key
            or execution.worker_key != claim.worker_key
        ):
            raise _changed()
        expected = capture_queued_claim(task) if claim.phase == "queued" else capture_running_claim(task)
        if expected != claim:
            raise _changed()
        try:
            async with ctx.sessionmaker() as db:
                await chapter_claim.release_chapter_claim(db, claim, terminal_status=terminal_status)
                await db.commit()
        except Exception:  # noqa: BLE001
            # 该收尾同样由管理员drain触发，原始SQL参数可能包含旧断点数据。
            logger.error("任务回退章节状态失败 task=%s", task.id)
            raise

    async def on_success(self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity) -> None:
        """正文已被 AI 覆盖：按项目开关自动排队分析本章（D2）。

        E4-g1：协作者执行（带 grant）同样派生，子任务归真实触发者并继承原令牌、模型绑定与授权轮次；
        enqueue_auto 在锁内核验原完成收据与当前 grant，撤权或无法证明时抛错不入队（不影响章节成功）。
        """
        if not isinstance(parse_target((execution.input.get("request") or {}).get("target")), SceneTarget):
            await analysis_service.enqueue_auto(ctx, execution.project_id, execution.chapter_id, execution=execution)


register(ChapterHandler())
