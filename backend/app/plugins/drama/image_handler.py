"""单个角色造型生图。图片预留、真实模型调用、媒体发布和任务终态使用同一原始身份。"""

import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.ai.images import MAX_COVER_BYTES
from app.ai.providers.base import AIProviderError, ImageCapabilityError, ImageInput, ImageOptions
from app.core.errors import AppError, BadRequestError, ConflictError
from app.media import cleanup, service
from app.models.media import MediaObject
from app.plugins.drama.episode_schemas import PositiveId
from app.plugins.drama.models import DramaAssetImage, DramaLook
from app.plugins.drama.services import images
from app.plugins.drama.services.task_material import TaskRequest, material, subjects
from app.plugins.drama.styles import STYLE_DIRECTIONS, STYLES
from app.services import ai_media_billing, collaboration_ai_guard, project_access, project_ai_config
from app.services.ai_dispatch import VisibilityPolicyError, read_visibility_stamp
from app.services.ai_usage_storage import UsageCallIdentity
from app.services.generated_media import _verify_ratio
from app.services.task_engine.execution_identity import matches_running, require_write_execution
from app.services.task_engine.registry import TaskSpec

KIND = "drama.image"


class LookTarget(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["look"]
    id: PositiveId


class ImageStart(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    kind: Literal["drama.image"]
    targets: list[LookTarget] = Field(min_length=1, max_length=1)
    ai_config_id: PositiveId | None = None


class ImageRequest(ImageStart):
    project_id: PositiveId
    series_id: PositiveId
    episode_id: PositiveId | None


async def _source(db, request):
    series, episode = await subjects(db, series_id=request.series_id, episode_id=request.episode_id)
    if series.project_id != request.project_id:
        raise BadRequestError("造型不属于当前作品")
    # 复用文本提示词的素材投影：包括剧/集边界与关联小说设定的 AI 隐藏字段。
    text_request = TaskRequest.model_validate({**request.model_dump(), "kind": "drama.prompts"})
    context = await material(db, text_request, series, episode)
    target = context["targets"][0]
    look = await db.get(DramaLook, target["id"])
    reference = await images.default_reference(db, look)
    description = look.image_prompt.strip() or "。".join(
        value
        for value in (
            f"绘制「{target['character']}」的「{target['name']}」造型定妆图",
            target["appearance"],
            target["styling"],
            "白色背景，全身正面、侧面与背面三视图，保持服装和人物比例一致",
        )
        if value
    )
    prompt = f"{STYLES[series.style]}。{STYLE_DIRECTIONS[series.style]}\n{description}"
    if reference:
        prompt += "\n沿用参考造型中同一张脸的五官、脸型与体态，只替换当前造型的服饰和妆发。"
    return look, prompt, reference


def _ref_ids(reference):
    return [reference.id] if reference is not None else []


async def _references(db, object_id, binding):
    if object_id is None:
        return ()
    obj = await db.get(MediaObject, object_id)
    if (
        obj is None
        or obj.project_id != binding.project_id
        or obj.owner_user_id != binding.project_owner_id
        or obj.scope != "project"
        or obj.plugin_id != "drama"
        or obj.kind != "image"
        or obj.parent_id is not None
        or obj.size_bytes > MAX_COVER_BYTES
        or obj.deleted_at is not None
    ):
        raise ConflictError("原造型参考图不可用，请重新生成")
    data = bytearray()
    async with aclosing(await service.open_range(db, obj)) as chunks:
        async for chunk in chunks:
            data.extend(chunk)
            if len(data) > MAX_COVER_BYTES:
                raise BadRequestError("造型参考图超过大小限制")
    return (ImageInput(bytes(data), obj.mime),)


@dataclass
class ImageJob:
    guard: collaboration_ai_guard.SendGuard
    provider: collaboration_ai_guard.BoundProvider
    request: ImageRequest
    object_id: str
    row_id: int
    owner_id: int
    prompt: str
    reference_ids: list[int]
    options: ImageOptions


class ImageHandler:
    kinds = (KIND,)
    execution_identity_aware = True
    admission_preflight_aware = True
    collaboration_execution_ready = True
    atomic_finalization = True
    admission_model_type = "image"

    async def validate_and_claim(self, db, ctx, *, user_id, kind, params, admission, accepted_model):
        request = ImageRequest.model_validate(params)
        if (
            kind != KIND
            or type(admission) is not project_ai_config.ProjectModelPreflight
            or admission.binding != accepted_model
            or accepted_model.model_type != "image"
            or (accepted_model.actor_id, accepted_model.project_id) != (user_id, request.project_id)
        ):
            raise BadRequestError("图片模型受理身份已变化")
        project = await project_access.require_project(
            db,
            user_id,
            request.project_id,
            "ai.use",
            for_update=True,
            visibility_write=True,
            grant_identity=accepted_model.grant_identity,
        )
        look, prompt, reference = await _source(db, request)
        cfg = admission.config
        identity = UsageCallIdentity(
            actor_id=user_id,
            payer_id=cfg.user_id,
            project_id=project.id,
            config_instance_key=cfg.instance_key,
            provider=cfg.provider,
            model=cfg.model,
            model_type="image",
            kind=KIND,
            author_paid=project.user_id == cfg.user_id,
        )
        await ai_media_billing.check_image_budget(db, identity, now=datetime.now(UTC))
        record = await images.reserve_generation(db, look, prompt=prompt, reference_image_ids=_ref_ids(reference))
        return TaskSpec(
            project.id,
            None,
            f"drama.image.look:{look.id}",
            f"生成造型图 · {look.name}",
            {
                "request": request.model_dump(),
                "image_id": record.id,
                "object_id": str(uuid4()),
                "reference_object_id": reference.media_object_id if reference else None,
                "ai_config_id": accepted_model.config_id,
            },
        )

    async def bind_queued(self, db, task):
        row = await db.get(DramaAssetImage, task.input["image_id"])
        if row is None or row.task_id is not None or row.status != "pending":
            raise ConflictError("造型图预留已变化")
        row.task_id = task.id
        await db.flush()

    async def validate_retry(self, db, ctx, *, user_id, task, admission, accepted_model):
        binding = project_ai_config.deserialize_project_model(
            task.input.get("_project_model"),
            actor_id=user_id,
            project_id=task.project_id,
            project_owner_id=accepted_model.project_owner_id,
        )
        if task.user_id != user_id or task.kind != KIND or binding != accepted_model:
            raise ConflictError("造型生图的原模型或授权已变化")
        return await self.validate_and_claim(
            db,
            ctx,
            user_id=user_id,
            kind=KIND,
            params=task.input["request"],
            admission=admission,
            accepted_model=accepted_model,
        )

    async def build_job(self, ctx, task, *, execution):
        if execution.kind != KIND or not matches_running(execution, task):
            raise ConflictError("造型生图的执行身份已变化")
        original = execution.input
        request = ImageRequest.model_validate(original["request"])
        async with ctx.sessionmaker() as db:
            await require_write_execution(db, execution)
            project = await project_access.require_project(
                db, execution.actor_id, execution.project_id, "ai.use", grant_identity=execution.grant_identity
            )
            binding = project_ai_config.deserialize_project_model(
                original.get("_project_model"),
                actor_id=execution.actor_id,
                project_id=project.id,
                project_owner_id=project.user_id,
            )
            if (
                not binding
                or binding.model_type != "image"
                or binding.config_id != original.get("ai_config_id")
                or binding.grant_identity != execution.grant_identity
                or request.project_id != project.id
            ):
                raise ConflictError("造型生图的模型绑定已变化")
            look, prompt, reference = await _source(db, request)
            row = await db.get(DramaAssetImage, original["image_id"])
            ref_object = reference.media_object_id if reference else None
            if (
                not row
                or row.series_id != request.series_id
                or row.owner_id != look.id
                or row.owner_type != "look"
                or row.task_id != task.id
                or row.status != "pending"
                or row.prompt != prompt
                or row.reference_image_ids != _ref_ids(reference)
                or ref_object != original["reference_object_id"]
            ):
                raise ConflictError("造型资料或参考图已变化，请重新生成")
            stamp = await read_visibility_stamp(db, project.id)
        guard = collaboration_ai_guard.for_task(
            execution, binding, stamp, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints
        )
        provider = await guard.prepare_provider(
            ctx.sessionmaker, ctx.factory, request_timeout=ctx.settings.ai_request_timeout
        )
        async with ctx.sessionmaker() as db:
            references = await _references(db, ref_object, binding)
        return ImageJob(
            guard,
            provider,
            request,
            original["object_id"],
            original["image_id"],
            binding.project_owner_id,
            prompt,
            _ref_ids(reference),
            ImageOptions("16:9", references),
        )

    async def run(self, job, ctx):
        yield "stage", {"stage": "生成角色造型图", "progress": 10}
        try:
            async with asyncio.timeout(ctx.settings.ai_request_timeout):
                image = await job.provider.provider.generate_image(job.prompt, job.provider.model, options=job.options)
            await asyncio.to_thread(_verify_ratio, image, "16:9", job.provider.provider_name)

            async def chunks():
                yield image.data

            async with ctx.sessionmaker() as db:
                await service.store(
                    db,
                    owner_id=job.owner_id,
                    project_id=job.request.project_id,
                    plugin_id="drama",
                    scope="project",
                    kind="image",
                    mime=image.mime_type,
                    source=chunks(),
                    max_bytes=MAX_COVER_BYTES,
                    origin="generated",
                    object_id=job.object_id,
                    write_guard=job.guard.require_task_write,
                )
        except VisibilityPolicyError:
            raise
        except ImageCapabilityError as exc:
            yield "error", {"detail": str(exc)}
            return
        except AppError as exc:
            yield "error", {"detail": exc.detail}
            return
        except (AIProviderError, TimeoutError):
            yield "error", {"detail": "图片模型生成失败或超时，请核对图片模型配置后重试"}
            return
        yield "done", {"image_id": job.row_id, "media_object_id": job.object_id, "look_id": job.request.targets[0].id}

    def stream_guard(self, job, *, execution, current_execution, event, data):
        if execution != current_execution:
            raise ConflictError("造型生图执行身份已变化")
        return job.guard

    async def verify_event(self, db, task, *, job, execution, current_execution, event, data):
        await job.guard.require_task_write(db)

    async def verify_finalization(self, db, task, *, job, execution, current_execution, status, result, text):
        if (
            not job
            or not isinstance(result, dict)
            or result.get("media_object_id") != job.object_id
            or result.get("image_id") != job.row_id
        ):
            raise ConflictError("造型图结果不匹配")
        await job.guard.require_task_write(db)
        look = await db.get(DramaLook, job.request.targets[0].id)
        if look is None:
            raise ConflictError("造型已删除")
        await images.add_generated(db, look, job.object_id, job.prompt, job.reference_ids, task_id=task.id)

    async def finalize_failure(self, db, task):
        """任务终态 CAS 已成功，清理同一次受理的预留与未关联的媒体；包括未领取即被撤权的情况。"""
        request = ImageRequest.model_validate(task.input["request"])
        row = await db.get(DramaAssetImage, task.input["image_id"])
        if row and (row.series_id, row.owner_type, row.owner_id) == (request.series_id, "look", request.targets[0].id):
            if row.status == "ready":
                return
            if row.task_id == task.id:
                row.status = "failed"
                row.error_message = task.error or "图片生成已停止"
        obj = await db.scalar(
            select(MediaObject.id).where(
                MediaObject.id == task.input["object_id"],
                MediaObject.project_id == task.project_id,
                MediaObject.scope == "project",
                MediaObject.plugin_id == "drama",
                MediaObject.kind == "image",
                MediaObject.parent_id.is_(None),
                MediaObject.source == "generated",
            )
        )
        if obj:
            await cleanup.mark_deleted(db, [obj])

    async def on_abort(self, ctx, task, **options):
        # 由引擎赢得终态 CAS 后执行 finalize_failure，旧轮次无权清理资产。
        return None

    def progress(self, task, text):
        return task.progress
