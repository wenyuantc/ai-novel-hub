"""按已确认的保存稿范围提出拆并建议，采纳事务复用人工结构写入。"""

import hmac
import json
import re
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts.scene import build_restructure_messages
from app.ai.providers.base import ChatMessage
from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.security import SecretCipher
from app.models import Chapter, Project, Scene, Task
from app.schemas.scene import (
    SceneBlocker,
    SceneConflict,
    SceneMerge,
    SceneMergePreview,
    SceneMergePreviewOut,
    SceneMutation,
    SceneSplit,
)
from app.schemas.scene_restructure import (
    MAX_CANDIDATES,
    RestructureApply,
    RestructureCandidate,
    RestructureChoices,
    RestructurePreview,
    RestructureProposal,
    RestructureRequest,
    RestructureResult,
    RestructureScene,
    RestructureStart,
    RestructureTaskRequest,
)
from app.schemas.task import TaskOut
from app.services import authorization_service as authz
from app.services import project_access, project_ai_config, scene_ai_context, scene_service
from app.services.audit_writer import AuditOperation
from app.services.chapter_service import get_manual_chapter
from app.services.collaboration_task_access import GRANT_KEY, GrantIdentity, task_grant_identity
from app.services.rich_document_store import capture_documents

KIND = "scene.restructure"
_MAX_SOURCE_CHARS = 12_000
MAX_RESPONSE_CHARS = 4_096
_PARAGRAPH_BREAK = re.compile(r"(?:\r\n|[\r\n])(?:[^\S\r\n]*(?:\r\n|[\r\n]))*")
_LINE_BREAK = re.compile(r"\r\n|[\r\n]")


@dataclass(frozen=True, slots=True, repr=False)
class SavedPlan:
    preview: RestructurePreview
    stamp: scene_ai_context.SceneContextStamp
    merge: SceneMergePreviewOut | None
    merge_separator: str


def _conflict(detail: str = "场景拆并建议已过期，请核对稿件后重新生成") -> ConflictError:
    return ConflictError(detail, extra={"code": "scene_preview_changed"})


def _authority(db: AsyncSession, user_id: int) -> authz.AuthenticatedActor:
    actor = authz.actor_snapshot(db, user_id)
    if not isinstance(actor, authz.AuthenticatedActor) or actor.id != user_id:
        raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    return actor


async def _access(
    db: AsyncSession, user_id: int, project_id: int, *, write: bool = False,
) -> tuple[Project, GrantIdentity | None]:
    project, access, member = await project_access.resolve_access(
        db, user_id, project_id, for_update=write, visibility_write=write,
    )
    if not (access.can_edit_content and access.can_use_ai):
        if not access.is_collaborator:
            raise NotFoundError("项目不存在")
        raise ForbiddenError("当前作品没有执行该操作的权限", extra={"code": "project_readonly"})
    grant = (member.invitation_id, member.revision) if access.is_collaborator and member is not None else None
    return project, grant


def _signature(settings: Settings, data: object) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.digest(settings.jwt_secret.encode(), canonical.encode(), "sha256").hex()


