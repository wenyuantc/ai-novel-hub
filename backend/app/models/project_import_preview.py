"""跨进程共享的项目导入暂存。

令牌只存摘要，备份原文按块加密。预览记录删除时级联清理其分块。
"""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ProjectImportPreviewQuota(Base):
    __tablename__ = "project_import_preview_quota"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("revision >= 1", name="positive_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ProjectImportPreview(Base):
    __tablename__ = "project_import_previews"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="positive_revision"),
        CheckConstraint("size_bytes BETWEEN 1 AND 104857600", name="size_limit"),
        Index("ix_project_import_previews_expiry", "expires_at", "revision"),
        Index("ix_project_import_previews_user_expiry", "user_id", "expires_at", "revision"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)


class ProjectImportPreviewChunk(Base):
    __tablename__ = "project_import_preview_chunks"
    __table_args__ = (CheckConstraint("chunk_index >= 0", name="nonnegative_index"),)

    token_hash: Mapped[str] = mapped_column(
        ForeignKey("project_import_previews.token_hash", ondelete="CASCADE"), primary_key=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
