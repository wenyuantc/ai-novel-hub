"""共享作品派生的本人灵感会话所依赖的原邀请。"""

from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InspirationOrigin(Base):
    """保留原作品和邀请，即使作品彻删后也不能把快照当成独立会话。"""

    __tablename__ = "inspiration_origins"
    __table_args__ = (
        CheckConstraint("project_id > 0", name="positive_project"),
        CheckConstraint("length(invitation_id) = 36", name="invitation_shape"),
        CheckConstraint("length(origin_key) = 36", name="origin_shape"),
        UniqueConstraint("origin_key"),
    )

    session_id: Mapped[int] = mapped_column(
        ForeignKey("inspiration_sessions.id", ondelete="CASCADE"), primary_key=True,
    )
    # 故意不引用 projects：物理删除后身份仍需保留，不能使旧会话成为无来源的个人会话。
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    invitation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    origin_key: Mapped[str] = mapped_column(String(36), default=lambda: str(uuid4()), nullable=False)
