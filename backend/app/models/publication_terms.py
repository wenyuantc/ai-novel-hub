from sqlalchemy import JSON, CheckConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PublicationTerms(Base):
    __tablename__ = "publication_terms"
    __table_args__ = (
        CheckConstraint("(id = 0 AND owner_id IS NULL) OR "
                        "(id > 0 AND owner_id IS NOT NULL AND id = owner_id)", name="scope"),
        CheckConstraint("revision > 0", name="revision_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    revision: Mapped[int]
    terms: Mapped[list[str]] = mapped_column(JSON)
