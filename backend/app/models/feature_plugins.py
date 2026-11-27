from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FeaturePluginState(Base):
    __tablename__ = "feature_plugin_states"
    __table_args__ = (CheckConstraint("revision >= 1", name="positive_revision"),)

    plugin_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    revision: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_operation_id: Mapped[str | None] = mapped_column(String(36))
