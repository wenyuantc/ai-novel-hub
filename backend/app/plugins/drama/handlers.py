"""短剧文字任务：受控发送、按场景分批、在任务成功事务内采纳结果。"""

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.core.errors import BadRequestError, ConflictError
from app.models import Task
from app.plugins.drama.prompts import parse_output, render_prompt
from app.plugins.drama.services.task_material import (
    TaskRequest,
    material,
    revisions,
    script_batches,
    subjects,
    validate_references,
)
from app.plugins.drama.services.task_results import apply_draft
from app.services import ai_config_service, collaboration_ai_guard, project_access, project_ai_config
from app.services.ai_dispatch import VisibilityStamp, read_visibility_stamp
from app.services.project_service import get_execution_project
from app.services.task_engine.execution_identity import (
    TaskExecutionIdentity,
    advance_checkpoint,
    matches_running,
    require_write_execution,
)
from app.services.task_engine.registry import Event, TaskSpec

_TITLES = {
    "drama.rewrite": "改写短剧剧本",
    "drama.extract": "提取短剧素材",
    "drama.storyboard": "拆解短剧分镜",
    "drama.prompts": "生成短剧提示词",
}
_MAX_RESPONSE = 200_000


def _stale():
    return ConflictError("任务材料已变化，请重新发起", extra={"code": "stale_revision"})


@dataclass
class DramaJob:
    request: TaskRequest
    task_execution_identity: TaskExecutionIdentity
    binding: project_ai_config.ResolvedProjectModel
    stamp: VisibilityStamp
    context: dict
    batches: list[str | None]
    signature: str
    guard: collaboration_ai_guard.SendGuard
    bound: collaboration_ai_guard.BoundProvider


def _guard(ctx, execution, binding, stamp):
    return collaboration_ai_guard.for_task(
        execution,
        binding,
        stamp,
        cipher=ctx.cipher,
        allow_private=ctx.settings.allow_private_ai_endpoints,
    )


