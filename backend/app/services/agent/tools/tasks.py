"""仅允许项目内已有任务。预览调用真实 handler，启动任务留在调用方事务内。"""

import hashlib
from typing import Literal

from pydantic import ConfigDict, Field, create_model
from sqlalchemy import select

from app.core.errors import BadRequestError, NotFoundError
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


class SkillInputItem(EmptyArgs):
    name: str = Field(min_length=1, max_length=32, description="输入项标识（load_skill 返回的 inputs[].name）")
    value: str = Field(max_length=5000)


# 请求里 skill_inputs 是「标识 → 值」映射；函数 schema 用带 properties 的对象数组表达
# （Gemini 会去掉 additionalProperties，映射会变成没有 properties 的空 object），执行前再转换回映射
_PARAM_FIELDS["skill_inputs"] = (
    list[SkillInputItem] | None,
    Field(default=None, max_length=20, description="Skill 输入项的值；仅在给了 skill_id 时使用"),
)
TaskParams = create_model("AgentTaskParams", __base__=EmptyArgs, **_PARAM_FIELDS)


class StartArgs(EmptyArgs):
    kind: AllowedKind
    params: TaskParams = Field(
        default_factory=TaskParams,
        description=(
            "任务参数。chapter.generate/polish/revise/analyze 必须给 chapter_id；revise 还需 analysis_id。"
            "chapter.batch 必须给 start_chapter_id、end_chapter_id；outline.expand 必须给 outline_id。"
            "可选 ai_config_id、instruction；生成可给 target_words；大纲/职业生成可给 count。项目由服务端绑定。"
            "带 skill_id 时可给 skill_inputs（输入项标识 → 值，定义见 load_skill 的 inputs），缺必填项会返回错误。"
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
    raw = args.params.model_dump(exclude_unset=True)
    items = raw.pop("skill_inputs", None)
    if items:
        names = [item["name"] for item in items]
        duplicated = sorted({n for n in names if names.count(n) > 1})
        if duplicated:
            # 映射会静默丢掉前面的值，直接报错让智能体修正
            raise BadRequestError(f"Skill 输入项标识重复：{'、'.join(duplicated)}")
        raw["skill_inputs"] = {item["name"]: item["value"] for item in items}
    return schema.model_validate({**raw, "project_id": ctx.project_id}).model_dump(mode="json")


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
        # Skill 快照的指纹：提案后 Skill 被修改时确认会刷新预览
        "skill": _skill_guard(spec.input.get("skill")),
    }


def _skill_guard(skill: dict | None) -> dict | None:
    if not skill:
        return None
    digest = hashlib.sha256(str(skill.get("body") or "").encode()).hexdigest()
    guard = {"id": skill.get("id"), "name": skill.get("name"), "hash": digest}
    if skill.get("inputs"):
        # 输入值同样参与 preview_changed 比较（正文哈希已包含填充结果，这里便于确认时展示）
        guard["inputs"] = dict(skill["inputs"])
    return guard


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
