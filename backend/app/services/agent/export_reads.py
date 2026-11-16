"""可阅读文档导出的本人会话快照及渲染后独立授权复核。

证明只保存在服务器内存中，调用方只能用 conversations 生成文件；这不是项目备份。
"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, UnauthorizedError
from app.models import AgentConversation, AgentMessage, Project, ProjectCollaborator, User
from app.schemas.agent import ConversationOut, MessageOut
from app.services import personal_projection, project_access
from app.services.agent import access as agent_access
from app.services.agent import history
from app.services.ai_dispatch import visibility_sessionmaker
from app.services.authorization_service import AuthenticatedActor

_BATCH = 200


@dataclass(frozen=True, slots=True, repr=False)
class DocumentAccessCapture:
    actor: AuthenticatedActor
    project_id: int
    owner_id: int
    project_created_at: Any
    last_deleted_at: Any
    grant: tuple[str, int] | None
    conversation_proofs: dict[int, dict[str, Any]] = field(repr=False)
    message_proofs: dict[int, dict[str, Any]] = field(repr=False)
    raw_payload: list[dict[str, Any]] = field(repr=False)
    projected: list[dict[str, Any]] = field(repr=False)
    fingerprints: dict[str, dict[int, Any]] = field(repr=False)

    @property
    def conversations(self) -> list[dict[str, Any]]:
        """防止调用方修改用于最终比对的原始安全投影。"""
        return deepcopy(self.projected)


def _changed() -> ConflictError:
    return ConflictError("导出期间内容或授权已变化，请重试", extra={"code": "export_snapshot_changed"})


def _actor(db: AsyncSession, user_id: int) -> AuthenticatedActor:
    original = db.info.get("authenticated_actor")
    if not isinstance(original, AuthenticatedActor) or original.id != user_id:
        raise UnauthorizedError("未登录或登录已过期")
    return original


def _chunks(ids: list[int]):
    for offset in range(0, len(ids), _BATCH):
        yield ids[offset:offset + _BATCH]


def _conversation_snapshot(conversation: AgentConversation, *, writable: bool) -> dict[str, Any]:
    return ConversationOut.model_validate(conversation).model_copy(
        update={"writable": writable},
    ).model_dump(mode="json")


async def capture_document_access(
    db: AsyncSession, user_id: int, project_id: int, conversation_ids: list[int],
) -> DocumentAccessCapture:
    """验证导出能力并捕获所选本人会话；不得把请求中的 ID 当成授权。"""
    actor = _actor(db, user_id)
    if (len(set(conversation_ids)) != len(conversation_ids)
            or any(type(i) is not int or i <= 0 for i in conversation_ids)):
        raise NotFoundError("会话不存在")
    with db.no_autoflush:
        project, access, member = await project_access.resolve_access(db, user_id, project_id)
        if not access.can_export:
            raise NotFoundError("项目不存在")
        grant = None if project.user_id == user_id else (member.invitation_id, member.revision) if member else None
        if project.user_id != user_id and grant is None:
            raise NotFoundError("项目不存在")
        # 用标量从数据库读取，避免同会话内旧 ORM 对象或陈旧项目代次成为证据。
        source = (await db.execute(select(Project.created_at, Project.last_deleted_at, Project.ai_history_epoch)
                                   .where(Project.id == project_id))).one()
        entry = agent_access.AgentEntry(project, access, member)
        selected: dict[int, tuple[AgentConversation, bool]] = {}
        for batch in _chunks(sorted(conversation_ids)):
            rows = await db.execute(
                select(AgentConversation, history.has_history_expression()).where(
                    AgentConversation.id.in_(batch), AgentConversation.project_id == project_id,
                    AgentConversation.user_id == user_id,
                ).execution_options(populate_existing=True)
            )
            selected.update({row.id: (row, has_history) for row, has_history in rows})
        if set(selected) != set(conversation_ids):
            raise NotFoundError("会话不存在")
        conversations = sorted(selected.values(), key=lambda pair: pair[0].id)
        proofs = {}
        raw = []
        for conversation, has_history in conversations:
            snapshot = _conversation_snapshot(conversation, writable=agent_access.conversation_writable(
                entry, user_id, conversation,
            ))
            proofs[conversation.id] = {
                "id": conversation.id,
                "created_at": jsonable_encoder(conversation.created_at),
                "snapshot": snapshot,
                "sealed": conversation.ai_history_epoch != source.ai_history_epoch and bool(has_history),
            }
            raw.append({"id": conversation.id, "title": conversation.title, "messages": []})
        target = {item["id"]: item for item in raw}
        message_proofs = {}
        for batch in _chunks(sorted(conversation_ids)):
            messages = await db.scalars(select(AgentMessage).where(
                AgentMessage.conversation_id.in_(batch), AgentMessage.role.in_(("user", "assistant")),
            ).order_by(AgentMessage.conversation_id, AgentMessage.id).execution_options(populate_existing=True))
            for message in messages:
                frozen = MessageOut.model_validate(message).model_dump(mode="json")
                message_proofs[message.id] = frozen
                target[message.conversation_id]["messages"].append({
                    "role": message.role, "content": message.content, "created_at": frozen["created_at"],
                })
        projected, fingerprints = await personal_projection.project_document_payload_in_snapshot(
            db, user_id, project_id, raw, is_owner=project.user_id == user_id,
        )
        return DocumentAccessCapture(
            actor=actor, project_id=project_id, owner_id=project.user_id,
            project_created_at=jsonable_encoder(source.created_at),
            last_deleted_at=jsonable_encoder(source.last_deleted_at), grant=grant,
            conversation_proofs=proofs, message_proofs=message_proofs, raw_payload=raw,
            projected=projected, fingerprints=fingerprints,
        )


async def _verify_access(db: AsyncSession, capture: DocumentAccessCapture):
    actor = capture.actor
    fact = (await db.execute(
        select(
            User.is_active.label("active"), User.token_version, Project.id.label("project_id"),
            Project.user_id.label("owner_id"), Project.created_at.label("project_created_at"), Project.deleted_at,
            Project.last_deleted_at, Project.deletion_started_at, Project.ai_history_epoch,
            ProjectCollaborator.status.label("member_status"), ProjectCollaborator.role.label("member_role"),
            ProjectCollaborator.invitation_id.label("member_invitation_id"),
            ProjectCollaborator.revision.label("member_revision"),
        ).select_from(User).outerjoin(Project, Project.id == capture.project_id).outerjoin(
            ProjectCollaborator, and_(ProjectCollaborator.project_id == Project.id,
                                      ProjectCollaborator.user_id == actor.id),
        ).where(User.id == actor.id)
    )).one_or_none()
    if fact is None or not fact.active or fact.token_version != actor.token_version:
        raise UnauthorizedError("未登录或登录已过期")
    if fact.project_id is None or fact.deleted_at is not None or fact.owner_id != capture.owner_id:
        raise NotFoundError("项目不存在")
    if fact.deletion_started_at is not None:
        raise ConflictError("作品正在删除，请稍后重试", extra={"code": "project_deleting"})
    if (jsonable_encoder(fact.project_created_at) != capture.project_created_at
            or jsonable_encoder(fact.last_deleted_at) != capture.last_deleted_at):
        raise _changed()
    if fact.owner_id == actor.id:
        if capture.grant is not None:
            raise _changed()
    elif (fact.member_status != "accepted" or capture.grant is None
          or (fact.member_invitation_id, fact.member_revision) != capture.grant):
        raise _changed()
    return fact


async def _verify_conversations(db: AsyncSession, capture: DocumentAccessCapture, fact) -> None:
    ids = sorted(capture.conversation_proofs)
    found: set[int] = set()
    for batch in _chunks(ids):
        rows = await db.execute(select(AgentConversation, history.has_history_expression()).where(
            AgentConversation.id.in_(batch), AgentConversation.project_id == capture.project_id,
            AgentConversation.user_id == capture.actor.id,
        ).execution_options(populate_existing=True))
        for conversation, has_history in rows:
            expected = capture.conversation_proofs[conversation.id]
            writable = agent_access.conversation_writable_fact(
                user_id=capture.actor.id, owner_id=fact.owner_id, member_status=fact.member_status,
                member_role=fact.member_role, member_invitation_id=fact.member_invitation_id,
                conversation_invitation_id=conversation.collaboration_invitation_id,
            )
            sealed = conversation.ai_history_epoch != fact.ai_history_epoch and bool(has_history)
            if (jsonable_encoder(conversation.created_at) != expected["created_at"]
                    or _conversation_snapshot(conversation, writable=writable) != expected["snapshot"]
                    or sealed != expected["sealed"]):
                raise _changed()
            found.add(conversation.id)
    if found != set(ids):
        raise _changed()

    message_ids = sorted(capture.message_proofs)
    found_messages: set[int] = set()
    for batch in _chunks(message_ids):
        messages = await db.scalars(select(AgentMessage).where(AgentMessage.id.in_(batch))
                                    .execution_options(populate_existing=True))
        for message in messages:
            expected = capture.message_proofs[message.id]
            if (message.conversation_id not in found or message.conversation_id != expected["conversation_id"]
                    or MessageOut.model_validate(message).model_dump(mode="json") != expected):
                raise _changed()
            found_messages.add(message.id)
    if found_messages != set(message_ids):
        raise _changed()


async def verify_document_access(db: AsyncSession, capture: DocumentAccessCapture) -> None:
    """渲染结束后以全新短只读快照核验，调用方收到异常时必须丢弃附件。"""
    if _actor(db, capture.actor.id) != capture.actor:
        raise UnauthorizedError("未登录或登录已过期")
    factory = visibility_sessionmaker(db)
    async with factory() as verify:
        verify.info["authenticated_actor"] = capture.actor
        dialect = verify.get_bind().dialect.name
        if dialect == "postgresql":
            await verify.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        elif dialect == "sqlite":
            # sqlite3 默认 SELECT 不自动 BEGIN；必须在首个 SELECT 之前真正开始读事务。
            await verify.execute(text("BEGIN"))
        else:
            raise RuntimeError("不支持的文档导出数据库")
        try:
            fact = await _verify_access(verify, capture)
            await _verify_conversations(verify, capture, fact)
            projected, fingerprints = await personal_projection.project_document_payload_in_snapshot(
                verify, capture.actor.id, capture.project_id, capture.raw_payload,
                is_owner=capture.actor.id == fact.owner_id,
            )
            if projected != capture.projected or fingerprints != capture.fingerprints:
                raise _changed()
        finally:
            await verify.rollback()
