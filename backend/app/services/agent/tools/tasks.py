"""仅允许项目内已有任务。预览调用真实 handler，启动任务留在调用方事务内。"""

from typing import Literal

from pydantic import ConfigDict, Field, create_model
from sqlalchemy import select

from app.core.errors import NotFoundError
from app.models import Chapter, Task
from app.schemas.analysis import AnalyzeRequest
from app.schemas.batch import BatchGenerateRequest
from app.schemas.career import CareerGenerateRequest
from app.schemas.chapter import GenerateChapterRequest, PolishChapterRequest, ReviseChapterRequest
from app.schemas.entity_ai import CharacterGenerateRequest, OrganizationGenerateRequest
from app.schemas.outline import OutlineContinueRequest, OutlineExpandRequest
from app.services import analysis_service
from app.services.task_engine.registry import TaskContext, get_handler

from .base import FIELD_LABELS, EmptyArgs, Tool, ToolCtx

AllowedKind = Literal[
    "chapter.generate",
    "chapter.polish",
    "chapter.revise",
    "chapter.batch",
    "chapter.analyze",
    "outline.expand",
    "outline.continue",
    "character.generate",
    "organization.generate",
    "career.generate",
]
_REQUESTS = {
    "chapter.generate": GenerateChapterRequest,
    "chapter.polish": PolishChapterRequest,
    "chapter.revise": ReviseChapterRequest,
    "chapter.batch": BatchGenerateRequest,
    "chapter.analyze": AnalyzeRequest,
    "outline.expand": OutlineExpandRequest,
    "outline.continue": OutlineContinueRequest,
    "character.generate": CharacterGenerateRequest,
    "organization.generate": OrganizationGenerateRequest,
    "career.generate": CareerGenerateRequest,
}
ALLOWED_KINDS = frozenset(_REQUESTS)

# 函数 schema 必须列明 params 的字段（Gemini 不接受没有 properties 的空 object）。
# 共用字段在这里可选；真正执行时按具体 kind 的原始 schema 应用默认值及限制。
_PARAM_FIELDS = {
    key: (field.annotation | None, Field(default=None, description=field.description))
    for request in _REQUESTS.values()
    for key, field in request.model_fields.items()
}
_PARAM_FIELDS.update({key: (int | None, None) for key in ("project_id", "chapter_id", "outline_id")})
TaskParams = create_model("AgentTaskParams", __base__=EmptyArgs, **_PARAM_FIELDS)


class StartArgs(EmptyArgs):
    kind: AllowedKind
    params: TaskParams = Field(
        default_factory=TaskParams,
        description=(
            "任务参数。chapter.generate/polish/revise/analyze 必须给 chapter_id；revise 还需 analysis_id。"
            "chapter.batch 必须给 start_chapter_id、end_chapter_id；outline.expand 必须给 outline_id。"
            "可选 ai_config_id、instruction；生成可给 target_words；大纲/职业生成可给 count。项目由服务端绑定。"
        ),
    )


def request_params(ctx: ToolCtx, args: StartArgs) -> dict:
    fields: dict = {"project_id": (int, Field(gt=0))}
    if args.kind.startswith("chapter.") and args.kind != "chapter.batch":
        fields["chapter_id"] = (int, Field(gt=0))
    if args.kind == "outline.expand":
        fields["outline_id"] = (int, Field(gt=0))
    schema = create_model(
        "AgentTaskRequest",
        __base__=_REQUESTS[args.kind],
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return schema.model_validate(
        {**args.params.model_dump(exclude_unset=True), "project_id": ctx.project_id}
    ).model_dump(mode="json")


class DeferredEngine:
    """task_service 的窄接口：审批提交后真实 engine.notify() 才调度，预览没有外部副作用。"""

    def __init__(self, context: TaskContext):
        self.ctx = context
        self.hubs = {}

    def ensure_hub(self, task_id: int, **kwargs) -> None:
        pass

    def notify(self) -> None:
        pass


async def preview(ctx: ToolCtx, args: StartArgs) -> dict:
    params = request_params(ctx, args)
    spec = await get_handler(args.kind).validate_and_claim(
        ctx.db,
        ctx.task_context,
        user_id=ctx.user_id,
        kind=args.kind,
        params=params,
    )
    if spec.project_id != ctx.project_id:
        raise NotFoundError("不存在")
    # 只保留任务实际涉及章节的指纹；批量范围由真实 handler 计算。
    chapter_ids = spec.input.get("chapter_ids", [spec.chapter_id] if spec.chapter_id is not None else [])
    chapters = (
        list(
            await ctx.db.scalars(
                select(Chapter)
                .where(
                    Chapter.project_id == ctx.project_id,
                    Chapter.id.in_(chapter_ids),
                )
                .order_by(Chapter.id)
            )
        )
        if chapter_ids
        else []
    )
    chapter_state = [
        {
            "id": row.id,
            "chapter_no": row.chapter_no,
            "title": row.title,
            "status": spec.input.get("prev_status", row.status),
            "content_hash": analysis_service.content_hash(row.content),
        }
        for row in chapters
    ]
    return {
        "title": spec.title,
        "target": spec.title,
        "diff": [{"field": "kind", "label": "任务类型", "before": None, "after": args.kind}]
        + [
            {"field": key, "label": FIELD_LABELS.get(key, key), "before": None, "after": value}
            for key, value in spec.input["request"].items()
            if key != "project_id"
        ],
        "warnings": ["会消耗 AI 调用；已启动的任务只能取消，不能撤销"],
        "chapter_state": chapter_state,
        "ai_config_id": spec.input.get("ai_config_id"),
    }


async def execute(ctx: ToolCtx, args: StartArgs) -> dict:
    from app.services import task_service

    task = await task_service.create_task(
        ctx.db,
        DeferredEngine(ctx.task_context),
        ctx.user_id,
        args.kind,
        request_params(ctx, args),
    )
    # handler 的最终绑定同样检查，防止将来注册表扩展时放宽边界。
    row = await ctx.db.get(Task, task.id)
    if row.project_id != ctx.project_id:
        raise NotFoundError("不存在")
    return {"task_id": task.id, "task": task.model_dump(mode="json")}


TOOLS = [
    Tool("start_task", "启动已有的生成、润色、重写、批量、分析或设定生成任务", StartArgs, "task", execute, preview)
]