async def _split_candidates(db: AsyncSession, scene: Scene) -> tuple[list[RestructureCandidate], list[SceneBlocker]]:
    text = scene.content
    first = len(text) - len(text.lstrip())
    last = len(text.rstrip())
    offsets = [match.end() for match in _PARAGRAPH_BREAK.finditer(text) if first < match.end() < last]
    if not offsets:
        return [], []
    if len(offsets) > MAX_CANDIDATES:
        offsets = [offsets[i * (len(offsets) - 1) // (MAX_CANDIDATES - 1)] for i in range(MAX_CANDIDATES)]
    documents, has_documents = await capture_documents(db, [scene])
    doc = documents[scene.id] if has_documents else None
    candidates = []
    for offset in offsets:
        try:
            scene_service.plan_split_document(text, offset, doc)
        except ConflictError:
            continue
        candidates.append(RestructureCandidate(candidate_id=len(candidates) + 1, offset=offset))
    blockers = [] if candidates else [SceneBlocker(
        kind="rich_document", id=scene.id, scene_id=scene.id,
        label="候选段落边界上的富格式、标记或区块无法安全拆分",
    )]
    return candidates, blockers


async def read_plan(
    db: AsyncSession, chapter: Chapter, request: RestructureRequest, actor: authz.AuthenticatedActor,
    grant: GrantIdentity | None, *, settings: Settings,
) -> SavedPlan:
    """同一份保存稿供预览、任务受理、构建及采纳，不读取作者区块作为素材。"""
    with db.no_autoflush:
        stamp = await scene_ai_context.read_stamp(db, chapter.project_id, chapter.id)
        await scene_service.check_revision(db, chapter, request.expected_content_revision)
        selected = [await scene_service.require_scene(db, chapter, scene_id) for scene_id in request.scene_ids]
        if sum(len(scene.content) for scene in selected) > min(_MAX_SOURCE_CHARS, settings.context_char_budget):
            raise BadRequestError("选中的场景正文超出 AI 建议范围，请先缩小场景")
        scenes = [RestructureScene(scene_id=scene.id, title=scene.title, content=scene.content) for scene in selected]
        merge = None
        candidates: list[RestructureCandidate] = []
        conflicts: list[SceneConflict] = []
        if request.operation == "merge":
            # 由人工合并预览决定相邻顺序、富文档可映射性和保留引用。
            merge = await scene_service._merge_preview(db, chapter, SceneMergePreview(
                scene_ids=request.scene_ids, expected_content_revision=request.expected_content_revision,
            ))
            blockers, conflicts = merge.blockers, merge.conflicts
            if not blockers and any(scene.content.strip() for scene in selected):
                candidates = [RestructureCandidate(candidate_id=1, offset=None)]
        else:
            blockers = [SceneBlocker.model_validate(row) for row in await scene_service.reference_blockers(
                db, chapter, request.scene_ids,
            )]
            if not blockers:
                candidates, blockers = await _split_candidates(db, selected[0])
        await scene_ai_context.require_stamp(db, stamp)
        proof = _signature(settings, [
            "scene-restructure-preview:v1", actor.id, actor.token_version, grant,
            stamp.log_data(), request.operation, request.scene_ids,
            [item.model_dump(mode="json") for item in scenes], [scene.separator_before for scene in selected],
            [item.model_dump(mode="json") for item in candidates], [item.model_dump(mode="json") for item in blockers],
            merge.signature if merge is not None else None,
        ])
        preview = RestructurePreview(
            operation=request.operation, scene_ids=request.scene_ids, content_revision=stamp.content_revision,
            planning_revision=stamp.planning_revision, signature=proof, scenes=scenes, candidates=candidates,
            blockers=blockers, conflicts=conflicts,
        )
    return SavedPlan(preview, stamp, merge, selected[1].separator_before if merge is not None else "")


async def confirmed_plan(
    db: AsyncSession, chapter: Chapter, request: RestructureStart, actor: authz.AuthenticatedActor,
    grant: GrantIdentity | None, *, settings: Settings,
) -> SavedPlan:
    plan = await read_plan(db, chapter, request, actor, grant, settings=settings)
    if not hmac.compare_digest(request.signature, plan.preview.signature):
        raise _conflict("场景预览已变化，请重新核对")
    if plan.preview.blockers:
        raise ConflictError("请先处理场景的保留引用或富文档资料", extra={
            "code": "scene_reference_conflict", "blockers": [item.model_dump() for item in plan.preview.blockers],
        })
    if not plan.preview.candidates:
        raise _conflict("当前稿件没有可用的拆并候选")
    return plan


async def preview(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, data: RestructureRequest, *, settings: Settings,
) -> RestructurePreview:
    _, grant = await _access(db, user_id, project_id)
    chapter = await get_manual_chapter(db, user_id, project_id, chapter_id, "content.read")
    plan = await read_plan(db, chapter, data, _authority(db, user_id), grant, settings=settings)
    _, current_grant = await _access(db, user_id, project_id)
    if current_grant != grant:
        raise ForbiddenError("协作授权已变化", extra={"code": "permission_denied"})
    await scene_ai_context.require_stamp(db, plan.stamp)
    return plan.preview


async def create_task(
    db: AsyncSession, engine, user_id: int, project_id: int, chapter_id: int, data: RestructureStart,
) -> TaskOut:
    from app.services import task_service
    from app.services.task_engine.admission import prepare_task_admission

    ctx = engine.ctx
    actor = _authority(db, user_id)
    if db.new or db.dirty or db.deleted:
        raise _conflict("任务受理凭据已变化，请重新发起")
    await db.rollback()
    # 用短会话核对预览，再在锁外准备模型，避免阻塞作者保存。
    async with ctx.sessionmaker() as read:
        await authz.require_authenticated(read, actor)
        _, grant = await _access(read, user_id, project_id)
        chapter = await get_manual_chapter(read, user_id, project_id, chapter_id, "content.read")
        await confirmed_plan(read, chapter, data, actor, grant, settings=ctx.settings)
    model = await project_ai_config.preflight_project_model(
        ctx.sessionmaker, actor, project_id, "text", cipher=ctx.cipher,
        allow_private=ctx.settings.allow_private_ai_endpoints, for_task_admission=True,
    )
    params = {"project_id": project_id, "chapter_id": chapter_id, **data.model_dump(mode="json")}
    admission = await prepare_task_admission(ctx, actor, KIND, params, model_preflight=model)
    return await task_service.create_task(db, engine, user_id, KIND, params, admission=admission)


def task_request(payload: object, project_id: int, chapter_id: int) -> RestructureTaskRequest:
    try:
        if not isinstance(payload, dict):
            raise ValueError
        request = RestructureTaskRequest.model_validate(payload.get("request"))
        if (request.project_id, request.chapter_id) != (project_id, chapter_id):
            raise ValueError
        return request
    except (ValidationError, ValueError):
        pass
    raise _conflict()


def model_binding(
    payload: dict, actor_id: int, project: Project, grant: GrantIdentity | None,
) -> project_ai_config.ResolvedProjectModel:
    binding = project_ai_config.deserialize_project_model(
        payload.get("_project_model"), actor_id=actor_id, project_id=project.id, project_owner_id=project.user_id,
    )
    if (binding is None or binding.model_type != "text" or binding.grant_identity != grant
            or type(payload.get("ai_config_id")) is not int or binding.config_id != payload["ai_config_id"]
            or (GRANT_KEY in payload) != (grant is not None) or task_grant_identity(payload) != grant):
        raise _conflict("模型绑定已变化，请重新生成建议")
    return binding


def model_messages(preview: RestructurePreview) -> list[ChatMessage]:
    left = preview.scenes[0].content
    right = preview.scenes[1].content if preview.operation == "merge" else ""
    lines = [(item.candidate_id, len(_LINE_BREAK.findall(left[:item.offset])) + 1)
             for item in preview.candidates if item.offset is not None]
    return build_restructure_messages(operation=preview.operation, candidates=lines, left=left, right=right)


def check_model_budget(messages: list[ChatMessage], char_budget: int) -> None:
    if sum(len(message["content"]) for message in messages) > char_budget:
        raise BadRequestError("当前模型的上下文不足，请先缩小所选场景")


def parse_choices(raw: object, preview: RestructurePreview) -> RestructureChoices:
    """严格约束模型原值，候选 ID 只能来自本次服务端预检。"""
    choices = (RestructureChoices.model_validate_json(raw) if isinstance(raw, str)
               else RestructureChoices.model_validate(raw))
    allowed = {item.candidate_id for item in preview.candidates}
    selected = [item.candidate_id for item in choices.proposals]
    if len(selected) != len(set(selected)) or not set(selected) <= allowed:
        raise ValueError("无效的场景候选")
    return choices


@dataclass(frozen=True, slots=True, repr=False)
class _Review:
    chapter: Chapter
    plan: SavedPlan
    result: RestructureResult


async def _review(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, task_key: UUID, *,
    settings: Settings, cipher: SecretCipher, write: bool = False,
) -> _Review:
    project, grant = await _access(db, user_id, project_id, write=write)
    statement = select(Task).where(
        Task.statistics_key == str(task_key), Task.user_id == user_id, Task.project_id == project_id,
        Task.chapter_id == chapter_id, Task.kind == KIND,
    ).execution_options(populate_existing=True)
    task = await db.scalar(statement.with_for_update() if write else statement)
    if task is None:
        raise NotFoundError("任务不存在")
    actor = _authority(db, user_id)
    request = task_request(task.input, project_id, chapter_id)
    if (task.status != "succeeded" or task.execution_key is not None or task.finished_at is None
            or type(task.input.get("_task_actor_token_version")) is not int
            or task.input["_task_actor_token_version"] != actor.token_version
            or grant != task_grant_identity(task.input)):
        raise _conflict("当前任务结果不可用，请重新生成建议")
    if write:
        chapter = await scene_service.get_manual_write_chapter(
            db, user_id, project_id, chapter_id, visibility_write=True,
        )
    else:
        chapter = await get_manual_chapter(db, user_id, project_id, chapter_id, "content.read")
    plan = await confirmed_plan(db, chapter, request, actor, grant, settings=settings)
    try:
        if type(task.result) is not dict:
            raise ValueError
        choices = parse_choices(task.result, plan.preview)
    except (ValidationError, ValueError):
        raise _conflict("任务结果无法验证，请重新生成建议") from None
    try:
        binding = model_binding(task.input, user_id, project, grant)
        await project_ai_config.revalidate_project_model(
            db, binding, cipher=cipher, allow_private=settings.allow_private_ai_endpoints,
            for_update=write, lock_config=write,
        )
    except BadRequestError:
        raise _conflict("模型绑定已变化，请重新生成建议") from None
    await scene_ai_context.require_stamp(db, plan.stamp)
    result = _render_review(str(task_key), plan, choices, settings)
    return _Review(chapter, plan, result)


def _render_review(
    task_key: str, plan: SavedPlan, choices: RestructureChoices, settings: Settings,
) -> RestructureResult:
    preview = plan.preview
    offsets = {item.candidate_id: item.offset for item in preview.candidates}
    source = preview.scenes[0].content
    proposals = []
    for item in choices.proposals:
        offset = offsets[item.candidate_id]
        if offset is None:
            before, after = source, plan.merge_separator + preview.scenes[1].content
        else:
            before, after = source[:offset], source[offset:]
        proposals.append(RestructureProposal(**item.model_dump(), offset=offset, before=before, after=after))
    return RestructureResult(
        operation=preview.operation, scene_ids=preview.scene_ids, content_revision=preview.content_revision,
        planning_revision=preview.planning_revision, conflicts=preview.conflicts, proposals=proposals,
        signature=_signature(settings, [
            "scene-restructure-apply:v1", task_key, preview.signature, choices.model_dump(mode="json"),
        ]),
    )


async def get_result(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, task_key: UUID, *,
    settings: Settings, cipher: SecretCipher,
) -> RestructureResult:
    return (await _review(db, user_id, project_id, chapter_id, task_key, settings=settings, cipher=cipher)).result


async def apply_result(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, task_key: UUID, data: RestructureApply, *,
    settings: Settings, cipher: SecretCipher,
) -> SceneMutation:
    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        review = await _review(
            db, user_id, project_id, chapter_id, task_key, settings=settings, cipher=cipher, write=True,
        )
        result = review.result
        proposal = next((item for item in result.proposals if item.candidate_id == data.candidate_id), None)
        if not hmac.compare_digest(data.signature, result.signature) or proposal is None:
            raise _conflict("选中的建议与核对结果不一致，请重新核对")
        chapter = review.chapter
        audit.target(chapter)
        audit.track(chapter, ["content"])
        if result.operation == "split":
            if data.metadata_source is not None or proposal.offset is None:
                raise scene_service.SceneParameterError("拆分建议仅接受右侧场景标题")
            items = await scene_service.split_scene_locked(db, chapter, result.scene_ids[0], SceneSplit(
                offset=proposal.offset, right_title=data.right_title, expected_content_revision=result.content_revision,
            ))
            note = "采纳 AI 拆分建议，原摘要保留在左片"
        else:
            if data.metadata_source is None or "right_title" in data.model_fields_set:
                raise scene_service.SceneParameterError("合并时请选择保留左侧或右侧场景资料")
            merge = review.plan.merge
            if merge is None:
                raise _conflict()
            items = await scene_service.merge_scenes_locked(db, chapter, SceneMerge(
                scene_ids=result.scene_ids, expected_content_revision=result.content_revision,
                expected_planning_revision=result.planning_revision, signature=merge.signature,
                metadata_source=data.metadata_source,
            ))
            note = "采纳 AI 合并建议"
        await scene_service._finish_structure(db, chapter, audit, note)
        return await scene_service._mutation(db, chapter, items)
