"""用户级 Skill（写作工作流与参考资料）与 MCP 插件。不属于项目，不进项目备份。"""

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
    event,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Skill(TimestampMixin, Base):
    """owner_user_id 为空 = 全局 Skill（只有管理员可写）；builtin_key 标识内置 Skill。"""

    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "slug"),
        # (owner_user_id, slug) 唯一约束不约束 NULL；全局 Skill 另用部分唯一索引
        Index(
            "uq_skills_global_slug",
            "slug",
            unique=True,
            postgresql_where=text("owner_user_id IS NULL"),
            sqlite_where=text("owner_user_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    slug: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="", server_default="", nullable=False)
    category: Mapped[str] = mapped_column(String(30), default="", server_default="", nullable=False)
    triggers: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    source_skill_id: Mapped[int | None] = mapped_column(
        ForeignKey("skills.id", ondelete="SET NULL"), index=True, nullable=True
    )
    builtin_key: Mapped[str | None] = mapped_column(String(48), unique=True, nullable=True)


class SkillReference(TimestampMixin, Base):
    __tablename__ = "skill_references"

    id: Mapped[int] = mapped_column(primary_key=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)


class McpPlugin(TimestampMixin, Base):
    """远程 MCP 服务（Streamable HTTP / SSE）。请求头整体 JSON 序列化后 Fernet 加密，接口只回显请求头名称。"""

    __tablename__ = "mcp_plugins"
    __table_args__ = (UniqueConstraint("user_id", "slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    headers_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    # [{name, description, input_schema, read_only_hint, schema_hash}]（测试 / 刷新时写入）
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    # {tool_name: {enabled, auto_confirm}}；缺省 = 启用且需要确认
    tool_prefs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}", nullable=False)
    tools_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)


@event.listens_for(Skill.__table__, "after_create")
def _seed_builtin_skills(target, connection, **_kw) -> None:
    """create_all（测试）建表后写入内置 Skill；生产环境由迁移 0019 写入。"""
    from app.skills import load_builtin

    connection.execute(target.insert(), [{**item, "owner_user_id": None} for item in load_builtin()])
