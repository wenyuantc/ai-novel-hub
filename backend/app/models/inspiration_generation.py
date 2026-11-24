"""一次灵感步骤生成的持久占用。"""

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InspirationGenerationClaim(Base):
    __tablename__ = "inspiration_generation_claims"

    session_id: Mapped[int] = mapped_column(
        ForeignKey("inspiration_sessions.id", ondelete="CASCADE"), primary_key=True,
    )
    step: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(36), nullable=False)
    worker_key: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
