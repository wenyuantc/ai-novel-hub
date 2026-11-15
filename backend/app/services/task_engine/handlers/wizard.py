"""wizard.outline / wizard.characters / wizard.world / wizard.careers：包装 wizard_service。

E4-g3c：作者与 accepted editor 共用（同 E4-g3a/b 的受控受理）。入口锁外 ai.use 短读、模型预检与 DNS，
受理锁内按原 grant 重验；领取（引擎复核）、构建前、检索前、每次发送（for_task lease，含续写来源）、每个输出事件
发布与保存/终态写入前均按原授权轮次重验。

与大纲/实体不同，向导**直接写库**（整体替换角色/世界观/职业或覆盖/追加大纲），因此不能按草稿型失败收尾：
内容替换与完成收据（`_wizard_completion`）在同一事务提交，收据之后若完成事件/终态核验因撤权、降级、关模型授权
或改密失败，由引擎 `_complete_from_wizard_receipt` 只凭本轮收据受限收尾为 succeeded（不发布、不派生、不写内容）；
内容从未提交时，未决凭据按原执行系统清理后以普通失败/中断收尾。证明不了时保持 running 屏障。

E5-c3：受控任务流的帧来源由 `stream_guard` 交出——未决阶段用构建时的发送 guard，保存后的 done 用按完成收据断点与
保存事务内新可见性戳构造的仅发布 guard；两者都只与自身原执行逐字匹配，不跨步骤转交来源。
设定类向导的未决帧若在本任务收紧可见性的保存之后才发出，由中央校验器按「同执行戳后继」改以同一 hub 登记的
done 仅发布 guard 复核（stream_evidence._verify_wizard_successor，2026-11-13）；这里不为此另造来源。
"""

import hashlib
from collections.abc import AsyncIterator
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError
from app.models import Task
from app.schemas.chapter import WizardRequest
from app.services import authorization_service, collaboration_ai_guard, project_ai_config, wizard_service
from app.services.ai_dispatch import _wait_send_cleanup, read_visibility_stamp, require_visibility_stamp
from app.services.collaboration_ai_guard import EMBEDDING_KEY
from app.services.collaboration_task_access import GRANT_KEY
from app.services.project_service import get_execution_project
from app.services.task_engine.execution_identity import (
    TaskExecutionIdentity,
    matches_running,
    require_write_execution,
)
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register

WIZARD_LABELS = {"outline": "大纲向导", "characters": "角色向导", "world": "世界观向导", "careers": "职业体系向导"}


def _wizard_kind(kind: str) -> wizard_service.WizardKind:
    return cast(wizard_service.WizardKind, kind.removeprefix("wizard."))


def _invalid_binding() -> BadRequestError:
    return BadRequestError("任务身份或模型绑定已失效，请重新发起")


