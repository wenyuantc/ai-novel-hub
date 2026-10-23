"""核心表定义。

不声明 ORM relationship：级联删除由数据库外键 ON DELETE 处理，
所有访问都经 service 层显式查询并按 user_id 过滤。
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

CHAPTER_STATUSES = ("draft", "generating", "done")
AI_PROVIDERS = ("openai_compat", "anthropic", "gemini")
PROJECT_STATUSES = ("planning", "writing", "completed")


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)


class AIConfig(TimestampMixin, Base):
    __tablename__ = "ai_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    genre: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    premise: Mapped[str] = mapped_column(Text, default="", nullable=False)
    style: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="planning", nullable=False)


class WorldSetting(TimestampMixin, Base):
    __tablename__ = "world_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Character(TimestampMixin, Base):
    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role_type: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    profile: Mapped[str] = mapped_column(Text, default="", nullable=False)
    relations_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Outline(TimestampMixin, Base):
    __tablename__ = "outlines"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    volume_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    volume_title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    chapter_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Chapter(TimestampMixin, Base):
    __tablename__ = "chapters"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    # 大纲被重新生成/删除时章节正文必须保留，因此用 SET NULL 而非 CASCADE
    outline_id: Mapped[int | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"), index=True, nullable=True
    )
    chapter_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
