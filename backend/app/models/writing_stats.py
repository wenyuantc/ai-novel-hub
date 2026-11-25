"""按操作者和作品保存每日写作量及个人码字目标。"""

from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, event, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import DDL

from app.models.base import Base


class WritingDailyStat(Base):
    __tablename__ = "writing_daily_stats"
    __table_args__ = (CheckConstraint("manual_words >= 0 AND ai_words >= 0", name="nonnegative_words"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(primary_key=True)
    manual_words: Mapped[int] = mapped_column(BigInteger, server_default="0")
    ai_words: Mapped[int] = mapped_column(BigInteger, server_default="0")


class WritingGoal(Base):
    __tablename__ = "writing_goals"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision"),
        CheckConstraint(
            "(period IS NULL AND target_words IS NULL) OR "
            "(period IS NOT NULL AND period IN ('day', 'week') AND target_words IS NOT NULL AND target_words > 0)",
            name="goal_shape",
        ),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    period: Mapped[str | None] = mapped_column(String(4))
    target_words: Mapped[int | None] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger, server_default="1")


class WritingStatsTracking(Base):
    __tablename__ = "writing_stats_tracking"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(WritingStatsTracking.__table__, "after_create", DDL("INSERT INTO writing_stats_tracking (id) VALUES (1)"))
