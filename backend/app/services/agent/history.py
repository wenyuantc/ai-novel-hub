"""旧智能体上下文只读保留；收紧后的轮次不能复用它。"""

from itertools import batched
from typing import TYPE_CHECKING

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import (
    AgentChange,
    AgentConversation,
    AgentMessage,
    Career,
    CharacterCareer,
    Organization,
    OrganizationMember,
)
from app.services.ai_dispatch import VisibilityStamp, read_visibility_stamp

if TYPE_CHECKING:
    from app.services.setting_ai_context import AiScope


class HistorySealedError(ConflictError):
    def __init__(self) -> None:
        super().__init__(
            "AI 可见设置已收紧，旧会话只可查看，请新开会话继续",
            extra={"code": "ai_history_sealed"},
        )


def has_history_expression():
    # last_message_at 只是便捷标记；旧工具/摘要、导入和中断时的真实持久化记录也算历史。
    return or_(
        AgentConversation.last_message_at.is_not(None),
        AgentConversation.summarized_until.is_not(None),
        AgentConversation.summary != "",
        exists(select(AgentMessage.id).where(AgentMessage.conversation_id == AgentConversation.id)),
        exists(select(AgentChange.id).where(AgentChange.conversation_id == AgentConversation.id)),
    )


async def is_sealed(db: AsyncSession, conversation: AgentConversation, epoch: int) -> bool:
    if conversation.ai_history_epoch == epoch:
        return False
    return bool(await db.scalar(
        select(has_history_expression()).where(AgentConversation.id == conversation.id),
    ))


async def require_open_history(
    db: AsyncSession,
    conversation: AgentConversation,
    *,
    adopt_empty: bool = False,
    expected_epoch: int | None = None,
) -> VisibilityStamp:
    """只有新轮次创建、且已持项目与会话写锁时才可让空会话采用当前代次。"""
    stamp = await read_visibility_stamp(db, conversation.project_id)
    if expected_epoch is not None and expected_epoch != stamp.epoch:
        raise HistorySealedError
    if conversation.ai_history_epoch != stamp.epoch:
        if not adopt_empty or await is_sealed(db, conversation, stamp.epoch):
            raise HistorySealedError
        conversation.ai_history_epoch = stamp.epoch
    return stamp


async def build_tool_scope(db: AsyncSession, stamp: VisibilityStamp, author_text: str) -> "AiScope":
    """只使用本轮作者文本，不把模型工具参数或浏览页升级成显式引用。"""
    from app.services.setting_ai_context import build_scope

    scope = await build_scope(db, stamp.project_id, texts=(author_text,), legacy_all=True, stamp=stamp)
    direct_characters = {ident for kind, ident in scope.direct if kind == "character"}
    for ids in batched(sorted(direct_characters), 250):
        for target_kind, model, target, foreign_key in (
            ("organization", OrganizationMember, Organization, OrganizationMember.organization_id),
            ("career", CharacterCareer, Career, CharacterCareer.career_id),
        ):
            stmt = (
                select(model.character_id, foreign_key).join(target, target.id == foreign_key)
                .where(model.character_id.in_(ids), target.project_id == stamp.project_id)
            )
            if model is OrganizationMember:
                stmt = stmt.where(OrganizationMember.status == "active")
            rows = await db.execute(stmt)
            scope.include_associations((("character", source), (target_kind, ident)) for source, ident in rows)
    await scope.verify(db)
    return scope