class WizardHandler:
    kinds = ("wizard.outline", "wizard.characters", "wizard.world", "wizard.careers")
    execution_identity_aware = True
    admission_preflight_aware = True
    # E4-g3c：开放 accepted editor；作者私人风格/模板不可见。E6-d / E6-f：作者与协作执行都在受理时冻结共享向量选择。
    collaboration_execution_ready = True

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any],
        admission: project_ai_config.ProjectModelPreflight, accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        original = project_ai_config.require_original_preflight(admission)
        if (type(accepted_model) is not project_ai_config.ResolvedProjectModel
                or accepted_model != original.binding or original.actor.id != user_id
                or project_ai_config.is_original_model_preflight(original)
                or original.allow_private != ctx.settings.allow_private_ai_endpoints
                or original.requested_config_id != params.get("ai_config_id")):
            raise ConflictError("任务模型预检身份已变化")
        return await self._claim(db, ctx, user_id=user_id, kind=kind, params=params, accepted_model=accepted_model)

    async def validate_retry(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task,
        admission: project_ai_config.ProjectModelPreflight, accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        """重试只能使用原任务服务端固定的模型与原邀请轮次，不能因默认配置变动而更换发送对象。"""
        data = task.input
        if (task.user_id != user_id or task.kind not in self.kinds or type(task.project_id) is not int
                or task.chapter_id is not None or type(data) is not dict or type(data.get("request")) is not dict
                or type(accepted_model) is not project_ai_config.ResolvedProjectModel
                or accepted_model.project_id != task.project_id
                or type(admission) is not project_ai_config.ProjectModelPreflight
                or admission.binding != accepted_model):
            raise _invalid_binding()
        # 作品作者取自中央按原来源接受的绑定；lock_wizard_project 再在锁内核对当前作品作者与原 grant 一致。
        binding = project_ai_config.deserialize_project_model(
            data.get("_project_model"), actor_id=user_id, project_id=task.project_id,
            project_owner_id=accepted_model.project_owner_id,
        )
        request = data["request"]
        if (binding is None or binding != accepted_model or binding.model_type != "text"
                or type(data.get("ai_config_id")) is not int or data["ai_config_id"] != binding.config_id
                or type(request.get("project_id")) is not int or request["project_id"] != task.project_id
                or (request.get("ai_config_id") is not None
                    and (type(request["ai_config_id"]) is not int or request["ai_config_id"] != binding.config_id))):
            raise _invalid_binding()
        params = {**request, "ai_config_id": binding.config_id}
        return await self._claim(db, ctx, user_id=user_id, kind=task.kind, params=params,
                                 accepted_model=accepted_model)

    async def _claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any],
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        wkind = _wizard_kind(kind)
        project_id = int(params["project_id"])
        req = WizardRequest.model_validate(params)
        actor = authorization_service.actor_snapshot(db, user_id)
        if (type(actor) is not authorization_service.AuthenticatedActor or actor.id != user_id
                or type(actor.token_version) is not int or actor.token_version < 0):
            raise ConflictError("向导原认证身份无法验证", extra={"code": "wizard_actor_unavailable"})
        # 认证 → 发送写门禁 → 作品（按中央接受的原 grant）→ 来源任务，避免在权限屏障前读取私人续写内容。
        await wizard_service.lock_wizard_project(
            db, user_id=user_id, project_id=project_id, accepted_model=accepted_model,
        )
        retry_of_id: int | None = None
        resume_binding = None
        if req.continue_from_task_id is not None:
            with db.no_autoflush:
                source = await wizard_service.load_resume_source(
                    db, user_id=user_id, project_id=project_id, kind=wkind, task_id=req.continue_from_task_id,
                    task_key=req.continue_from_task_key, for_update=True,
                    grant_identity=accepted_model.grant_identity,
                )
                version = source.input.get("_wizard_actor_token_version")
                generic_version = source.input.get("_task_actor_token_version")
                if (type(version) is not int or version < 0 or version != actor.token_version
                        or type(generic_version) is not int or generic_version != version):
                    raise BadRequestError("要继续的向导任务不存在")
                resume_binding = wizard_service.bind_resume_source(source)
                req = wizard_service.resume_request(source, req)
                retry_of_id = source.id
        if req.ai_config_id is not None and req.ai_config_id != accepted_model.config_id:
            raise _invalid_binding()
        await wizard_service.validate_wizard_replacement(db, project_id=project_id, kind=wkind, req=req)
        embedding: dict[str, Any] = {}
        # E6-d（裁决 9）/ E6-f（裁决 13）：作者与协作执行都在同一受理事务冻结共享向量选择（未授权 / 作者无向量
        # → keyword），运行时每次查询嵌入经 for_task 守卫逐次重验。重试 / 续写按当前授权重新冻结（裁决 15）。
        try:
            selection = await project_ai_config.resolve_shared_embedding(
                db, user_id, project_id, cipher=ctx.cipher,
                allow_private=ctx.settings.allow_private_ai_endpoints, for_update=True,
            )
        except BadRequestError:
            if accepted_model.grant_identity is not None:
                raise
            # 作者本人默认向量配置本地不可用（解密 / 端点格式等）：保持原行为（检索退回关键词，不拒绝受理），
            # 不冻结选择；运行时无该键 → 只用关键词、零向量发送。协作执行仍按 E4-g3c 原样拒绝。
            selection = None
        if selection is not None:
            if (selection.grant_identity != accepted_model.grant_identity
                    or selection.project_owner_id != accepted_model.project_owner_id):
                raise ConflictError("任务模型预检身份已变化")
            embedding = {EMBEDDING_KEY: project_ai_config.serialize_shared_embedding(selection)}
        stamp = await read_visibility_stamp(db, project_id)
        return TaskSpec(
            project_id=project_id,
            chapter_id=None,
            resource_key=f"wizard:{project_id}:{wkind}",
            title=WIZARD_LABELS[wkind],
            input={
                "request": {"project_id": project_id, **req.model_dump(mode="json")},
                "ai_config_id": accepted_model.config_id,
                "_project_model": project_ai_config.serialize_project_model(accepted_model),
                "ai_history_epoch": stamp.epoch,
                "_wizard_actor_token_version": actor.token_version,
                **embedding,
                **({"_wizard_resume": resume_binding} if resume_binding is not None else {}),
            },
            retry_of_id=retry_of_id,
        )

    async def build_job(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity,
    ) -> wizard_service.WizardJob:
        if (not matches_running(execution, task)
                or execution.project_id is None or execution.kind not in self.kinds
                or execution.actor_token_version is None):
            raise ConflictError("向导任务执行身份已变化", extra={"code": "wizard_execution_changed"})
        captured = execution.input
        params = dict(captured["request"])
        if params.get("project_id") != execution.project_id:
            raise ConflictError("向导任务执行身份已变化", extra={"code": "wizard_execution_changed"})
        req = WizardRequest.model_validate(params)
        config_id = captured.get("ai_config_id")
        if type(config_id) is not int or config_id <= 0:
            raise ConflictError("向导原模型身份无法验证，请重新发起", extra={"code": "wizard_model_unavailable"})
        if execution.grant_identity is not None:
            # 构建前按原领取身份锁内重验（令牌、accepted editor、原邀请轮次、作者模型授权）；随即结束该写事务。
            # 未决凭据写入之前即拒绝，撤权后不留下需要清理的凭据，也不发生任何发送。
            async with ctx.sessionmaker() as barrier:
                await require_write_execution(barrier, execution)
                await barrier.rollback()
        async with ctx.sessionmaker() as db:
            owner_id = (execution.actor_id if execution.grant_identity is None
                        else (await get_execution_project(
                            db, execution.actor_id, execution.project_id, execution,
                        )).user_id)
        model_binding = project_ai_config.deserialize_project_model(
            captured.get("_project_model"), actor_id=execution.actor_id, project_id=execution.project_id,
            project_owner_id=owner_id,
        )
        if (model_binding is None or model_binding.model_type != "text" or model_binding.config_id != config_id
                or model_binding.grant_identity != execution.grant_identity):
            raise ConflictError("向导原模型身份无法验证，请重新发起", extra={"code": "wizard_model_unavailable"})
        shared_embedding = None
        if EMBEDDING_KEY in captured:
            # E6-f（裁决 13）：作者与协作执行都带受理冻结的选择。没有该键的只有两类（均只用关键词、零向量发送）：
            # 升级前入队的作者 / 协作向导任务，以及受理时作者本人向量配置本地不可用的作者任务。
            shared_embedding = project_ai_config.deserialize_shared_embedding(
                captured[EMBEDDING_KEY], actor_id=execution.actor_id, project_id=execution.project_id,
                project_owner_id=owner_id,
                task_grant=captured.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
            )
            if shared_embedding is None or shared_embedding.grant_identity != execution.grant_identity:
                raise ConflictError("向导原模型身份无法验证，请重新发起", extra={"code": "wizard_model_unavailable"})
        binding = wizard_service.parse_resume_binding(
            captured, req, user_id=execution.actor_id, project_id=execution.project_id,
            kind=_wizard_kind(execution.kind), grant_identity=execution.grant_identity,
        )
        pending = await wizard_service.begin_wizard_pending(ctx.sessionmaker, execution)
        try:
            async with ctx.sessionmaker() as db:
                return await wizard_service.build_wizard_job(
                    db,
                    user_id=execution.actor_id,
                    project_id=execution.project_id,
                    kind=_wizard_kind(execution.kind),
                    req=req,
                    resume_binding=binding,
                    # 使用受理时固定的配置 ID；排队期间更换默认配置不能换成本次新模型。
                    ai_config_id=config_id,
                    model_binding=model_binding,
                    shared_embedding=shared_embedding,
                    execution=pending.active,
                    pending=pending,
                    cipher=ctx.cipher,
                    factory=ctx.factory,
                    ai_timeout=ctx.settings.ai_request_timeout,
                    allow_private=ctx.settings.allow_private_ai_endpoints,
                )
        except BaseException:
            # 构建失败尚未尝试内容保存；只清理已确认属于原执行体的凭据。
            await _wait_send_cleanup(wizard_service.discard_wizard_pending(ctx.sessionmaker, pending))
            raise

    def run(self, job: wizard_service.WizardJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return wizard_service.run_wizard_job(job, ctx.sessionmaker)

    def progress(self, task: Task, text: str) -> int:
        # 输出长度未知：生成中按字符数在 10–90 之间缓慢推进
        return min(90, 10 + len(text) // 50)

    def checkpoint_out(self, task: Task) -> None:
        """向导断点只含服务端执行凭据，不对客户端暴露。"""
        return None

    async def verify_event(
        self, db: AsyncSession, task: Task, *, job: wizard_service.WizardJob,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
    ) -> None:
        """调用方已持原 actor、可见性、项目与当前 Task 的短事务（事件核验经 require_write_execution，含原 grant）。"""
        if job.execution != current_execution or job.visibility_stamp is None or job.model_binding is None:
            raise ConflictError("向导任务执行身份已变化", extra={"code": "wizard_execution_changed"})
        await require_visibility_stamp(db, job.visibility_stamp)
        if job.resume_binding is not None:
            # done 只在保存后产出：job.visibility_stamp 已是保存事务内读取的新戳（上一行要求它仍成立），
            # 来源 epoch 改与构建戳 epoch 比较（保存事务已核对二者相等），因为本任务自己的覆盖保存可能推进 epoch。
            # 其余事件（stage / delta / error）均在保存前，仍与当前 epoch 比较。
            expected_epoch = None
            if event == "done":
                if type(job.resume_epoch) is not int:
                    raise wizard_service.WizardResumeChanged()
                expected_epoch = job.resume_epoch
            await wizard_service.verify_resume_source(
                db, job.resume_binding, for_update=True, expected_epoch=expected_epoch,
            )
        if job.cipher is None:
            raise ConflictError("向导原模型身份无法验证", extra={"code": "wizard_model_unavailable"})
        await project_ai_config.revalidate_project_model(
            db, job.model_binding, cipher=job.cipher, allow_private=job.allow_private,
        )
        if event == "done":
            wizard_service.verify_wizard_completion(job, current_execution, data)

    def stream_guard(
        self, job: wizard_service.WizardJob, *, execution: TaskExecutionIdentity,
        current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
    ) -> collaboration_ai_guard.SendGuard:
        """把本执行刚通过 verify_event 的同一状态绑定成帧来源交给中央 ASGI 帧租约（E5-c3）；不补签。

        - 未决凭据阶段（stage / delta / error）：交出构建时的发送 guard（原执行即本轮未决断点、构建可见性戳）。
        - 保存后的 done：内容与完成收据同事务提交，断点与可见性戳都已推进；交出按「本轮完成断点 + 保存事务内
          读取的可见性戳」构造的仅发布 guard（缓存，按全部绑定输入的对象身份失效），并先按原未决凭据逐键
          核对完成收据（verify_wizard_completion）。不从持锁行重签，也不把未决阶段的帧转交给它。
          续写时该 guard 另携构建戳 epoch，来源 epoch 与之比较（本任务的覆盖保存可能已推进当前 epoch）。
        """
        if (type(job) is not wizard_service.WizardJob or job.pending is None
                or type(job.guard) is not collaboration_ai_guard.SendGuard
                or job.guard._task is not job.pending.active or job.guard._binding != job.model_binding
                or job.guard._resume is not job.resume_binding or job.model_binding is None
                or job.execution != current_execution):
            raise collaboration_ai_guard.CollaborationSendBlocked()
        if current_execution == job.pending.active:
            if job.visibility_stamp is not job.guard._stamp:
                raise collaboration_ai_guard.CollaborationSendBlocked()
            return job.guard
        if (event != "done" or job.cipher is None or job.visibility_stamp is None
                or (job.resume_binding is not None and type(job.resume_epoch) is not int)):
            raise collaboration_ai_guard.CollaborationSendBlocked()
        wizard_service.verify_wizard_completion(job, current_execution, data)
        inputs = (current_execution, job.model_binding, job.visibility_stamp, job.cipher, job.allow_private,
                  job.resume_binding, job.resume_epoch)
        cached = job.publication_guard
        if (isinstance(cached, tuple) and len(cached) == 2 and type(cached[0]) is tuple
                and len(cached[0]) == len(inputs)
                and all(old is new for old, new in zip(cached[0], inputs, strict=True))
                and type(cached[1]) is collaboration_ai_guard.SendGuard):
            return cached[1]
        guard = collaboration_ai_guard.for_task(
            current_execution, job.model_binding, job.visibility_stamp, cipher=job.cipher,
            allow_private=job.allow_private, resume=job.resume_binding, publication_only=True,
            resume_epoch=job.resume_epoch if job.resume_binding is not None else None,
            # 与构建 guard 相同的冻结共享向量选择（E6-d）；任务输入带选择时缺省会被拒绝。
            extra_bindings=job.guard._extra_bindings,
        )
        job.publication_guard = (inputs, guard)
        return guard

    async def verify_finalization(
        self, db: AsyncSession, task: Task, *, job: wizard_service.WizardJob | None,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, status: str,
        result: object, text: str,
    ) -> None:
        if status != "succeeded":
            return
        if job is None:
            raise ConflictError("向导结果凭据无法验证", extra={"code": "wizard_result_uncertain"})
        if current_execution.grant_identity is not None:
            # 终态事务只取了认证/门禁/作品锁；协作执行在同一事务按原 grant 重验 accepted editor 与作者模型授权，
            # 失败由引擎按完成收据受限收尾（结果已随内容提交，不再发布）。
            await require_write_execution(db, current_execution)
        await self.verify_event(db, task, job=job, execution=execution, current_execution=current_execution,
                                event="done", data=result)
        receipt = current_execution.checkpoint["_wizard_completion"]
        if receipt["stream_hash"] != hashlib.sha256(text.encode()).hexdigest():
            raise ConflictError("向导结果凭据无法验证", extra={"code": "wizard_result_uncertain"})

    async def on_abort(self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity | None = None) -> None:
        # 向导不修改任何状态，解析失败也不删除旧数据
        return None


register(WizardHandler())
