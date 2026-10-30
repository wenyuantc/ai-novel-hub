"""平台访问密钥及小说授权。凭据不随项目备份迁移。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    all_projects: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rate_window: Mapped[int] = mapped_column(Integer, default=-1, server_default="-1", nullable=False)
    rate_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class ApiKeyProject(Base):
    __tablename__ = "api_key_projects"

    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
