"""核心表定义。

不声明 ORM relationship：级联删除由数据库外键 ON DELETE 处理，
所有访问都经 service 层显式查询并按 user_id 过滤。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

CHAPTER_STATUSES = ("draft", "generating", "done")
AI_PROVIDERS = ("openai_compat", "anthropic", "gemini")
PROJECT_STATUSES = ("planning", "writing", "completed")
# 版本来源：手动保存 / AI 或回滚替换前补的快照 / 整章生成 / 润色 / 局部改写采纳 / 回滚
VERSION_SOURCES = ("manual", "baseline", "generate", "polish", "rewrite", "restore")
# 生成记录类型：整章生成、润色，以及局部改写的各个操作
GENERATION_KINDS = ("generate", "polish", "rewrite", "expand", "condense", "deslop", "continue")
GENERATION_STATUSES = ("success", "error", "cancelled")
# 后台任务
TASK_KINDS = ("chapter.generate", "chapter.polish", "wizard.outline", "wizard.characters", "wizard.world")
TASK_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")
ACTIVE_TASK_STATUSES = ("queued", "running")
FINISHED_TASK_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")
# 部分唯一索引条件：同一资源同时只能有一个排队 / 运行中的任务（PostgreSQL 与 SQLite 都支持）
_ACTIVE_RESOURCE_WHERE = "status IN ('queued', 'running') AND resource_key IS NOT NULL"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    # 会话版本号：重置密码 / 禁用 / 自助改密时递增，使旧令牌（payload.ver）立即失效
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class AIConfig(TimestampMixin, Base):
    __tablename__ = "ai_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    # text / image；默认配置按类型各自唯一
    model_type: Mapped[str] = mapped_column(String(16), default="text", server_default="text", nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # off / low / medium / high；关闭 = 不发送任何思考参数
    thinking_level: Mapped[str] = mapped_column(String(16), default="off", server_default="off", nullable=False)
    # 单位 K（×1000 tokens）；为空表示使用系统 / 接口默认
    context_window_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_k: Mapped[int | None] = mapped_column(Integer, nullable=True)


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


class GenerationLog(TimestampMixin, Base):
    """一次 AI 调用的记录。provider / model 冗余存储，配置删除后仍可查看。"""

    __tablename__ = "generation_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    # 章节删除后记录保留在项目下
    chapter_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    ai_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_configs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    output_chars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 预留：现有 provider 流式接口拿不到 token 用量，暂时为空
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 产生该调用的后台任务；任务被清理后记录保留
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True)


class ChapterVersion(TimestampMixin, Base):
    """章节正文快照（事件发生后的状态）。合并手动保存时会刷新 updated_at。"""

    __tablename__ = "chapter_versions"
    __table_args__ = (UniqueConstraint("chapter_id", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True, nullable=False)
    # 章节内递增序号（max+1），删除后不复用
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    starred: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    note: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    generation_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("generation_logs.id", ondelete="SET NULL"), index=True, nullable=True
    )


class Task(TimestampMixin, Base):
    """持久化后台任务。tasks 表即队列：进程内调度器按 id 先进先出领取 queued 任务。"""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_id", "status", "id"),
        Index("ix_tasks_user_id_id", "user_id", "id"),
        Index(
            "uq_tasks_active_resource",
            "resource_key",
            unique=True,
            postgresql_where=text(_ACTIVE_RESOURCE_WHERE),
            sqlite_where=text(_ACTIVE_RESOURCE_WHERE),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # (user_id, id) 复合索引已覆盖按用户查询，不再单独建索引
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    chapter_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # 展示用中文标题（创建时生成，章节被删 / 改名后保持不变）
    title: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    # chapter:{id} / wizard:{project_id}:{kind}；用于同资源互斥
    resource_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_text: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    stage: Mapped[str] = mapped_column(String(100), default="", server_default="", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 多步任务断点（当前单步类型不用）
    step_cursor: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    retry_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
