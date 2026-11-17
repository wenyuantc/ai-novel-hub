from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PenName(TimestampMixin, Base):
    __tablename__ = "pen_names"
    __table_args__ = (
        UniqueConstraint("owner_id", "name"),
        UniqueConstraint("id", "owner_id"),
        CheckConstraint("revision >= 1", name="revision"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(default=1, server_default="1")


class ProjectPenName(Base):
    __tablename__ = "project_pen_names"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "owner_id"], ["projects.id", "projects.user_id"],
            name="fk_project_pen_names_project_owner", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["pen_name_id", "owner_id"], ["pen_names.id", "pen_names.owner_id"],
            name="fk_project_pen_names_pen_owner", ondelete="CASCADE",
        ),
    )

    project_id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int]
    pen_name_id: Mapped[int] = mapped_column(index=True)
