from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TwoStepFactor(Base):
    __tablename__ = "two_step_factors"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    secret_enc: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool]
    setup_version: Mapped[int]
    setup_expires: Mapped[int]
    last_step: Mapped[int]
    window_start: Mapped[int]
    failures: Mapped[int]


class RecoveryCode(Base):
    __tablename__ = "two_step_recovery_codes"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("two_step_factors.user_id", ondelete="CASCADE"), index=True)


class LoginChallenge(Base):
    __tablename__ = "two_step_login_challenges"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("two_step_factors.user_id", ondelete="CASCADE"), index=True)
    version: Mapped[int]
    expires: Mapped[int] = mapped_column(index=True)
