"""wizard.outline / wizard.characters / wizard.world：包装 wizard_service。"""

from collections.abc import AsyncIterator
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.schemas.chapter import WizardRequest
from app.services import wizard_service
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register

WIZARD_LABELS = {"outline": "大纲向导", "characters": "角色向导", "world": "世界观向导"}


def _wizard_kind(kind: str) -> wizard_service.WizardKind:
    return cast(wizard_service.WizardKind, kind.removeprefix("wizard."))


class WizardHandler:
    kinds = ("wizard.outline", "wizard.characters", "wizard.world")

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any]
    ) -> TaskSpec:
        wkind = _wizard_kind(kind)
        project_id = int(params["project_id"])
        req = WizardRequest.model_validate(params)
        ai_config_id = await wizard_service.validate_wizard(
            db,
            user_id=user_id,
            project_id=project_id,
            req=req,
            cipher=ctx.cipher,
            factory=ctx.factory,
            ai_timeout=ctx.settings.ai_request_timeout,
            allow_private=ctx.settings.allow_private_ai_endpoints,
        )
        return TaskSpec(
            project_id=project_id,
            chapter_id=None,
            resource_key=f"wizard:{project_id}:{wkind}",
            title=WIZARD_LABELS[wkind],
            input={
                "request": {"project_id": project_id, **req.model_dump(mode="json")},
                "ai_config_id": ai_config_id,
            },
        )

    async def build_job(self, ctx: TaskContext, task: Task) -> wizard_service.WizardJob:
        params = dict(task.input["request"])
        async with ctx.sessionmaker() as db:
            return await wizard_service.build_wizard_job(
                db,
                user_id=task.user_id,
                project_id=int(params["project_id"]),
                kind=_wizard_kind(task.kind),
                req=WizardRequest.model_validate(params),
                # 按原请求重新解析：未指定 = 运行时的默认配置；指定的配置已删除 → 任务失败「模型配置不存在」
                ai_config_id=params.get("ai_config_id"),
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
            )

    def run(self, job: wizard_service.WizardJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return wizard_service.run_wizard_job(job, ctx.sessionmaker)

    def progress(self, task: Task, text: str) -> int:
        # 输出长度未知：生成中按字符数在 10–90 之间缓慢推进
        return min(90, 10 + len(text) // 50)

    async def on_abort(self, ctx: TaskContext, task: Task) -> None:
        # 向导不修改任何状态，解析失败也不删除旧数据
        return None


register(WizardHandler())
