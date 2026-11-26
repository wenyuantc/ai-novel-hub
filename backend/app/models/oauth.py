from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"
    __table_args__ = (UniqueConstraint("user_id", "provider"),)

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    subject: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)


class OAuthState(Base):
    __tablename__ = "oauth_states"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_digest: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(16))
    intent: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_version: Mapped[int | None]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