class DramaHandler:
    kinds = tuple(_TITLES)
    execution_identity_aware = True
    admission_preflight_aware = True
    collaboration_execution_ready = True
    # 成功唯一的业务写与任务终态同事务。提交前失败可安全由引擎记作无结果失败。
    atomic_finalization = True

    async def validate_and_claim(self, db, ctx, *, user_id, kind, params, admission, accepted_model):
        try:
            request = TaskRequest.model_validate(params)
        except ValidationError:
            raise BadRequestError("短剧任务参数无效") from None
        if (
            kind != request.kind
            or type(admission) is not project_ai_config.ProjectModelPreflight
            or admission.binding != accepted_model
            or accepted_model.model_type != "text"
            or (accepted_model.actor_id, accepted_model.project_id) != (user_id, request.project_id)
        ):
            raise BadRequestError("短剧任务身份或模型绑定已失效")
        project = await project_access.require_project(
            db,
            user_id,
            request.project_id,
            "ai.use",
            for_update=True,
            visibility_write=True,
            grant_identity=accepted_model.grant_identity,
        )
        series, episode = await subjects(db, series_id=request.series_id, episode_id=request.episode_id)
        if series.project_id != project.id or (episode is None and kind != "drama.prompts"):
            raise BadRequestError("短剧任务目标无效")
        await material(db, request, series, episode)
        subject = f"episode:{episode.id}" if episode is not None else f"series:{series.id}"
        return TaskSpec(
            project.id,
            None,
            f"drama.{subject}",
            f"{_TITLES[kind]} · {(episode or series).title}",
            {
                "request": request.model_dump(mode="json"),
                "revision": revisions(series, episode),
                "ai_config_id": accepted_model.config_id,
            },
        )

    async def validate_retry(self, db, ctx, *, user_id, task, admission, accepted_model):
        binding = project_ai_config.deserialize_project_model(
            task.input.get("_project_model"),
            actor_id=user_id,
            project_id=task.project_id,
            project_owner_id=accepted_model.project_owner_id,
        )
        if task.user_id != user_id or binding != accepted_model:
            raise BadRequestError("短剧任务原身份已失效")
        spec = await self.validate_and_claim(
            db,
            ctx,
            user_id=user_id,
            kind=task.kind,
            params=task.input["request"],
            admission=admission,
            accepted_model=accepted_model,
        )
        if task.checkpoint and spec.input["revision"] != task.input["revision"]:
            raise _stale()
        return spec

    async def build_job(self, ctx, task, *, execution):
        if not matches_running(execution, task) or execution.kind not in self.kinds:
            raise BadRequestError("短剧任务执行身份已变化")
        request = TaskRequest.model_validate(execution.input["request"])
        async with ctx.sessionmaker() as db:
            await require_write_execution(db, execution)
            project = await get_execution_project(db, execution.actor_id, execution.project_id, execution)
            series, episode = await subjects(db, series_id=request.series_id, episode_id=request.episode_id)
            if (
                request.kind != execution.kind
                or project.id != series.project_id
                or project.id != request.project_id
                or revisions(series, episode) != execution.input["revision"]
            ):
                raise _stale()
            binding = project_ai_config.deserialize_project_model(
                execution.input.get("_project_model"),
                actor_id=execution.actor_id,
                project_id=project.id,
                project_owner_id=project.user_id,
            )
            if (
                not binding
                or binding.model_type != "text"
                or binding.config_id != execution.input["ai_config_id"]
                or binding.grant_identity != execution.grant_identity
            ):
                raise BadRequestError("短剧任务模型绑定已失效")
            context = await material(db, request, series, episode)
            stamp = await read_visibility_stamp(db, project.id)
        guard = _guard(ctx, execution, binding, stamp)
        bound = await guard.prepare_provider(
            ctx.sessionmaker, ctx.factory, request_timeout=ctx.settings.ai_request_timeout
        )
        budget = ai_config_service.context_char_budget(bound, ctx.settings.context_char_budget)
        batches = [None]
        if request.kind in ("drama.storyboard", "drama.extract"):
            script = context.pop("script")
            overhead = sum(len(m["content"]) for m in render_prompt(request.kind, context)) + 512
            if budget <= overhead:
                raise BadRequestError("素材列表超过模型上下文，请改用更大上下文的模型")
            batches = script_batches(script, min(5000, budget - overhead))
        elif sum(len(m["content"]) for m in render_prompt(request.kind, context)) > budget:
            raise BadRequestError("短剧材料超过模型上下文，请缩短原文、减少目标或更换模型")
        signature = hashlib.sha256(
            json.dumps([context, batches], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        if execution.checkpoint and execution.checkpoint.get("signature") != signature:
            raise _stale()
        return DramaJob(request, execution, binding, stamp, context, batches, signature, guard, bound)

    async def _checkpoint(self, job, ctx, drafts, cursor):
        checkpoint = {"signature": job.signature, "drafts": drafts}
        current = job.task_execution_identity
        async with ctx.sessionmaker() as db:
            await job.guard.require_task_write(db)
            row = await db.get(Task, current.task_id)
            row.checkpoint, row.step_cursor = checkpoint, cursor
            await db.commit()
        job.task_execution_identity = advance_checkpoint(current, checkpoint, step_cursor=cursor)
        job.guard = _guard(ctx, job.task_execution_identity, job.binding, job.stamp)
        if cursor < len(job.batches):
            job.bound = await job.guard.prepare_provider(
                ctx.sessionmaker, ctx.factory, request_timeout=ctx.settings.ai_request_timeout
            )

    async def run(self, job, ctx) -> AsyncIterator[Event]:
        kind = job.request.kind
        checkpoint = job.task_execution_identity.checkpoint or {}
        drafts = list(checkpoint.get("drafts", []))
        if len(drafts) != job.task_execution_identity.step_cursor or len(drafts) > len(job.batches):
            raise BadRequestError("短剧任务断点无效")
        for index in range(len(drafts), len(job.batches)):
            context = dict(job.context)
            batch = job.batches[index]
            if batch is not None:
                context["script"] = batch
            if kind == "drama.storyboard":
                shots = [shot for part in drafts for shot in part["shots"]]
                context.update(first_shot=len(shots) + 1, elapsed_seconds=sum(s["duration_seconds"] for s in shots))
            yield (
                "stage",
                {
                    "stage": f"{_TITLES[kind]}（{index + 1}/{len(job.batches)}）",
                    "progress": 5 + index * 85 // len(job.batches),
                },
            )
            messages = render_prompt(kind, context)
            for attempt in range(2):
                raw = ""
                bound = job.bound
                stream = bound.provider.generate_stream(
                    messages,
                    bound.model,
                    thinking=bound.thinking_level,
                    max_tokens=bound.max_output_k * ai_config_service.TOKENS_PER_K if bound.max_output_k else None,
                )
                async with aclosing(stream) as pieces:
                    async for piece in pieces:
                        if len(raw) + len(piece) > _MAX_RESPONSE:
                            yield "error", {"detail": "短剧生成结果超过长度上限"}
                            return
                        raw += piece
                        if kind == "drama.rewrite":
                            yield "delta", {"text": piece}
                try:
                    draft = parse_output(kind, raw)
                    validate_references(job.request, context, draft)
                    if kind == "drama.storyboard" and context["first_shot"] - 1 + len(draft["shots"]) > 200:
                        raise ValueError("整集分镜不得超过 200 条")
                except ValueError as exc:
                    if attempt or kind == "drama.rewrite":
                        if kind != "drama.rewrite":
                            yield "delta", {"text": raw}
                        yield "error", {"detail": "模型返回的短剧内容格式无效，请查看任务输出"}
                        return
                    issue = (
                        str(exc.errors(include_input=False)[0]["msg"])
                        if isinstance(exc, ValidationError)
                        else str(exc)[:200]
                    )
                    messages = [
                        *messages,
                        {"role": "user", "content": f"上次输出未通过校验：{issue}。请重新输出完整 JSON。"},
                    ]
                else:
                    drafts.append(draft)
                    break
            if len(job.batches) > 1:
                await self._checkpoint(job, ctx, drafts, index + 1)
        combined = dict(drafts[0])
        if len(drafts) > 1:
            combined = {key: [item for part in drafts for item in part[key]] for key in combined}
        yield "done", {"kind": kind, "draft": combined, "applied": False}

    async def verify_event(self, db, task, *, job, execution, current_execution, event, data):
        await job.guard.require_task_write(db)

    def stream_guard(self, job, *, execution, current_execution, event, data):
        if current_execution != job.task_execution_identity:
            raise BadRequestError("任务执行身份已变化")
        return job.guard

    async def verify_finalization(self, db, task, *, job, execution, current_execution, status, result: Any, text):
        if status != "succeeded":
            return
        if job is None or not isinstance(result, dict) or not isinstance(result.get("draft"), dict):
            raise BadRequestError("短剧任务缺少结果")
        await job.guard.require_task_write(db)
        series, episode = await subjects(db, series_id=job.request.series_id, episode_id=job.request.episode_id)
        if series.project_id != execution.project_id:
            raise _stale()
        current = revisions(series, episode)
        if current != execution.input["revision"]:
            result.update(applied=False, current=current)
            return
        await apply_draft(db, job.request, series, episode, result["draft"])
        result["applied"] = True

    def progress(self, task, text):
        return min(90, 10 + len(text) // 50)

    @staticmethod
    def checkpoint_successor(original, later):
        """同一执行中，后继只允许追加已经完成的场景批次。核心另核对执行身份和模型绑定。"""
        if original.kind not in ("drama.storyboard", "drama.extract"):
            return False
        before, after = original.checkpoint or {}, later.checkpoint
        if not isinstance(after, dict) or not isinstance(after.get("signature"), str):
            return False
        previous, following = before.get("drafts", []), after.get("drafts")
        return (
            isinstance(previous, list)
            and isinstance(following, list)
            and len(previous) == original.step_cursor < later.step_cursor == len(following)
            and following[: len(previous)] == previous
            and (not before or before.get("signature") == after["signature"])
        )

    def checkpoint_out(self, task):
        return {"finished_batches": task.step_cursor} if task.checkpoint else None

    async def on_abort(self, ctx, task, *, execution=None):
        return None
