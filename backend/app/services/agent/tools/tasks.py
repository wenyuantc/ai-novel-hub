"""仅允许项目内已有任务。预览调用真实 handler，启动任务留在调用方事务内。

受控受理类型（章节生成/润色/重写、大纲、设定生成）：
- 预览：只用会话绑定的原认证身份做短读模型预检并调用真实 handler 校验；不做 DNS、不建任务、
  不领取章节占用、不调用模型。
- 提案：runner 以本轮 Task UUID/入队时间、真实 actor 令牌版本、提案身份、参数及章节实例签发来源证明，
  存入提案 result 的私有键（个人投影与事件均隐藏）。
- 确认：确认服务在取得任何写锁前核对来源证明并完成模型预检/DNS（prepare_confirmed_admission），
  锁内重验原轮次与章节实例后，经中央 create_task 在确认事务的保存点内受理（execute_admitted）。
旧提案没有或带有损坏的来源证明时失败关闭，不用当前数据补签。
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field, ValidationError, create_model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.models import AgentChange, AIConfig, Chapter, Task
from app.schemas.analysis import AnalyzeRequest
from app.schemas.batch import BatchGenerateRequest
from app.schemas.career import CareerGenerateRequest
from app.schemas.chapter import GenerateChapterRequest, PolishChapterRequest, ReviseChapterRequest
from app.schemas.entity_ai import CharacterGenerateRequest, OrganizationGenerateRequest
from app.schemas.outline import OutlineContinueRequest, OutlineExpandRequest
from app.services import analysis_service, project_ai_config
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.authorization_service import AuthenticatedActor
from app.services.collaboration_task_access import task_grant_identity
from app.services.system_settings_snapshot import SettingsUnavailable
from app.services.task_engine.admission import TaskAdmission
from app.services.task_engine.drain import TaskAdmissionClosed, require_task_admission_open
from app.services.task_engine.execution_identity import _utc
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
_DERIVED_CHAPTER_KINDS = frozenset({"chapter.generate", "chapter.polish", "chapter.revise"})
ORIGIN_KEY = "_agent_task_origin"
_ORIGIN_VERSION = 1
_ORIGIN_DOMAIN = b"ainovel/agent-task-origin/v1\x00"


def admission_required(kind: object) -> bool:
    """这些任务只能经中央受控受理（锁外模型预检 + 同事务消费凭据）入队。"""
    return (type(kind) is str and kind in ALLOWED_KINDS
            and getattr(get_handler(kind), "admission_preflight_aware", False) is True)


def _identity_required(kind: str) -> AppError:
    if kind in _DERIVED_CHAPTER_KINDS:
        # 不能从当前 Task 或章节整数 ID 重建原身份；缺少提案时签发的来源证明即拒绝。
        return VisibilityPolicyError(
            "暂无法验证派生章节任务的原始身份，请直接发起章节任务",
            extra={"code": "agent_derived_chapter_identity_required"},
        )
    return ConflictError(
        "智能体暂不能发起该类任务，请直接在对应页面发起",
        extra={"code": "agent_task_admission_unavailable"},
    )


def _bound_actor(ctx_db, user_id: int, kind: str) -> AuthenticatedActor:
    """只接受会话已绑定的原认证身份（runner 的轮次 actor 或确认请求的认证 actor）。"""
    actor = ctx_db.info.get("authenticated_actor")
    if (type(actor) is not AuthenticatedActor or actor.id != user_id
            or type(actor.token_version) is not int or actor.token_version < 0):
        raise _identity_required(kind)
    return actor


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
    return _request_params(ctx.project_id, args)


def _request_params(project_id: int, args: StartArgs) -> dict:
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
    return schema.model_validate({**raw, "project_id": project_id}).model_dump(mode="json")


class DeferredEngine:
    """提案预览只构造受理结果，真实调度由审批根事务提交后触发。"""

    def __init__(self, context: TaskContext):
        self.ctx = context
        self.hubs = {}

    def ensure_hub(self, task_id: int, **kwargs) -> None:
        pass

    def notify_queue_changed(self) -> None:
        pass


async def _validate_spec(ctx: ToolCtx, args: StartArgs, params: dict):
    handler = get_handler(args.kind)
    if not admission_required(args.kind):
        return await handler.validate_and_claim(
            ctx.db, ctx.task_context, user_id=ctx.user_id, kind=args.kind, params=params,
        )
    actor = _bound_actor(ctx.db, ctx.user_id, args.kind)
    deps = ctx.task_context
    allow_private = deps.settings.allow_private_ai_endpoints
    # 短读预检在独立会话完成且不解析 DNS；锁内按同一身份重选，二者不一致即拒绝。
    model = await project_ai_config.preflight_project_model(
        deps.sessionmaker, actor, ctx.project_id, "text", cipher=deps.cipher,
        config_id=params.get("ai_config_id"), allow_private=allow_private,
    )
    current = await project_ai_config.resolve_project_model(
        ctx.db, actor.id, ctx.project_id, "text", cipher=deps.cipher, config_id=params.get("ai_config_id"),
        allow_private=allow_private, for_update=True,
    )
    if current != model.binding:
        raise ConflictError("模型授权或配置已变化，请重新发起", extra={"code": "collaboration_busy"})
    return await handler.validate_and_claim(
        ctx.db, deps, user_id=ctx.user_id, kind=args.kind, params=params, admission=model, accepted_model=current,
    )


async def preview(ctx: ToolCtx, args: StartArgs) -> dict:
    params = request_params(ctx, args)
    spec = await _validate_spec(ctx, args, params)
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
    config_id = spec.input.get("ai_config_id")
    with ctx.db.no_autoflush:
        config_owner = await ctx.db.scalar(select(AIConfig.user_id).where(AIConfig.id == config_id)) \
            if type(config_id) is int else None
    own_config = config_owner == ctx.user_id
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
        "warnings": ["会消耗 AI 调用；已启动的任务只能取消，不能撤销"] + (
            [] if own_config or config_id is None else ["将使用作者授权的模型配置，外部费用由作者账户承担"]
        ),
        "chapter_state": chapter_state,
        # 协作者回退作者模型时不暴露作者的配置 ID（提案预览进入模型上下文与本人面板）。
        "ai_config_id": config_id if own_config else None,
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

    if admission_required(args.kind):
        # 通用工具执行位于保存点且已持写锁，无法先做锁外模型预检；只能经确认服务受理。
        raise _identity_required(args.kind)
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


# ---------------- 派生任务来源证明与受控受理 ----------------


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signing_key(db) -> bytes | None:
    key = db.info.get("agent_view_hmac_key")
    return key if isinstance(key, bytes) and len(key) == 32 else None


def _origin_mac(key: bytes, origin: dict, *, project_id: int, conversation_id: int, turn_task_id: int,
                tool_call_id: str, arguments: dict) -> str:
    payload = {
        "origin": {name: value for name, value in origin.items() if name != "mac"},
        "project": project_id, "conversation": conversation_id, "turn_task": turn_task_id,
        "call": tool_call_id, "tool": "start_task", "arguments": arguments,
    }
    return hmac.new(key, _ORIGIN_DOMAIN + _canonical(payload), hashlib.sha256).hexdigest()


def _iso(value: object) -> str | None:
    return _utc(value).isoformat() if isinstance(value, datetime) else None


def _target_chapter_id(arguments: object) -> int | None:
    params = arguments.get("params") if isinstance(arguments, dict) else None
    value = params.get("chapter_id") if isinstance(params, dict) else None
    return value if type(value) is int and value > 0 else None


async def sign_origin(
    db: AsyncSession, *, actor_id: int, token_version: int, project_id: int, conversation_id: int, turn_task_id: int,
    turn_task_key: str, queued_at: datetime, change_key: str, tool_call_id: str, arguments: dict,
) -> dict | None:
    """runner 在提案写入的同一锁内签发；非受控受理类型返回 None。"""
    kind = arguments.get("kind") if isinstance(arguments, dict) else None
    if not admission_required(kind):
        return None
    key = _signing_key(db)
    queued = _iso(queued_at)
    if key is None or queued is None or type(token_version) is not int or token_version < 0:
        raise _identity_required(kind)
    chapter = None
    if kind in _DERIVED_CHAPTER_KINDS:
        chapter_id = _target_chapter_id(arguments)
        with db.no_autoflush:
            row = (await db.execute(select(Chapter.id, Chapter.created_at).where(
                Chapter.id == chapter_id, Chapter.project_id == project_id,
            ))).one_or_none() if chapter_id is not None else None
        if row is None or _iso(row.created_at) is None:
            raise NotFoundError("不存在")
        # 章节实例：同项目 + 主键 + 创建时间；正文变化由预览指纹刷新，不纳入身份。
        chapter = {"id": row.id, "created_at": _iso(row.created_at)}
    origin = {
        "version": _ORIGIN_VERSION, "kind": kind, "actor": actor_id, "token_version": token_version,
        "task_key": turn_task_key, "queued_at": queued, "change_key": change_key, "chapter": chapter,
    }
    origin["mac"] = _origin_mac(
        key, origin, project_id=project_id, conversation_id=conversation_id, turn_task_id=turn_task_id,
        tool_call_id=tool_call_id, arguments=arguments,
    )
    return {ORIGIN_KEY: origin}


@dataclass(frozen=True, slots=True)
class ChangeSnapshot:
    """确认服务在释放只读事务前冻结的提案字段；之后不再访问可能过期的 ORM 属性。"""

    id: int
    kind: str
    status: str
    tool_name: str
    change_key: str
    project_id: int
    conversation_id: int
    turn_task_id: int | None
    turn_task_key: str | None
    tool_call_id: str
    arguments: Any
    result: Any

    @classmethod
    def of(cls, change: AgentChange) -> "ChangeSnapshot":
        return cls(change.id, change.kind, change.status, change.tool_name, change.change_key, change.project_id,
                   change.conversation_id, change.turn_task_id, change.turn_task_key, change.tool_call_id,
                   json.loads(json.dumps(change.arguments)), json.loads(json.dumps(change.result)))


def _admitted_kind(change: ChangeSnapshot | AgentChange) -> str | None:
    if change.kind != "task" or change.tool_name != "start_task" or not isinstance(change.arguments, dict):
        return None
    kind = change.arguments.get("kind")
    return kind if admission_required(kind) else None


def requires_admission(change: AgentChange) -> bool:
    return _admitted_kind(change) is not None


def _verified_origin(db, change: ChangeSnapshot | AgentChange, kind: str) -> dict:
    """只核对签名与提案自身字段；数据库事实由锁内 _require_origin_facts 复核。"""
    result = change.result
    origin = result.get(ORIGIN_KEY) if isinstance(result, dict) else None
    key = _signing_key(db)
    expected = {"version", "kind", "actor", "token_version", "task_key", "queued_at", "change_key", "chapter", "mac"}
    if (key is None or type(origin) is not dict or set(origin) != expected or set(result) != {ORIGIN_KEY}
            or origin["version"] != _ORIGIN_VERSION or origin["kind"] != kind
            or type(origin["actor"]) is not int or type(origin["token_version"]) is not int
            or origin["change_key"] != change.change_key or origin["task_key"] != change.turn_task_key
            or type(change.turn_task_id) is not int or type(origin["queued_at"]) is not str
            or type(origin["mac"]) is not str):
        raise _identity_required(kind)
    chapter = origin["chapter"]
    if kind in _DERIVED_CHAPTER_KINDS:
        if (type(chapter) is not dict or set(chapter) != {"id", "created_at"}
                or chapter["id"] != _target_chapter_id(change.arguments) or type(chapter["created_at"]) is not str):
            raise _identity_required(kind)
    elif chapter is not None:
        raise _identity_required(kind)
    expected_mac = _origin_mac(
        key, origin, project_id=change.project_id, conversation_id=change.conversation_id,
        turn_task_id=change.turn_task_id, tool_call_id=change.tool_call_id, arguments=change.arguments,
    )
    if not hmac.compare_digest(origin["mac"], expected_mac):
        raise _identity_required(kind)
    return origin


@dataclass(frozen=True, slots=True, eq=False)
class PreparedTaskAdmission:
    """确认请求在锁外准备的受理；只能用于同一提案，error 表示预检失败、应记为提案失败。"""

    change_key: str
    kind: str
    params_json: str
    admission: TaskAdmission | None
    error: str | None = None


async def prepare_confirmed_admission(
    db: AsyncSession, deps: TaskContext, change: ChangeSnapshot, *, user_id: int, project_id: int,
) -> PreparedTaskAdmission | None:
    """必须在取得任何业务写锁前调用：核对来源证明，再做短读模型预检与 DNS。"""
    from app.services import generation_service
    from app.services.task_engine.admission import prepare_task_admission

    kind = _admitted_kind(change)
    if kind is None or change.status != "proposed":
        return None
    origin = _verified_origin(db, change, kind)
    actor = _bound_actor(db, user_id, kind)
    if (origin["actor"] != actor.id or origin["token_version"] != actor.token_version
            or change.project_id != project_id):
        raise _identity_required(kind)
    try:
        args = StartArgs.model_validate(change.arguments)
        params = _request_params(project_id, args)
    except (ValidationError, AppError):
        # 参数非法时锁内的最新预览会失败并记录；这里不受理。
        return None
    params_json = _canonical(params).decode()
    await require_task_admission_open(db)
    try:
        if kind in _DERIVED_CHAPTER_KINDS:
            admission = await generation_service.prepare_chapter_task_admission(db, deps, user_id, kind, params)
        else:
            await db.rollback()
            model = await project_ai_config.preflight_project_model(
                deps.sessionmaker, actor, project_id, "text", cipher=deps.cipher,
                config_id=params.get("ai_config_id"), allow_private=deps.settings.allow_private_ai_endpoints,
                for_task_admission=True,
            )
            admission = await prepare_task_admission(deps, actor, kind, params, model_preflight=model)
    except (VisibilityPolicyError, TaskAdmissionClosed, SettingsUnavailable):
        raise
    except AppError as exc:
        return PreparedTaskAdmission(change.change_key, kind, params_json, None, exc.detail)
    return PreparedTaskAdmission(change.change_key, kind, params_json, admission)


async def _require_origin_facts(ctx: ToolCtx, change: AgentChange, origin: dict, kind: str) -> None:
    """锁内复核：原轮次 Task UUID/入队代次与真实 actor 令牌版本、章节实例均未变化。"""
    actor = _bound_actor(ctx.db, ctx.user_id, kind)
    with ctx.db.no_autoflush:
        turn = await ctx.db.scalar(select(Task).where(
            Task.id == change.turn_task_id, Task.statistics_key == origin["task_key"],
        ).execution_options(populate_existing=True))
    data = turn.input if turn is not None and isinstance(turn.input, dict) else {}
    if (turn is None or turn.kind != "agent.turn" or turn.user_id != actor.id or turn.project_id != ctx.project_id
            or turn.status != "paused" or _iso(turn.queued_at) != origin["queued_at"]
            or origin["actor"] != actor.id or origin["token_version"] != actor.token_version
            or data.get("_agent_actor_token_version") != actor.token_version
            or data.get("_task_actor_token_version") != actor.token_version):
        raise _identity_required(kind)
    if kind in _DERIVED_CHAPTER_KINDS:
        chapter = origin["chapter"]
        with ctx.db.no_autoflush:
            row = (await ctx.db.execute(select(Chapter.id, Chapter.created_at).where(
                Chapter.id == chapter["id"], Chapter.project_id == ctx.project_id,
            ))).one_or_none()
        if row is None or _iso(row.created_at) != chapter["created_at"]:
            raise _identity_required(kind)


async def execute_admitted(ctx: ToolCtx, change: AgentChange, prepared: PreparedTaskAdmission | None) -> dict:
    """确认事务根层级调用（不在保存点内）：中央 create_task 在自身保存点完成受理。

    模型重选失败时中央受理已回滚根事务，异常直接抛出，调用方不得把提案记为已处理。
    """
    from app.services import task_service

    kind = _admitted_kind(change)
    if kind is None:
        raise BadRequestError("工具不存在")
    origin = _verified_origin(ctx.db, change, kind)
    if prepared is None or prepared.change_key != change.change_key or prepared.kind != kind:
        raise _identity_required(kind)
    if prepared.error is not None:
        raise BadRequestError(prepared.error)
    args = await TOOLS[0]._validate(ctx, change.arguments)
    params = request_params(ctx, args)
    if prepared.admission is None or _canonical(params).decode() != prepared.params_json:
        raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})
    await _require_origin_facts(ctx, change, origin, kind)
    task = await task_service.create_task(
        ctx.db, DeferredEngine(ctx.task_context), ctx.user_id, kind, params,
        admission=prepared.admission, caller_transaction=True,
    )
    row = await ctx.db.get(Task, task.id)
    if row is None or row.project_id != ctx.project_id or row.user_id != ctx.user_id:
        raise NotFoundError("不存在")
    # 派生任务归真实 actor，且协作轮次派生的任务必须绑定同一邀请轮次（作者轮次两者均为 None）。
    # 不一致属授权变化：上抛终止并整体回滚确认事务，不能把已插入的任务当作提案失败后提交。
    if task_grant_identity(row.input if isinstance(row.input, dict) else {}) != ctx.grant:
        from app.services.agent.transactions import AgentAuthorizationChangedError

        raise AgentAuthorizationChangedError()
    return {"task_id": task.id, "task": task.model_dump(mode="json")}


TOOLS = [
    Tool("start_task", "启动已有的生成、润色、重写、批量、分析或设定生成任务", StartArgs, "task", execute, preview)
]
