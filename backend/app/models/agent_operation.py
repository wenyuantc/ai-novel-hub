"""智能体会话当前操作的跨进程归属。"""

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentConversationOperation(Base):
    __tablename__ = "agent_conversation_operations"

    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_conversations.id", ondelete="CASCADE"), primary_key=True,
    )
    operation_key: Mapped[str] = mapped_column(String(36), nullable=False)
    worker_key: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
