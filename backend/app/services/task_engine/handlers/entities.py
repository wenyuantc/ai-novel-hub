"""character.generate / organization.generate / career.generate / relations.parse。

包装 entity_ai_service：只产出草稿，不写库。
"""

from collections.abc import AsyncIterator
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.schemas.career import CareerGenerateRequest
from app.schemas.entity_ai import CharacterGenerateRequest, OrganizationGenerateRequest, RelationsParseRequest
from app.services import entity_ai_service
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register

_REQUESTS = {
    "character.generate": CharacterGenerateRequest,
    "organization.generate": OrganizationGenerateRequest,
    "career.generate": CareerGenerateRequest,
    "relations.parse": RelationsParseRequest,
}


class EntityHandler:
    kinds = ("character.generate", "organization.generate", "career.generate", "relations.parse")

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any]
    ) -> TaskSpec:
        ekind = cast(entity_ai_service.EntityKind, kind)
        project_id = int(params["project_id"])
        character_id = int(params["character_id"]) if params.get("character_id") is not None else None
        req = _REQUESTS[kind].model_validate(params)
        ai_config_id, title = await entity_ai_service.validate_entity_task(
            db,
            user_id=user_id,
            kind=ekind,
            project_id=project_id,
            character_id=character_id,
            ai_config_id=req.ai_config_id,
            cipher=ctx.cipher,
            factory=ctx.factory,
            ai_timeout=ctx.settings.ai_request_timeout,
            allow_private=ctx.settings.allow_private_ai_endpoints,
        )
        request: dict[str, Any] = {"project_id": project_id, **req.model_dump(mode="json")}
        if character_id is not None:
            request["character_id"] = character_id
        return TaskSpec(
            project_id=project_id,
            chapter_id=None,
            # 生成角色 / 组织 / 职业允许并发；同一角色的旧关系解析同时只能有一个
            resource_key=f"relparse:{character_id}" if kind == "relations.parse" else None,
            title=title,
            input={"request": request, "ai_config_id": ai_config_id},
        )

    async def build_job(self, ctx: TaskContext, task: Task) -> entity_ai_service.EntityJob:
        params = dict(task.input["request"])
        async with ctx.sessionmaker() as db:
            return await entity_ai_service.build_entity_job(
                db,
                user_id=task.user_id,
                kind=cast(entity_ai_service.EntityKind, task.kind),
                project_id=int(params["project_id"]),
                # 按原请求重新解析：未指定 = 运行时的默认配置
                params=params,
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
            )

    def run(self, job: entity_ai_service.EntityJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return entity_ai_service.run_entity_job(job, ctx.sessionmaker)

    def progress(self, task: Task, text: str) -> int:
        return min(90, 10 + len(text) // 20)

    async def on_abort(self, ctx: TaskContext, task: Task) -> None:
        # 只产出草稿，没有需要还原的状态
        return None


register(EntityHandler())
