"""实例级角色/授权屏障与用户级配额；不属于小说备份数据。"""

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Role(TimestampMixin, Base):
    __tablename__ = "roles"
    __table_args__ = (CheckConstraint("revision >= 1", name="revision_positive"), {"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_key: Mapped[str | None] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True)


class AuthorizationState(Base):
    __tablename__ = "authorization_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class UserQuota(TimestampMixin, Base):
    __tablename__ = "user_quotas"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "(project_mode = 'limited' AND project_value IS NOT NULL AND project_value >= 0) OR "
            "(project_mode IN ('inherit', 'unlimited') AND project_value IS NULL)", name="project_config",
        ),
        CheckConstraint(
            "(task_mode = 'limited' AND task_value IS NOT NULL AND task_value >= 0) OR "
            "(task_mode IN ('inherit', 'unlimited') AND task_value IS NULL)", name="task_config",
        ),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    project_mode: Mapped[str] = mapped_column(String(16), default="inherit", server_default="inherit", nullable=False)
    project_value: Mapped[int | None] = mapped_column(Integer)
    task_mode: Mapped[str] = mapped_column(String(16), default="inherit", server_default="inherit", nullable=False)
    task_value: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
